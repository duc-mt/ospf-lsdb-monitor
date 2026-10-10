"""
==============================================================================
Module Name:   cisco_style.py
Description:   Source module cisco_style.py.
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 cisco_style.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from src.vendors.base import (
    UNKNOWN_AREA,
    KIND_STUB,
    LinkRecord,
    Lsdb,
    LsdbAdapter,
    NeighborRecord,
    NetworkLsa,
    ParserError,
    RouterLsa,
    dotted_mask,
    link_kind,
    normalize_state,
    parse_seq,
)

logger = logging.getLogger(__name__)

_IP = r"\d+\.\d+\.\d+\.\d+"
_HEADER = re.compile(rf"OSPF Router with ID\s*\(({_IP})\)(.*)")
_PROCESS = re.compile(r"\((?:Process|Instance) ID\s+([^)\s]+)\)")
_VRF = re.compile(r"\(VRF\s+([^)\s]+)\)")
_AREA = re.compile(
    r"(?:Router|Net(?:work)?)\s+Link\s+States\s*\(Area\s+([\d.]+)\)", re.IGNORECASE
)
_LS_START = re.compile(r"^\s*LS\s+age\s*:", re.IGNORECASE)
_LS_AGE = re.compile(r"^\s*LS\s+age\s*:\s*(?:MAXAGE\()?(\d+)", re.IGNORECASE)
_ABR_LINE = re.compile(r"^\s*Area\s+Border\s+Router\s*$", re.IGNORECASE)
_ASBR_LINE = re.compile(r"^\s*AS\s+Boundary\s+Router\s*$", re.IGNORECASE)
_ROUTER_FLAGS = re.compile(r"^\s*Flags:\s*0x([0-9a-fA-F]+)", re.IGNORECASE)
_FRR_NEIGHBOR = re.compile(rf"^\s*({_IP})\s+(\d+)\s+(\S+)\s+\S+\s+\S+\s+({_IP})\s+(\S+)\s+\d+\s+\d+\s+\d+\s*$")
_LS_SEQ = re.compile(r"^\s*LS\s+Seq(?:uence)?\s+Number\s*:\s*(\S+)", re.IGNORECASE)
_LS_TYPE = re.compile(r"^\s*LS\s+Type\s*:\s*(.+?)\s*$", re.IGNORECASE)
_LS_ID = re.compile(rf"^\s*Link\s+State\s+ID\s*:\s*({_IP})", re.IGNORECASE)
_ADV = re.compile(rf"^\s*Advertising\s+Router\s*:\s*({_IP})", re.IGNORECASE)
_LINK = re.compile(
    r"^\s*Link\s+connected\s+to\s*:\s*(?:an?\s+)?(.+?)\s*$", re.IGNORECASE
)
_LINK_ID = re.compile(rf"^\s*\(Link\s+ID\)\s*[^:]*:\s*({_IP})", re.IGNORECASE)
_LINK_DATA = re.compile(rf"^\s*\(Link\s+Data\)\s*[^:]*:\s*({_IP})", re.IGNORECASE)
_METRIC = re.compile(r"^\s*TOS\s+0\s+Metrics?\s*:\s*(\d+)", re.IGNORECASE)
_MASK = re.compile(r"^\s*Network\s+Mask\s*:\s*(\S+)", re.IGNORECASE)
_ATTACHED = re.compile(rf"^\s*Attached\s+Router\s*:\s*({_IP})", re.IGNORECASE)

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
    seq: int | None = None
    age: int | None = None
    abr: bool = False
    asbr: bool = False
    mask: str | None = None
    attached: list[str] = field(default_factory=list)
    links: list[LinkRecord] = field(default_factory=list)


class CiscoStyleTextAdapter(LsdbAdapter):
    """Parse Cisco-style ``show ip ospf database router|network`` text."""

    filters_by_process = True

    def __init__(self, platform: str, metric_hint: str = "", neighbor_format: str | None = None) -> None:
        """
        Args:
            platform: Name used in error messages (e.g. ``"Arista EOS"``).
            metric_hint: Extra advice appended when the output has no link metrics.
            neighbor_format: ``"frr"`` or ``"arista"`` - which neighbour-table layout to parse (``None`` = none).
        """
        self.platform = platform
        self.metric_hint = metric_hint
        self.neighbor_format = neighbor_format

    # ------------------------------------------------------------------ parse
    def parse(self, raw_router: str, raw_network: str, process_id: str | None) -> Lsdb:
        lsdb = Lsdb()
        router_text, seen = self._scan(raw_router)
        network_text, seen_net = self._scan(raw_network)
        lsdb.process_ids = sorted(set(seen.processes) | set(seen_net.processes))
        lsdb.router_id = next((rid for rid, proc, vrf in seen.headers
                               if (vrf in (None, DEFAULT_VRF)) and (process_id is None or proc in (None, process_id))), None)

        for lsa in router_text:
            if self._wanted(lsa, process_id) and lsa.lsa_type.lower().startswith(
                "router"
            ):
                lsdb.router_lsas.append(
                    RouterLsa(
                        router_id=lsa.adv or lsa.lsa_id, area=lsa.area, links=lsa.links, seq=lsa.seq,
                        age=lsa.age, abr=lsa.abr, asbr=lsa.asbr
                    )
                )
        for lsa in network_text:
            if self._wanted(lsa, process_id) and lsa.lsa_type.lower().startswith(
                "network"
            ):
                lsdb.network_lsas.append(
                    NetworkLsa(
                        address=lsa.lsa_id,
                        dr=lsa.adv,
                        area=lsa.area,
                        mask=dotted_mask(lsa.mask),
                        attached=lsa.attached,
                        seq=lsa.seq,
                        age=lsa.age,
                    )
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

    # ------------------------------------------------------------- neighbours
    def parse_neighbors(self, raw: str, process_id: str | None) -> list[NeighborRecord] | None:
        if self.neighbor_format == "frr":
            return self._frr_neighbors(raw)
        if self.neighbor_format == "arista":
            return self._arista_neighbors(raw, process_id)
        return None

    @staticmethod
    def _frr_neighbors(raw: str) -> list[NeighborRecord]:
        """FRR/VyOS ``show ip ospf neighbor``: the Interface column is ``ifname:local-address``."""
        records = []
        for line in (raw or "").splitlines():
            m = _FRR_NEIGHBOR.match(line)
            if not m:
                continue
            rid, _pri, state_text, address, interface = m.groups()
            name, _, local = interface.rpartition(":")
            if not name or not re.fullmatch(_IP, local):  # no ":address" suffix
                name = interface
            state, role = normalize_state(state_text)
            records.append(NeighborRecord(router_id=rid, state=state, role=role, interface=name, address=address))
        return records

    @staticmethod
    def _arista_neighbors(raw: str, process_id: str | None) -> list[NeighborRecord]:
        """Arista ``show ip ospf neighbor``: ``ID Instance VRF Pri State Dead-Time Address Interface``
        (the Instance column is absent in some filtered views)."""
        records = []
        for line in (raw or "").splitlines():
            tokens = line.split()
            if len(tokens) not in (7, 8) or not re.fullmatch(_IP, tokens[0]):
                continue
            instance, vrf = (tokens[1], tokens[2]) if len(tokens) == 8 else (None, tokens[1])
            if vrf != DEFAULT_VRF or (process_id is not None and instance not in (None, process_id)):
                continue
            state, role = normalize_state(tokens[-4])
            records.append(NeighborRecord(router_id=tokens[0], state=state, role=role,
                                          interface=tokens[-1], address=tokens[-2]))
        return records

    # ---------------------------------------------------------- state machine
    @staticmethod
    def _scan(raw: str) -> tuple[list[_Lsa], _Seen]:
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
                process, vrf, area = (
                    (p.group(1) if p else None),
                    (v.group(1) if v else None),
                    UNKNOWN_AREA,
                )
                seen.headers.append((header.group(1), process, vrf))
                if process:
                    seen.processes.add(process)
                continue
            if (m := _AREA.search(line)) is not None:
                close()
                area = m.group(1)
                continue
            if _LS_START.match(line):
                close()
                age = _LS_AGE.match(line)
                cur = _Lsa(area=area, process=process, vrf=vrf, age=int(age.group(1)) if age else None)
                continue
            if cur is None:
                continue
            if m := _LS_TYPE.match(line):
                cur.lsa_type = m.group(1)
            elif m := _LS_ID.match(line):
                cur.lsa_id = m.group(1)
            elif m := _LS_SEQ.match(line):
                cur.seq = parse_seq(m.group(1))
            elif _ABR_LINE.match(line):
                cur.abr = True
            elif _ASBR_LINE.match(line):
                cur.asbr = True
            elif m := _ROUTER_FLAGS.match(line):
                bits = int(m.group(1), 16)  # RFC 2328 A.4.2: B = 0x01 (ABR), E = 0x02 (ASBR)
                cur.abr, cur.asbr = cur.abr or bool(bits & 0x01), cur.asbr or bool(bits & 0x02)
            elif m := _ADV.match(line):
                cur.adv = m.group(1)
            elif m := _LINK.match(line):
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
            elif m := _MASK.match(line):
                cur.mask = m.group(1)
            elif m := _ATTACHED.match(line):
                cur.attached.append(m.group(1))
        close()
        return lsas, seen


@dataclass
class _Seen:
    processes: set[str] = field(default_factory=set)
    #: ``(router ID, process, vrf)`` of every "OSPF Router with ID" banner, in order.
    headers: list[tuple[str, str | None, str | None]] = field(default_factory=list)
