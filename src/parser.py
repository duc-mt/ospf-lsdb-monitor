"""Parsing layer: raw ``show ... ospf database`` text -> standardized topology dict.

``OSPFParser`` is vendor-neutral. It looks up the platform's adapter (see
``src/vendors``), which turns raw CLI text into a small neutral model of Type 1
and Type 2 LSAs - via Cisco pyATS/Genie wherever Genie has a parser - and this
module flattens that model into nodes and edges.

Output of ``OSPFParser.parse()``::

    {
      "metadata": {"ospf_process_ids": ["1"], "areas": ["0.0.0.0"], ...},
      "nodes": [
        {"id": "10.4.1.1", "type": "router", "areas": [...], "resolved": True},
        {"id": "net-10.1.2.1", "type": "network", "address": "10.1.2.1",
         "dr": "10.4.1.1", "mask": "255.255.255.0", "prefix": "10.1.2.0/24", ...},
      ],
      "edges": [
        {"source": "10.4.1.1", "target": "net-10.1.2.1", "metric": 1,
         "link_type": "transit", "area": "0.0.0.0"},
      ],
    }

Edge model (directed, mirrors how OSPF describes the topology):

* router -> router   : point-to-point (or virtual) link, cost advertised by the source router
* router -> network  : router's interface cost onto a transit network (Type 1 link)
* network -> router  : attachment from a Type 2 LSA, always metric 0

Stub networks are deliberately not graphed. ``resolved`` is False for nodes that
are referenced by a link but have no LSA of their own in the polled database
(typically routers or DRs in a different area).
"""

from __future__ import annotations

import ipaddress
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from src import network_node_id, node_sort_key
from src.vendors import get_profile
from src.vendors.base import (
    KIND_P2P,
    KIND_STUB,
    KIND_TRANSIT,
    KIND_VIRTUAL,
    Lsdb,
    ParserError,
    dotted_mask,
)

__all__ = ["BaseParser", "OSPFParser", "ParserError"]

logger = logging.getLogger(__name__)

_EDGE_TYPE = {KIND_P2P: "point-to-point", KIND_TRANSIT: "transit", KIND_VIRTUAL: "virtual-link"}


class BaseParser(ABC):
    """Interface for parsers; keeps the pipeline independent of the vendor."""

    @abstractmethod
    def parse(self, raw_router: str, raw_network: str) -> dict[str, Any]:
        """Return the standardized ``{"metadata", "nodes", "edges"}`` dictionary."""


