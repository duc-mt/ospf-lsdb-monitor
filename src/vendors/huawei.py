"""
==============================================================================
Module Name:   huawei.py
Description:   Source module huawei.py.
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 huawei.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from src.vendors.base import (
    UNKNOWN_AREA,
    LinkRecord,
    Lsdb,
    LsdbAdapter,
    NeighborRecord,
    NetworkLsa,
    RouterLsa,
    dotted_mask,
    link_kind,
    normalize_state,
    parse_seq,
)

logger = logging.getLogger(__name__)

_IP = r"\d+\.\d+\.\d+\.\d+"
_PROCESS = re.compile(
    rf"OSPF\s+Process\s+(\S+)\s+with\s+Router\s+ID\s+({_IP})", re.IGNORECASE
)
_AREA = re.compile(rf"^\s*Area\s*:\s*({_IP}|\d+)\s*$", re.IGNORECASE)
_TYPE = re.compile(r"^\s*Type\s*:\s*(\S+)", re.IGNORECASE)
_LS_ID = re.compile(rf"^\s*Ls\s+id\s*:\s*({_IP})", re.IGNORECASE)
_AGE = re.compile(r"^\s*Ls\s+age\s*:\s*(\d+)", re.IGNORECASE)
_OPTIONS = re.compile(r"^\s*Options\s*:\s*(.*)$", re.IGNORECASE)
_PEER_ROW = re.compile(rf"^\s*({_IP})\s+(\S+)\s+({_IP})\s+(\S+)\s*$")  # area, interface, neighbor id, state
_SEQ = re.compile(r"^\s*seq#\s*:\s*(\S+)", re.IGNORECASE)
_ADV = re.compile(rf"Adv\s+rtr\s*:\s*({_IP})", re.IGNORECASE)
_LINK_ID = re.compile(rf"^\s*\*?\s*Link\s+ID\s*:\s*({_IP})", re.IGNORECASE)
_LINK_DATA = re.compile(rf"^\s*Data\s*:\s*({_IP})", re.IGNORECASE)
_LINK_TYPE = re.compile(r"^\s*Link\s+Type\s*:\s*(\S+)", re.IGNORECASE)
_METRIC = re.compile(r"^\s*Metric\s*:\s*(\d+)", re.IGNORECASE)
_MASK = re.compile(r"^\s*Net\s+mask\s*:\s*(\S+)", re.IGNORECASE)
_ATTACHED = re.compile(rf"^\s*Attached\s+Router\s*:?\s+({_IP})", re.IGNORECASE)

DEFAULT_AREA = "n/a"


@dataclass
class _Lsa:
    area: str
    process: str | None
    lsa_type: str
    lsa_id: str = ""
    adv: str = ""
    seq: int | None = None
    age: int | None = None
    abr: bool = False
    asbr: bool = False
    mask: str | None = None
    attached: list[str] = field(default_factory=list)
    links: list[LinkRecord] = field(default_factory=list)


class HuaweiAdapter(LsdbAdapter):
    """Parse Huawei VRP router/network LSA detail into the neutral model."""

    filters_by_process = True

    def parse(self, raw_router: str, raw_network: str, process_id: str | None) -> Lsdb:
        lsdb = Lsdb()
        routers, procs_r, own_r = self._scan(raw_router)
        networks, procs_n, _own_n = self._scan(raw_network)
        lsdb.process_ids = sorted(procs_r | procs_n)
        lsdb.router_id = next((rid for proc, rid in own_r if process_id is None or proc == process_id), None)

        for lsa in routers:
            if lsa.lsa_type.lower() == "router" and self._wanted(lsa, process_id):
                lsdb.router_lsas.append(
                    RouterLsa(
                        router_id=lsa.adv or lsa.lsa_id, area=lsa.area, links=lsa.links, seq=lsa.seq,
                        age=lsa.age, abr=lsa.abr, asbr=lsa.asbr
                    )
                )
        for lsa in networks:
            if lsa.lsa_type.lower() == "network" and self._wanted(lsa, process_id):
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
        return lsdb

    def parse_neighbors(self, raw: str, process_id: str | None) -> list[NeighborRecord]:
        """``display ospf peer brief``: ``Area Id  Interface  Neighbor id  State`` (no DR/BDR role is printed)."""
        records, process = [], None
        for line in (raw or "").splitlines():
            if (m := _PROCESS.search(line)):
                process = m.group(1)
                continue
            if (m := _PEER_ROW.match(line)) and (process_id is None or process in (None, process_id)):
                area, interface, router_id, state_text = m.groups()
                state, role = normalize_state(state_text)
                records.append(NeighborRecord(router_id=router_id, state=state, role=role,
                                              interface=interface, area=area))
        return records

    @staticmethod
    def _wanted(lsa: _Lsa, process_id: str | None) -> bool:
        return process_id is None or lsa.process is None or lsa.process == process_id

    @staticmethod
    def _scan(raw: str) -> tuple[list[_Lsa], set[str], list[tuple[str, str]]]:
        """Single pass over the text; returns all LSAs found and the process IDs seen."""
        lsas: list[_Lsa] = []
        processes: set[str] = set()
        own_ids: list[tuple[str, str]] = []  # (process, router ID) from the banner lines
        process: str | None = None
        area = DEFAULT_AREA
        cur: _Lsa | None = None
        link: LinkRecord | None = None

        for line in (raw or "").splitlines():
            if m := _PROCESS.search(line):
                process = m.group(1)
                processes.add(process)
                own_ids.append((process, m.group(2)))
                cur, link = None, None
                continue
            if m := _AREA.match(line):
                area, cur, link = m.group(1), None, None
                continue
            if m := _TYPE.match(line):
                cur = _Lsa(area=area, process=process, lsa_type=m.group(1))
                lsas.append(cur)
                link = None
                continue
            if cur is None:
                continue
            if m := _LS_ID.match(line):
                cur.lsa_id = m.group(1)
                # 'Adv rtr' is sometimes glued onto the same line in copy-pasted output
            if m := _ADV.search(line):
                cur.adv = m.group(1)
            if (m := _SEQ.match(line)):
                cur.seq = parse_seq(m.group(1))
            if (m := _AGE.match(line)):
                cur.age = int(m.group(1))
            if (m := _OPTIONS.match(line)) and cur.lsa_type.lower() == "router":
                words = set(m.group(1).upper().split())
                cur.abr, cur.asbr = "ABR" in words, "ASBR" in words
            if cur.lsa_type.lower() == "router":
                if m := _LINK_ID.match(line):
                    link = LinkRecord(kind="", link_id=m.group(1), metric=None)
                    cur.links.append(link)
                elif link is not None and (m := _LINK_DATA.match(line)):
                    link.link_data = m.group(
                        1
                    )  # mask for stub links; only used for non-stub
                elif link is not None and (m := _LINK_TYPE.match(line)):
                    link.kind = link_kind(m.group(1)) or ""
                elif link is not None and (m := _METRIC.match(line)):
                    link.metric = int(m.group(1))
            elif cur.lsa_type.lower() == "network":
                if m := _MASK.match(line):
                    cur.mask = m.group(1)
                elif m := _ATTACHED.match(line):
                    cur.attached.append(m.group(1))

        # 'Link Type' follows 'Link ID', so a link's kind is only known after the fact;
        # drop any link whose type was missing or unrecognized.
        for lsa in lsas:
            lsa.links = [link for link in lsa.links if link.kind]
        return lsas, processes, own_ids
