"""
==============================================================================
Module Name:   genie_junos.py
Description:   Implementation and logic for genie_junos.
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 genie_junos.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""
"""Genie adapter for Juniper Junos.

Genie's Junos parser (``ShowOspfDatabaseExtensive``) returns the XML-style
``ospf-database-information`` schema and models **one area per run**. A router
in several areas prints several ``OSPF database, Area x`` blocks, so the output
is cut into one block per area first (a plain string split on that header, no LSA
parsing) and each block goes through Genie on its own.

Commands used: ``show ospf database router extensive`` and
``show ospf database network extensive``. Junos has no numeric OSPF process ID,
so ``process_id`` is not applicable here.
"""

from __future__ import annotations

import importlib
import logging
import re

from src.vendors.base import (
    KIND_STUB,
    LinkRecord,
    Lsdb,
    LsdbAdapter,
    NetworkLsa,
    ParserError,
    RouterLsa,
    as_list,
    dotted_mask,
    link_kind,
)

logger = logging.getLogger(__name__)

_AREA_BLOCK = re.compile(r"(?m)^(?=[ \t]*OSPF database, Area )")


def _clean(value: str | None) -> str:
    """Junos marks our own LSAs with a leading '*' on the ID."""
    return str(value or "").lstrip("*").strip()


class GenieJunosAdapter(LsdbAdapter):
    """Parse Junos router/network LSAs (Genie) into the neutral model."""

    filters_by_process = False

    def __init__(self) -> None:
        try:
            module = importlib.import_module("genie.libs.parser.junos.show_ospf")
            from genie.metaparser.util.exceptions import SchemaEmptyParserError
        except ImportError as exc:
            raise ParserError("Cisco pyATS/Genie is not installed. Run: pip install 'pyats[library]'") from exc
        self._parser = module.ShowOspfDatabaseExtensive
        self._empty_exc = SchemaEmptyParserError

    def parse(self, raw_router: str, raw_network: str, process_id: str | None) -> Lsdb:
        if process_id is not None:
            logger.warning("Junos has no numeric OSPF process ID; ignoring '%s'", process_id)
        lsdb = Lsdb()
        for area, lsas in self._lsas_by_area(raw_router, "router", required=True):
            lsdb.router_lsas.extend(self._router_lsa(area, d) for d in lsas if d.get("lsa-type") == "Router")
        for area, lsas in self._lsas_by_area(raw_network, "network", required=False):
            lsdb.network_lsas.extend(self._network_lsa(area, d) for d in lsas if d.get("lsa-type") == "Network")
        return lsdb

    # ------------------------------------------------------------------ Genie
    def _lsas_by_area(self, raw: str, name: str, required: bool) -> list[tuple[str, list[dict]]]:
        blocks = [b for b in _AREA_BLOCK.split(raw or "") if "OSPF database, Area" in b]
        if not blocks:
            if required:
                raise ParserError(f"No 'OSPF database, Area ...' section found in the {name} LSDB output")
            return []
        result = []
        for block in blocks:
            try:
                info = self._parser(device=None).parse(output=block)["ospf-database-information"]
            except self._empty_exc:
                continue
            except Exception as exc:
                raise ParserError(f"Genie failed to parse a Junos {name} LSDB block: {exc!r}") from exc
            area = str((info.get("ospf-area-header") or {}).get("ospf-area", "unknown"))
            result.append((area, as_list(info.get("ospf-database"))))
        return result

    # ---------------------------------------------------------------- mapping
    def _router_lsa(self, area: str, lsa: dict) -> RouterLsa:
        router = RouterLsa(router_id=_clean(lsa.get("advertising-router")), area=area)
        for link in as_list((lsa.get("ospf-router-lsa") or {}).get("ospf-link")):
            kind = link_kind(str(link.get("link-type-name", "")))
            if kind is None:
                continue
            try:
                metric = None if kind == KIND_STUB else int(link.get("metric"))
            except (TypeError, ValueError):
                metric = None
            link_data = None if kind == KIND_STUB else (_clean(link.get("link-data")) or None)
            router.links.append(
                LinkRecord(kind=kind, link_id=_clean(link.get("link-id")), metric=metric, link_data=link_data)
            )
        return router

    def _network_lsa(self, area: str, lsa: dict) -> NetworkLsa:
        body = lsa.get("ospf-network-lsa") or {}
        return NetworkLsa(
            address=_clean(lsa.get("lsa-id")),
            dr=_clean(lsa.get("advertising-router")),
            area=area,
            mask=dotted_mask(body.get("address-mask")),
            attached=[_clean(r) for r in as_list(body.get("attached-router"))],
        )
