"""Vendor-neutral building blocks shared by every platform adapter.

Each platform adapter turns that platform's raw ``show`` output into the small
intermediate model below (``Lsdb``). Everything downstream - the node/edge
builder in ``src/parser.py``, the graph engine, the visualizer - only ever sees
this model, so adding a platform never touches them.
"""

from __future__ import annotations

import ipaddress
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from src import TrackerError

# Link kinds used by the intermediate model.
KIND_P2P = "p2p"
KIND_TRANSIT = "transit"
KIND_STUB = "stub"
KIND_VIRTUAL = "virtual"


class ParserError(TrackerError):
    """Raised when LSDB text cannot be parsed into a usable topology."""


@dataclass
class LinkRecord:
    """One link inside a Type 1 (router) LSA.

    ``link_id`` is the neighbor router-ID for p2p/virtual links and the DR's
    interface address for transit links (same meaning on every platform).
    ``link_data`` is the advertising router's own interface address on that link
    (the "Link Data" field; an ifIndex on unnumbered links). It is what lets the
    diagram label each link end with something a person can find on the box.
    """

    kind: str
    link_id: str
    metric: int | None
    link_data: str | None = None


@dataclass
class RouterLsa:
    """Type 1 LSA: a router and the links it advertises."""

    router_id: str
    area: str
    links: list[LinkRecord] = field(default_factory=list)


@dataclass
class NetworkLsa:
    """Type 2 LSA: a transit network, originated by its Designated Router."""

    address: str  # DR interface address (the LSA's Link State ID)
    dr: str  # DR router-ID (the LSA's advertising router)
    area: str
    mask: str | None
    attached: list[str] = field(default_factory=list)


@dataclass
class Lsdb:
    """Everything an adapter extracted from one poll."""

    router_lsas: list[RouterLsa] = field(default_factory=list)
    network_lsas: list[NetworkLsa] = field(default_factory=list)
    #: Every OSPF process/instance seen in the output, *before* any filtering.
    process_ids: list[str] = field(default_factory=list)


class LsdbAdapter(ABC):
    """Turns one platform's raw router/network LSDB text into an ``Lsdb``."""

    #: True if ``process_id`` can be honored when parsing (header or schema carries it).
    filters_by_process: bool = False

    @abstractmethod
    def parse(self, raw_router: str, raw_network: str, process_id: str | None) -> Lsdb:
        """Parse raw output. ``process_id`` (if given) selects one OSPF process."""


# --------------------------------------------------------------------- helpers
def link_kind(text: str) -> str | None:
    """Map a platform's link-type wording to a kind (``None`` if unrecognized).

    Handles e.g. Cisco "another Router (point-to-point)", Junos "PointToPoint",
    Huawei "P-2-P" / "TransNet" / "StubNet", FRR "Stub Network".
    """
    t = text.lower().replace("-", "").replace(" ", "")
    if "virtual" in t:
        return KIND_VIRTUAL
    if "pointtopoint" in t or "p2p" in t or "point" in t:
        return KIND_P2P
    if "transit" in t or "transnet" in t:
        return KIND_TRANSIT
    if "stub" in t:
        return KIND_STUB
    return None


def dotted_mask(mask: str | None) -> str | None:
    """Normalize ``/24``, ``24`` or ``255.255.255.0`` to dotted-quad form."""
    if not mask:
        return None
    text = str(mask).strip()
    try:
        if text.startswith("/") or text.isdigit():
            return str(ipaddress.ip_network(f"0.0.0.0/{text.lstrip('/')}").netmask)
        ipaddress.ip_address(text)
        return text
    except ValueError:
        return None


def as_list(value) -> list:
    """Genie/XML-style fields are a dict/str when single and a list when repeated."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
