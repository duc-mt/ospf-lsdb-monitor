"""
==============================================================================
Module Name:   cisco_style.py
Description:   Implementation and logic for cisco_style.
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 cisco_style.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""
"""Text adapter for platforms that print Cisco-style LSDB detail: FRR (VyOS) and Arista EOS.

Genie has no OSPF parsers for these platforms, which is the case the "no custom
regex unless Genie lacks the parser" rule allows. The format is a line-oriented
``Key: value`` layout, so a small state machine over a handful of anchored
patterns is enough::

    OSPF Router with ID(10.0.0.1) (Instance ID 100) (VRF default)   <- Arista header
    OSPF Router with ID (192.0.2.1)                                  <- FRR header
        Router Link States (Area 0.0.0.0)
      LS Type: Router Links | router-LSA
      Link State ID / Advertising Router
        Link connected to: a Transit Network | Transit Network
         (Link ID) Designated Router address: 10.1.1.1
         TOS 0 Metrics: 10 | TOS 0 Metric: 10
      Network Mask: /24 | 255.255.255.0
        Attached Router: 10.0.0.1

Patterns are deliberately tolerant of the small wording differences between the
two (``a Stub Network`` vs ``Stub Network``, ``Metrics`` vs ``Metric``, ...).
Only the default VRF is read.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from src.vendors.base import (
    KIND_STUB,
    LinkRecord,
    Lsdb,
    LsdbAdapter,
    NetworkLsa,
    ParserError,
    RouterLsa,
    dotted_mask,
    link_kind,
)

logger = logging.getLogger(__name__)

_IP = r"\d+\.\d+\.\d+\.\d+"
_HEADER = re.compile(rf"OSPF Router with ID\s*\(({_IP})\)(.*)")
_PROCESS = re.compile(r"\((?:Process|Instance) ID\s+([^)\s]+)\)")
_VRF = re.compile(r"\(VRF\s+([^)\s]+)\)")
_AREA = re.compile(r"(?:Router|Net(?:work)?)\s+Link\s+States\s*\(Area\s+([\d.]+)\)", re.I)
_LS_START = re.compile(r"^\s*LS\s+age\s*:", re.I)
_LS_TYPE = re.compile(r"^\s*LS\s+Type\s*:\s*(.+?)\s*$", re.I)
_LS_ID = re.compile(rf"^\s*Link\s+State\s+ID\s*:\s*({_IP})", re.I)
_ADV = re.compile(rf"^\s*Advertising\s+Router\s*:\s*({_IP})", re.I)
_LINK = re.compile(r"^\s*Link\s+connected\s+to\s*:\s*(?:an?\s+)?(.+?)\s*$", re.I)
_LINK_ID = re.compile(rf"^\s*\(Link\s+ID\)\s*[^:]*:\s*({_IP})", re.I)
_LINK_DATA = re.compile(rf"^\s*\(Link\s+Data\)\s*[^:]*:\s*({_IP})", re.I)
_METRIC = re.compile(r"^\s*TOS\s+0\s+Metrics?\s*:\s*(\d+)", re.I)
_MASK = re.compile(r"^\s*Network\s+Mask\s*:\s*(\S+)", re.I)
_ATTACHED = re.compile(rf"^\s*Attached\s+Router\s*:\s*({_IP})", re.I)

DEFAULT_VRF = "default"
UNKNOWN_AREA = "n/a"  # Arista's per-LSA detail view has no area banner


@dataclass
class _Lsa:
    area: str
    process: str | None
    vrf: str | None
    lsa_type: str = ""
    lsa_id: str = ""
    adv: str = ""
    mask: str | None = None
    attached: list[str] = field(default_factory=list)
    links: list[LinkRecord] = field(default_factory=list)


