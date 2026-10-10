from __future__ import annotations

"""
==============================================================================
Module Name:   test_parser.py
Description:   Implementation and logic for test_parser.
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 test_parser.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""
"""Unit tests for OSPFParser internals.

These tests bypass the vendor adapter and call ``_build()`` directly with
crafted ``Lsdb`` objects, so they run without Genie or any network device.
"""


import pytest

from src.parser import OSPFParser
from src.vendors.base import (
    KIND_P2P,
    KIND_STUB,
    KIND_TRANSIT,
    Lsdb,
    LinkRecord,
    NetworkLsa,
    RouterLsa,
)


def _parser() -> OSPFParser:
    return OSPFParser("cisco_ios")


def _simple_lsdb() -> Lsdb:
    """2 routers + 1 transit network — the canonical test topology."""
    return Lsdb(
        process_ids=["1"],
        router_lsas=[
            RouterLsa(
                router_id="10.0.0.1",
                area="0.0.0.0",
                links=[
                    LinkRecord(kind=KIND_P2P, link_id="10.0.0.2", metric=10),
                    LinkRecord(kind=KIND_TRANSIT, link_id="192.168.1.1", metric=1),
                    LinkRecord(
                        kind=KIND_STUB, link_id="10.0.0.100", metric=5
                    ),  # must be ignored
                ],
            ),
            RouterLsa(
                router_id="10.0.0.2",
                area="0.0.0.0",
                links=[
                    LinkRecord(kind=KIND_P2P, link_id="10.0.0.1", metric=20),
                ],
            ),
        ],
        network_lsas=[
            NetworkLsa(
                address="192.168.1.1",
                dr="10.0.0.1",
                area="0.0.0.0",
                mask="255.255.255.0",
                attached=["10.0.0.1", "10.0.0.2"],
            ),
        ],
    )


# ----------------------------------------------------------------- stub links
def test_stub_links_are_not_graphed():
    parsed = _parser()._build(_simple_lsdb())
    node_ids = {n["id"] for n in parsed["nodes"]}
    edge_targets = {e["target"] for e in parsed["edges"]}
    assert "10.0.0.100" not in node_ids
    assert "10.0.0.100" not in edge_targets


# --------------------------------------------------------- parallel edge logic
def test_parallel_edge_keeps_lower_metric():
    lsdb = Lsdb(
        router_lsas=[
            RouterLsa(
                router_id="10.0.0.1",
                area="0.0.0.0",
                links=[
                    LinkRecord(kind=KIND_P2P, link_id="10.0.0.2", metric=100),
                    LinkRecord(
                        kind=KIND_P2P, link_id="10.0.0.2", metric=10
                    ),  # lower — must win
                ],
            ),
        ]
    )
    parsed = _parser()._build(lsdb)
    edge_metrics = {(e["source"], e["target"]): e["metric"] for e in parsed["edges"]}
    assert edge_metrics[("10.0.0.1", "10.0.0.2")] == 10


def test_parallel_edge_equal_metric_is_idempotent():
    lsdb = Lsdb(
        router_lsas=[
            RouterLsa(
                router_id="10.0.0.1",
                area="0.0.0.0",
                links=[
                    LinkRecord(kind=KIND_P2P, link_id="10.0.0.2", metric=10),
                    LinkRecord(kind=KIND_P2P, link_id="10.0.0.2", metric=10),
                ],
            ),
        ]
    )
    parsed = _parser()._build(lsdb)
    edges = [(e["source"], e["target"]) for e in parsed["edges"]]
    assert edges.count(("10.0.0.1", "10.0.0.2")) == 1  # no duplicate


# ---------------------------------------------------------- None metric guard
def test_none_metric_link_is_skipped_not_raised():
    """A None metric must be silently dropped — _add_edge must not raise TypeError."""
    lsdb = Lsdb(
        router_lsas=[
            RouterLsa(
                router_id="10.0.0.1",
                area="0.0.0.0",
                links=[
                    LinkRecord(kind=KIND_P2P, link_id="10.0.0.2", metric=None),
                    LinkRecord(kind=KIND_P2P, link_id="10.0.0.3", metric=5),
                ],
            ),
        ]
    )
    parsed = _parser()._build(lsdb)
    edge_pairs = {(e["source"], e["target"]) for e in parsed["edges"]}
    assert ("10.0.0.1", "10.0.0.2") not in edge_pairs  # None metric skipped
    assert ("10.0.0.1", "10.0.0.3") in edge_pairs  # valid edge present


# ------------------------------------------------------------- node upsert
def test_upsert_node_merges_areas():
    """A router seen in two areas accumulates both."""
    lsdb = Lsdb(
        router_lsas=[
            RouterLsa(
                "10.0.0.1",
                area="0.0.0.0",
                links=[
                    LinkRecord(KIND_P2P, "10.0.0.2", 10),
                ],
            ),
            RouterLsa(
                "10.0.0.1",
                area="0.0.0.1",
                links=[
                    LinkRecord(KIND_P2P, "10.0.0.3", 10),
                ],
            ),
        ]
    )
    parsed = _parser()._build(lsdb)
    r1 = next(n for n in parsed["nodes"] if n["id"] == "10.0.0.1")
    assert set(r1["areas"]) == {"0.0.0.0", "0.0.0.1"}


def test_resolved_flag_set_for_lsa_owner():
    parsed = _parser()._build(_simple_lsdb())
    by_id = {n["id"]: n for n in parsed["nodes"]}
    assert by_id["10.0.0.1"]["resolved"] is True
    assert by_id["10.0.0.2"]["resolved"] is True


# ----------------------------------------------------------- metadata fields
def test_metadata_fields_are_populated():
    parsed = _parser()._build(_simple_lsdb())
    meta = parsed["metadata"]
    assert meta["device_type"] == "cisco_ios"
    assert meta["router_lsas"] == 2
    assert meta["network_lsas"] == 1
    assert "0.0.0.0" in meta["areas"]
    assert "parsed_at" in meta


# --------------------------------------------------------------- transit net
def test_transit_network_node_created_from_type2_lsa():
    parsed = _parser()._build(_simple_lsdb())
    net = next(n for n in parsed["nodes"] if n["type"] == "network")
    assert net["id"] == "net-192.168.1.1"
    assert net["prefix"] == "192.168.1.0/24"
    assert net["dr"] == "10.0.0.1"
    assert net["resolved"] is True


def test_attachment_edges_have_zero_metric():
    parsed = _parser()._build(_simple_lsdb())
    attachments = [e for e in parsed["edges"] if e["link_type"] == "attachment"]
    assert attachments
    assert all(e["metric"] == 0 for e in attachments)


# ------------------------------------------------- interface addresses on edges
def test_link_data_becomes_interface_address_on_the_edge():
    lsdb = Lsdb(
        router_lsas=[
            RouterLsa(
                "10.0.0.1",
                area="0.0.0.0",
                links=[
                    LinkRecord(KIND_P2P, "10.0.0.2", 10, link_data="10.12.0.1"),
                    LinkRecord(KIND_TRANSIT, "192.168.1.1", 1, link_data="192.168.1.1"),
                    LinkRecord(KIND_P2P, "10.0.0.9", 5),  # platform gave no address
                ],
            ),
        ],
        network_lsas=[
            NetworkLsa(
                "192.168.1.1",
                dr="10.0.0.1",
                area="0.0.0.0",
                mask="/24",
                attached=["10.0.0.1"],
            )
        ],
    )
    edges = {(e["source"], e["target"]): e for e in _parser()._build(lsdb)["edges"]}
    assert edges[("10.0.0.1", "10.0.0.2")]["interface_address"] == "10.12.0.1"
    assert edges[("10.0.0.1", "net-192.168.1.1")]["interface_address"] == "192.168.1.1"
    assert "interface_address" not in edges[("10.0.0.1", "10.0.0.9")]
    # attachments are described by the network LSA, not by a router interface
    assert "interface_address" not in edges[("net-192.168.1.1", "10.0.0.1")]


def test_lower_metric_parallel_link_brings_its_own_interface_address():
    lsdb = Lsdb(
        router_lsas=[
            RouterLsa(
                "10.0.0.1",
                area="0.0.0.0",
                links=[
                    LinkRecord(KIND_P2P, "10.0.0.2", 100, link_data="10.1.0.1"),
                    LinkRecord(
                        KIND_P2P, "10.0.0.2", 10, link_data="10.2.0.1"
                    ),  # cheaper link wins, address included
                ],
            )
        ]
    )
    (edge,) = _parser()._build(lsdb)["edges"]
    assert (edge["metric"], edge["interface_address"]) == (10, "10.2.0.1")
