from __future__ import annotations

"""
==============================================================================
Module Name:   genie_cisco.py
Description:   Implementation and logic for genie_cisco.
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 genie_cisco.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""
"""Genie adapter for the Cisco family: IOS, IOS-XE, IOS-XR and NX-OS.

All four platforms share Genie's OSPF schema::

    vrf -> address_family -> instance -> areas -> database -> lsa_types
        -> {1|2} -> lsas -> <lsa> -> ospfv2 -> body -> router.links / network

so one walker serves them all; only the Genie parser classes (and the CLI
commands, see ``src/vendors/__init__.py``) differ. Genie does all text parsing.
Only the default VRF is read.
"""


import importlib
import logging
from typing import Any

from src.vendors.base import (
    KIND_STUB,
    LinkRecord,
    Lsdb,
    LsdbAdapter,
    NetworkLsa,
    ParserError,
    RouterLsa,
    link_kind,
)

logger = logging.getLogger(__name__)

LSA_TYPE_ROUTER = 1
LSA_TYPE_NETWORK = 2
DEFAULT_VRF = "default"


class GenieCiscoAdapter(LsdbAdapter):
    """Parse Type 1/2 LSAs with a pair of Genie parser classes."""

    filters_by_process = True

    def __init__(self, module: str, router_cls: str, network_cls: str) -> None:
        """
        Args:
            module: Dotted path of the Genie module, e.g. ``genie.libs.parser.iosxe.show_ospf_database``.
            router_cls: Genie parser class for the router-LSA command.
            network_cls: Genie parser class for the network-LSA command.

        Raises:
            ParserError: If pyATS/Genie is not installed.
        """
        try:
            parsers = importlib.import_module(module)  # slow import, hence done lazily
            from genie.metaparser.util.exceptions import SchemaEmptyParserError
        except ImportError as exc:
            raise ParserError(
                "Cisco pyATS/Genie is not installed. Run: pip install 'pyats[library]'"
            ) from exc
        self._router_parser = getattr(parsers, router_cls)
        self._network_parser = getattr(parsers, network_cls)
        self._empty_exc = SchemaEmptyParserError

    # ------------------------------------------------------------------ Genie
    def _genie(
        self, parser_cls: type, raw: str, name: str, allow_empty: bool = False
    ) -> dict:
        """Run one Genie parser against raw text (no live device needed)."""
        if not raw or not raw.strip():
            if allow_empty:
                return {}
            raise ParserError(f"The {name} LSDB output is empty")
        try:
            return parser_cls(device=None).parse(output=raw)
        except self._empty_exc as exc:
            if allow_empty:
                logger.debug("Genie found no %s LSAs", name)
                return {}
            raise ParserError(f"Genie found no {name} LSAs in the output") from exc
        except Exception as exc:  # Genie raises assorted schema/regex errors
            raise ParserError(
                f"Genie failed to parse the {name} LSDB output: {exc!r}"
            ) from exc

    def parse(self, raw_router: str, raw_network: str, process_id: str | None) -> Lsdb:
        router_data = self._genie(self._router_parser, raw_router, "router")
        network_data = self._genie(
            self._network_parser, raw_network, "network", allow_empty=True
        )
        return self.from_genie(router_data, network_data, process_id)

    # ---------------------------------------------------------------- walking
    def from_genie(
        self, router_data: dict, network_data: dict, process_id: str | None
    ) -> Lsdb:
        """Flatten already-parsed Genie dictionaries (also used directly by the tests)."""
        lsdb = Lsdb(process_ids=self._instances(router_data))
        for area, lsa in self._iter_lsas(router_data, LSA_TYPE_ROUTER, process_id):
            lsdb.router_lsas.append(self._router_lsa(area, lsa))
        for area, lsa in self._iter_lsas(network_data, LSA_TYPE_NETWORK, process_id):
            body = ((lsa.get("ospfv2") or {}).get("body") or {}).get("network") or {}
            lsdb.network_lsas.append(
                NetworkLsa(
                    address=str(lsa.get("lsa_id")),
                    dr=str(lsa.get("adv_router")),
                    area=area,
                    mask=body.get("network_mask"),
                    attached=[str(r) for r in (body.get("attached_routers") or {})],
                )
            )
        return lsdb

    @staticmethod
    def _vrfs(parsed: dict) -> list[dict]:
        """Default VRF only; if Genie used another label, fall back to what is there."""
        vrfs = parsed.get("vrf") or {}
        if DEFAULT_VRF in vrfs:
            return [vrfs[DEFAULT_VRF]]
        if vrfs:
            logger.warning(
                "No '%s' VRF in Genie output; using: %s", DEFAULT_VRF, ", ".join(vrfs)
            )
        return list(vrfs.values())

    def _iter_lsas(self, parsed: dict, lsa_type: int, process_id: str | None):
        """Yield ``(area, lsa)`` for every LSA of ``lsa_type`` in the selected process."""
        for vrf in self._vrfs(parsed):
            for family in (vrf.get("address_family") or {}).values():
                for instance_id, instance in (family.get("instance") or {}).items():
                    if process_id is not None and str(instance_id) != process_id:
                        continue
                    for area, area_data in (instance.get("areas") or {}).items():
                        types = (area_data.get("database") or {}).get("lsa_types") or {}
                        block = types.get(lsa_type) or types.get(str(lsa_type)) or {}
                        for lsa in (block.get("lsas") or {}).values():
                            yield str(area), lsa

    def _instances(self, parsed: dict) -> list[str]:
        found: set[str] = set()
        for vrf in self._vrfs(parsed):
            for family in (vrf.get("address_family") or {}).values():
                found.update(str(i) for i in (family.get("instance") or {}))
        return sorted(found)

    @staticmethod
    def _metric(link: dict) -> int | None:
        """Base-topology (MT 0) metric of a router link."""
        topologies = link.get("topologies") or {}
        topology = topologies.get(0) or topologies.get("0")
        if topology is None and topologies:
            topology = next(iter(topologies.values()))
        try:
            return int((topology or {}).get("metric"))
        except (TypeError, ValueError):
            return None

    def _router_lsa(self, area: str, lsa: dict[str, Any]) -> RouterLsa:
        router = RouterLsa(
            router_id=str(lsa.get("adv_router") or lsa.get("lsa_id")), area=area
        )
        links = (((lsa.get("ospfv2") or {}).get("body") or {}).get("router") or {}).get(
            "links"
        ) or {}
        for link in links.values():
            kind = link_kind(str(link.get("type", "")))
            if kind is None:
                logger.debug(
                    "Router %s: ignoring unsupported link type %r",
                    router.router_id,
                    link.get("type"),
                )
                continue
            metric = None if kind == KIND_STUB else self._metric(link)
            link_data = (
                None
                if kind == KIND_STUB
                else (str(link.get("link_data")) if link.get("link_data") else None)
            )
            router.links.append(
                LinkRecord(
                    kind=kind,
                    link_id=str(link.get("link_id")),
                    metric=metric,
                    link_data=link_data,
                )
            )
        return router