class OSPFParser(BaseParser):
    """Parse Type 1 (router) and Type 2 (network) LSAs for any registered platform."""

    def __init__(self, device_type: str = "cisco_ios", process_id: int | str | None = None) -> None:
        """
        Args:
            device_type: Netmiko-style device type; selects the platform adapter.
            process_id: Only keep this OSPF process/instance (``None`` keeps all of them).

        Raises:
            UnsupportedVendorError: If the device type is not registered.
            ParserError: If the adapter's dependencies (e.g. Genie) are missing.
        """
        self.device_type = device_type
        self.process_id = None if process_id is None else str(process_id)
        self.profile = get_profile(device_type)
        self.adapter = self.profile.adapter_factory()
        if self.process_id is not None and not self.adapter.filters_by_process:
            logger.warning("%s: ospf_process_id '%s' cannot be applied and is ignored",
                           self.profile.description, self.process_id)

    # ------------------------------------------------------------------- parse
    def parse(self, raw_router: str, raw_network: str = "") -> dict[str, Any]:
        """Parse raw LSDB text into the standardized topology dictionary.

        Raises:
            ParserError: If the platform parser fails or the output holds no router LSAs.
        """
        lsdb = self.adapter.parse(raw_router, raw_network, self.process_id)
        if not lsdb.router_lsas:
            hint = ""
            if self.process_id is not None:
                hint = f" for OSPF process {self.process_id} (process IDs in output: {lsdb.process_ids or 'none'})"
            raise ParserError(f"No router (Type 1) LSAs found{hint}")
        return self._build(lsdb)

    def _build(self, lsdb: Lsdb) -> dict[str, Any]:
        """Flatten the neutral LSA model into nodes and edges."""
        nodes: dict[str, dict] = {}
        edges: dict[tuple[str, str], dict] = {}

        # Type 1: routers and the links they advertise.
        for lsa in lsdb.router_lsas:
            self._upsert_node(nodes, lsa.router_id, "router", lsa.area, resolved=True)
            for link in lsa.links:
                self._add_router_link(nodes, edges, lsa.router_id, link, lsa.area)

        # Type 2: transit networks (the DR originates one per segment).
        for lsa in lsdb.network_lsas:
            mask = dotted_mask(lsa.mask)
            net_id = network_node_id(lsa.address)
            self._upsert_node(
                nodes, net_id, "network", lsa.area, resolved=True,
                address=lsa.address, dr=lsa.dr, mask=mask, prefix=self._prefix(lsa.address, mask),
            )
            for attached in lsa.attached:
                self._upsert_node(nodes, attached, "router", lsa.area)
                self._add_edge(edges, net_id, attached, 0, "attachment", lsa.area)

        unresolved = sorted(n["id"] for n in nodes.values() if not n["resolved"])
        if unresolved:
            logger.info("%d node(s) referenced but not described by an LSA in this database: %s",
                        len(unresolved), ", ".join(unresolved))

        for node in nodes.values():
            node["areas"] = sorted(node["areas"])
        return {
            "metadata": {
                "device_type": self.device_type,
                "ospf_process_ids": lsdb.process_ids,
                "areas": sorted({a for n in nodes.values() for a in n["areas"]}),
                "router_lsas": len(lsdb.router_lsas),
                "network_lsas": len(lsdb.network_lsas),
                "parsed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            "nodes": [nodes[k] for k in sorted(nodes, key=node_sort_key)],
            "edges": [edges[k] for k in sorted(edges, key=lambda e: (node_sort_key(e[0]), node_sort_key(e[1])))],
        }

    def _add_router_link(self, nodes, edges, router_id: str, link, area: str) -> None:
        """Translate one Type 1 link into a node (if needed) and a directed edge."""
        if link.kind == KIND_STUB or link.kind not in _EDGE_TYPE:
            return
        if link.metric is None:
            logger.warning("Router %s: %s link %s has no usable metric; skipped", router_id, link.kind, link.link_id)
            return
        if link.kind == KIND_TRANSIT:
            # link_id is the DR's interface address -> the network node.
            target = network_node_id(link.link_id)
            self._upsert_node(nodes, target, "network", area, address=link.link_id)
        else:
            # link_id is the neighbor's router ID.
            target = link.link_id
            self._upsert_node(nodes, target, "router", area)
        self._add_edge(edges, router_id, target, link.metric, _EDGE_TYPE[link.kind], area)

    @staticmethod
    def _prefix(address: str, mask: str | None) -> str | None:
        try:
            return str(ipaddress.ip_interface(f"{address}/{mask}").network)
        except ValueError:
            return None

    @staticmethod
    def _upsert_node(
        nodes: dict[str, dict], node_id: str, node_type: str, area: str, resolved: bool = False, **attrs: Any
    ) -> None:
        """Create a node or merge more information into an existing one."""
        node = nodes.setdefault(node_id, {"id": node_id, "type": node_type, "areas": set(), "resolved": False})
        node["areas"].add(area)
        if resolved:
            node["resolved"] = True
        node.update({k: v for k, v in attrs.items() if v is not None})

    @staticmethod
    def _add_edge(
        edges: dict[tuple[str, str], dict], source: str, target: str, metric: int, link_type: str, area: str
    ) -> None:
        """Add a directed edge; parallel links between the same pair keep the lowest metric."""
        existing = edges.get((source, target))
        if existing is None or metric < existing["metric"]:
            edges[(source, target)] = {
                "source": source, "target": target, "metric": metric, "link_type": link_type, "area": area,
            }