class CiscoStyleTextAdapter(LsdbAdapter):
    """Parse Cisco-style ``show ip ospf database router|network`` text."""

    filters_by_process = True

    def __init__(self, platform: str, metric_hint: str = "") -> None:
        """
        Args:
            platform: Name used in error messages (e.g. ``"Arista EOS"``).
            metric_hint: Extra advice appended when the output has no link metrics.
        """
        self.platform = platform
        self.metric_hint = metric_hint

    # ------------------------------------------------------------------ parse
    def parse(self, raw_router: str, raw_network: str, process_id: str | None) -> Lsdb:
        lsdb = Lsdb()
        router_text, seen = self._scan(raw_router)
        network_text, seen_net = self._scan(raw_network)
        lsdb.process_ids = sorted(set(seen.processes) | set(seen_net.processes))

        for lsa in router_text:
            if self._wanted(lsa, process_id) and lsa.lsa_type.lower().startswith("router"):
                lsdb.router_lsas.append(RouterLsa(router_id=lsa.adv or lsa.lsa_id, area=lsa.area, links=lsa.links))
        for lsa in network_text:
            if self._wanted(lsa, process_id) and lsa.lsa_type.lower().startswith("network"):
                lsdb.network_lsas.append(
                    NetworkLsa(address=lsa.lsa_id, dr=lsa.adv, area=lsa.area, mask=dotted_mask(lsa.mask),
                               attached=lsa.attached)
                )
        self._require_metrics(lsdb)
        return lsdb

    @staticmethod
    def _wanted(lsa: _Lsa, process_id: str | None) -> bool:
        if lsa.vrf is not None and lsa.vrf != DEFAULT_VRF:
            return False
        return process_id is None or lsa.process is None or lsa.process == process_id

    def _require_metrics(self, lsdb: Lsdb) -> None:
        """Fail loudly if links exist but none carries a metric (summary view instead of detail)."""
        links = [l for r in lsdb.router_lsas for l in r.links if l.kind != KIND_STUB]
        if links and all(l.metric is None for l in links):
            raise ParserError(
                f"{self.platform}: router LSAs were found but none has a 'TOS 0 Metric' line, so link "
                f"costs are unknown. {self.metric_hint}".strip()
            )

    # ---------------------------------------------------------- state machine
    @staticmethod
    def _scan(raw: str) -> tuple[list[_Lsa], "_Seen"]:
        """Walk the text once and return every LSA found, plus the process IDs seen."""
        seen = _Seen()
        lsas: list[_Lsa] = []
        process: str | None = None
        vrf: str | None = None
        area = UNKNOWN_AREA
        cur: _Lsa | None = None
        link: LinkRecord | None = None

        def close() -> None:
            nonlocal cur, link
            if cur is not None and cur.lsa_type:
                lsas.append(cur)
            cur, link = None, None

        for line in (raw or "").splitlines():
            header = _HEADER.search(line)
            if header:
                close()
                rest = header.group(2)
                p, v = _PROCESS.search(rest), _VRF.search(rest)
                process, vrf, area = (p.group(1) if p else None), (v.group(1) if v else None), UNKNOWN_AREA
                if process:
                    seen.processes.add(process)
                continue
            if (m := _AREA.search(line)) is not None:
                close()
                area = m.group(1)
                continue
            if _LS_START.match(line):
                close()
                cur = _Lsa(area=area, process=process, vrf=vrf)
                continue
            if cur is None:
                continue
            if (m := _LS_TYPE.match(line)):
                cur.lsa_type = m.group(1)
            elif (m := _LS_ID.match(line)):
                cur.lsa_id = m.group(1)
            elif (m := _ADV.match(line)):
                cur.adv = m.group(1)
            elif (m := _LINK.match(line)):
                kind = link_kind(m.group(1))
                link = LinkRecord(kind=kind, link_id="", metric=None) if kind else None
                if link:
                    cur.links.append(link)
            elif link is not None and (m := _LINK_ID.match(line)):
                link.link_id = m.group(1)
            elif link is not None and (m := _LINK_DATA.match(line)):
                if link.kind != KIND_STUB:  # for stubs this field is the network mask
                    link.link_data = m.group(1)
            elif link is not None and (m := _METRIC.match(line)):
                link.metric = int(m.group(1))
            elif (m := _MASK.match(line)):
                cur.mask = m.group(1)
            elif (m := _ATTACHED.match(line)):
                cur.attached.append(m.group(1))
        close()
        return lsas, seen


@dataclass
class _Seen:
    processes: set[str] = field(default_factory=set)
