from __future__ import annotations

"""
==============================================================================
Module Name:   test_vendors.py
Description:   Vendor parser tests.  Every raw-text vendor sample under ``samples/<device_type>/`` describes the *same* small topology in that vendor's own CLI layout, so one expectation covers all of them. Cisco IOS-XR and NX-OS are exercised through Genie's own expected-output fixtures (the raw device text is not shipped with Genie).
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 test_vendors.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""


import importlib.util
from pathlib import Path

import pytest

from src.config import ConfigError, parse_process_id
from src.parser import OSPFParser
from src.vendors import UnsupportedVendorError, get_profile, supported_device_types
from src.vendors.base import ParserError, dotted_mask, link_kind

SAMPLES = Path(__file__).resolve().parents[1] / "samples"

EXPECTED_NODES = {"10.0.0.1", "10.0.0.2", "10.0.0.3", "net-192.168.1.1"}
EXPECTED_EDGES = {
    ("10.0.0.1", "10.0.0.2", 10, "point-to-point"),
    ("10.0.0.2", "10.0.0.1", 20, "point-to-point"),  # asymmetric cost
    ("10.0.0.1", "net-192.168.1.1", 1, "transit"),
    ("10.0.0.3", "net-192.168.1.1", 5, "transit"),
    ("net-192.168.1.1", "10.0.0.1", 0, "attachment"),
    ("net-192.168.1.1", "10.0.0.3", 0, "attachment"),
}
VENDORS = ["cisco_ios", "juniper_junos", "arista_eos", "vyos", "huawei"]


def load(vendor: str) -> tuple[str, str]:
    d = SAMPLES / vendor
    return (d / "router_lsdb.txt").read_text(), (d / "network_lsdb.txt").read_text()


@pytest.mark.parametrize("vendor", VENDORS)
def test_vendor_sample_yields_canonical_topology(vendor):
    parsed = OSPFParser(vendor).parse(*load(vendor))
    assert {n["id"] for n in parsed["nodes"]} == EXPECTED_NODES
    assert {
        (e["source"], e["target"], e["metric"], e["link_type"]) for e in parsed["edges"]
    } == EXPECTED_EDGES
    net = next(n for n in parsed["nodes"] if n["type"] == "network")
    assert (net["prefix"], net["dr"], net["mask"]) == (
        "192.168.1.0/24",
        "10.0.0.1",
        "255.255.255.0",
    )
    assert all(
        n["resolved"] for n in parsed["nodes"]
    )  # stubs are ignored, nothing dangling


@pytest.mark.parametrize("vendor", ["cisco_ios", "huawei"])
def test_process_filter_matches_and_rejects(vendor):
    router, network = load(vendor)
    assert OSPFParser(vendor, process_id=1).parse(router, network)["nodes"]
    with pytest.raises(ParserError, match="process 99"):
        OSPFParser(vendor, process_id=99).parse(router, network)


def test_arista_instance_id_is_the_process_id():
    router, network = load("arista_eos")
    assert OSPFParser("arista_eos", process_id=1).parse(router, network)["nodes"]
    with pytest.raises(ParserError):
        OSPFParser("arista_eos", process_id=2).parse(router, network)


def test_arista_non_default_vrf_is_skipped():
    router, network = load("arista_eos")
    other_vrf = router.replace("(VRF default)", "(VRF red)")
    with pytest.raises(ParserError, match="No router"):
        OSPFParser("arista_eos").parse(other_vrf, network)


def test_arista_summary_view_fails_loudly_with_hint():
    """If the router command returns links without metrics, say so instead of guessing costs."""
    router, network = load("arista_eos")
    stripped = "\n".join(l for l in router.splitlines() if "TOS 0" not in l)
    with pytest.raises(ParserError, match="detail"):
        OSPFParser("arista_eos").parse(stripped, network)


def test_junos_multi_area_is_split_per_area():
    router, network = load("juniper_junos")
    # An ABR prints one block per area; give R3's LSA its own area 0.0.0.1 block.
    head = router.split("Router  *10.0.0.1")[0]
    r1, r2, r3 = ("Router  *" + part for part in router.split("Router  *")[1:])
    two_areas = head + r1 + r2 + head + r3
    parsed = OSPFParser("juniper_junos").parse(
        two_areas.replace(head + r3, head.replace("0.0.0.0", "0.0.0.1") + r3), network
    )
    areas = {n["id"]: n["areas"] for n in parsed["nodes"]}
    assert areas["10.0.0.1"] == ["0.0.0.0"]
    assert "0.0.0.1" in areas["10.0.0.3"]


def test_no_network_lsas_is_fine():
    router, _ = load("huawei")
    parsed = OSPFParser("huawei").parse(router, "")
    assert not any(e["link_type"] == "attachment" for e in parsed["edges"])
    # The transit network exists only as a dangling reference (no Type 2 LSA in the database).
    net = next(n for n in parsed["nodes"] if n["type"] == "network")
    assert net["resolved"] is False


# ------------------------------------------------- IOS-XR / NX-OS via Genie fixtures
def _genie_fixture(relative: str) -> dict:
    genie = pytest.importorskip("genie.libs.parser")
    path = Path(genie.__file__).parent / relative
    if not path.exists():
        pytest.skip(f"Genie fixture not shipped: {relative}")
    spec = importlib.util.spec_from_file_location("fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.expected_output


def _count_default_vrf(data: dict, lsa_type: int) -> int:
    total = 0
    for inst in data["vrf"]["default"]["address_family"]["ipv4"]["instance"].values():
        for area in inst["areas"].values():
            total += len(area["database"]["lsa_types"][lsa_type]["lsas"])
    return total


@pytest.mark.parametrize(
    "device_type, router_fx, network_fx",
    [
        (
            "cisco_xr",
            "iosxr/tests/ShowOspfVrfAllInclusiveDatabaseRouter/cli/equal/golden_output_1_expected.py",
            "iosxr/tests/ShowOspfVrfAllInclusiveDatabaseNetwork/cli/equal/golden_output_1_expected.py",
        ),
        (
            "cisco_nxos",
            "nxos/tests/ShowIpOspfDatabaseRouterDetail/cli/equal/golden_output_1_expected.py",
            "nxos/tests/ShowIpOspfDatabaseNetworkDetail/cli/equal/golden_output_expected.py",
        ),
    ],
)
def test_xr_and_nxos_genie_schema_is_walked(device_type, router_fx, network_fx):
    router, network = _genie_fixture(router_fx), _genie_fixture(network_fx)
    adapter = OSPFParser(device_type).adapter
    lsdb = adapter.from_genie(router, network, None)
    # Default VRF only: VRF1's LSAs in the fixture must not leak in.
    assert len(lsdb.router_lsas) == _count_default_vrf(router, 1) > 0
    assert len(lsdb.network_lsas) == _count_default_vrf(network, 2) > 0
    # (The fixtures keep their point-to-point links in VRF1, which is correctly skipped.)
    kinds = {l.kind for r in lsdb.router_lsas for l in r.links}
    assert {"transit", "stub"} <= kinds
    assert all(
        isinstance(l.metric, int)
        for r in lsdb.router_lsas
        for l in r.links
        if l.kind != "stub"
    )
    assert all(n.attached for n in lsdb.network_lsas)


# ------------------------------------------------------------ registry / helpers
def test_registry_commands():
    ios = get_profile("cisco_ios")
    assert ios.command("router", None) == "show ip ospf database router"
    assert ios.command("router", "7") == "show ip ospf 7 database router"
    assert (
        get_profile("huawei").command("network", "1") == "display ospf 1 lsdb network"
    )
    assert (
        get_profile("juniper_junos").command("router", "1")
        == "show ospf database router extensive"
    )  # no pid form
    assert (
        ios.command("router", "1", "show ip ospf {pid} database router self-originate")
        == "show ip ospf 1 database router self-originate"
    )
    with pytest.raises(ConfigError):
        ios.command("router", None, "show ip ospf {pid} database router")


def test_unsupported_vendor_messages():
    with pytest.raises(UnsupportedVendorError, match="not supported yet"):
        get_profile("mikrotik_routeros")
    with pytest.raises(UnsupportedVendorError, match="Supported:"):
        get_profile("no_such_vendor")
    assert {"cisco_ios", "juniper_junos", "arista_eos", "vyos", "huawei"} <= set(
        supported_device_types()
    )


@pytest.mark.parametrize(
    "value, expected", [(1, "1"), ("mpls1", "mpls1"), (None, None), ("", None)]
)
def test_process_id_accepts_numbers_and_names(value, expected):
    assert parse_process_id(value) == expected


@pytest.mark.parametrize("bad", ["1; reload", "1 | include x", "$(id)", "a" * 65])
def test_process_id_rejects_command_injection(bad):
    with pytest.raises(ConfigError):
        parse_process_id(bad)


def test_helpers():
    assert (
        dotted_mask("/24") == "255.255.255.0"
        and dotted_mask("255.255.0.0") == "255.255.0.0"
    )
    assert dotted_mask("garbage") is None and dotted_mask(None) is None
    assert link_kind("another Router (point-to-point)") == "p2p"  # IOS / IOS-XR wording
    assert link_kind("router (point-to-point)") == "p2p"  # NX-OS wording
    assert link_kind("P-2-P") == "p2p" and link_kind("TransNet") == "transit"
    assert (
        link_kind("a Stub Network") == "stub" and link_kind("Virtual Link") == "virtual"
    )
    assert link_kind("something else") is None


# --------------------------------------- every platform reports the interface address
EXPECTED_INTERFACES = {
    ("10.0.0.1", "10.0.0.2"): "10.12.0.1",
    ("10.0.0.2", "10.0.0.1"): "10.12.0.2",
    ("10.0.0.1", "net-192.168.1.1"): "192.168.1.1",
    ("10.0.0.3", "net-192.168.1.1"): "192.168.1.3",
}


@pytest.mark.parametrize("vendor", VENDORS)
def test_vendor_sample_reports_interface_addresses(vendor):
    """'Link Data' (the router's own interface address) survives every adapter, stubs excluded."""
    parsed = OSPFParser(vendor).parse(*load(vendor))
    got = {
        (e["source"], e["target"]): e.get("interface_address")
        for e in parsed["edges"]
        if e["link_type"] != "attachment"
    }
    assert got == EXPECTED_INTERFACES
