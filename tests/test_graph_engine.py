
"""
==============================================================================
Module Name:   test_graph_engine.py
Description:   Graph engine tests: diffing, state rotation and the partial-LSDB guard.
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 test_graph_engine.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""

from __future__ import annotations


import json
from copy import deepcopy

import pytest

from src.graph_engine import GraphEngine, GuardConfig


def topology(extra_routers: int = 0) -> dict:
    """A ring of routers r1..rN plus one transit network; every node stays connected."""
    ids = [f"10.0.0.{i}" for i in range(1, 6 + extra_routers)]
    nodes = [
        {"id": r, "type": "router", "areas": ["0.0.0.0"], "resolved": True} for r in ids
    ]
    edges = []
    for a, b in zip(ids, ids[1:] + ids[:1]):
        edges.append(
            {
                "source": a,
                "target": b,
                "metric": 10,
                "link_type": "point-to-point",
                "area": "0.0.0.0",
            }
        )
        edges.append(
            {
                "source": b,
                "target": a,
                "metric": 10,
                "link_type": "point-to-point",
                "area": "0.0.0.0",
            }
        )
    return {"metadata": {}, "nodes": nodes, "edges": edges}


def without(parsed: dict, drop: set[str]) -> dict:
    out = deepcopy(parsed)
    out["nodes"] = [n for n in out["nodes"] if n["id"] not in drop]
    out["edges"] = [
        e for e in out["edges"] if e["source"] not in drop and e["target"] not in drop
    ]
    return out


def read(path):
    return json.loads(path.read_text())


def test_first_run_saves_baseline_without_changes(tmp_path):
    engine = GraphEngine(tmp_path)
    _, diff = engine.process(topology())
    assert (
        not diff.baseline_available and not diff.has_changes and diff.baseline_updated
    )
    assert engine.current_path.exists() and not engine.previous_path.exists()


def test_unchanged_second_run_rotates_state(tmp_path):
    engine = GraphEngine(tmp_path)
    engine.process(topology())
    _, diff = engine.process(topology())
    assert diff.baseline_available and not diff.has_changes
    assert engine.previous_path.exists() and engine.current_path.exists()


def test_metric_change_and_link_removal_are_reported(tmp_path):
    engine = GraphEngine(tmp_path)
    engine.process(topology())
    changed = topology()
    changed["edges"][0]["metric"] = 99
    del changed["edges"][3]
    _, diff = engine.process(changed)
    assert [(e["old_metric"], e["new_metric"]) for e in diff.changed_metrics] == [
        (10, 99)
    ]
    assert len(diff.removed_edges) == 1 and not diff.suspect_reasons


def test_guard_holds_baseline_when_too_many_nodes_vanish(tmp_path):
    engine = GraphEngine(tmp_path)
    engine.process(topology())
    before = read(engine.current_path)

    degraded = without(
        topology(), {"10.0.0.3", "10.0.0.4", "10.0.0.5"}
    )  # 2 of 5 kept = 40%
    _, diff = engine.process(degraded)

    assert diff.suspect_reasons and not diff.baseline_updated
    assert read(engine.current_path)["nodes"] == before["nodes"]  # baseline untouched
    assert not engine.previous_path.exists()  # and not rotated
    assert engine.suspect_path.exists()
    assert len(diff.removed_nodes) == 3  # the report still shows what was seen


def test_guard_counts_only_nodes_that_still_have_an_lsa(tmp_path):
    """Surviving LSAs keep referencing vanished routers, which then linger as unresolved placeholders."""
    engine = GraphEngine(tmp_path)
    engine.process(topology())
    partial = topology()
    for node in partial["nodes"]:
        if node["id"] in {"10.0.0.3", "10.0.0.4", "10.0.0.5"}:
            node["resolved"] = (
                False  # still referenced by neighbors, but no LSA of their own
            )
    _, diff = engine.process(partial)
    assert diff.suspect_reasons and not diff.baseline_updated
    assert "still have an LSA" in diff.suspect_reasons[0]


def test_guard_flags_partition_even_with_high_retention(tmp_path):
    engine = GraphEngine(tmp_path, GuardConfig(min_node_retention=0.0))
    engine.process(topology(extra_routers=4))  # 10-router ring
    split = topology(extra_routers=4)
    # Cut the ring in two places -> two weakly connected parts, all nodes still present.
    cut = {
        ("10.0.0.2", "10.0.0.3"),
        ("10.0.0.3", "10.0.0.2"),
        ("10.0.0.7", "10.0.0.8"),
        ("10.0.0.8", "10.0.0.7"),
    }
    split["edges"] = [
        e for e in split["edges"] if (e["source"], e["target"]) not in cut
    ]
    _, diff = engine.process(split)
    assert (
        any("disconnected" in r for r in diff.suspect_reasons)
        and not diff.baseline_updated
    )


def test_accept_changes_commits_a_flagged_run(tmp_path):
    engine = GraphEngine(tmp_path)
    engine.process(topology())
    degraded = without(topology(), {"10.0.0.3", "10.0.0.4", "10.0.0.5"})
    engine.process(degraded)  # held back
    _, diff = engine.process(degraded, accept_changes=True)
    assert diff.suspect_reasons and diff.baseline_updated
    assert len(read(engine.current_path)["nodes"]) == 2
    assert engine.previous_path.exists() and not engine.suspect_path.exists()


def test_guard_can_be_disabled(tmp_path):
    engine = GraphEngine(tmp_path, GuardConfig(enabled=False))
    engine.process(topology())
    _, diff = engine.process(without(topology(), {"10.0.0.3", "10.0.0.4", "10.0.0.5"}))
    assert not diff.suspect_reasons and diff.baseline_updated


def test_recovers_from_crash_between_rotation_and_write(tmp_path):
    engine = GraphEngine(tmp_path)
    engine.process(topology())
    engine.rotate_state()  # simulate dying right after the move: current is gone, previous holds the baseline
    assert not engine.current_path.exists()
    _, diff = engine.process(topology())
    assert diff.baseline_available and not diff.has_changes


def test_corrupt_state_is_treated_as_no_baseline(tmp_path):
    engine = GraphEngine(tmp_path)
    engine.process(topology())
    engine.current_path.write_text("{not json")
    _, diff = engine.process(topology())
    assert not diff.baseline_available


def test_empty_topology_is_rejected(tmp_path):
    from src.graph_engine import GraphEngineError

    with pytest.raises(GraphEngineError):
        GraphEngine(tmp_path).process({"metadata": {}, "nodes": [], "edges": []})


def test_guard_settings_validation():
    from src.graph_engine import GraphEngineError

    assert GuardConfig.from_settings(None) == GuardConfig()
    assert (
        GuardConfig.from_settings({"min_node_retention": 0.5}).min_node_retention == 0.5
    )
    with pytest.raises(GraphEngineError):
        GuardConfig.from_settings({"min_node_retention": 3})


def test_interface_address_survives_state_round_trip_and_reaches_removed_edge_records(
    tmp_path,
):
    """The diagram labels removed links too, so the diff must carry each edge's interface address."""
    engine = GraphEngine(tmp_path)
    with_addr = topology()
    for edge in with_addr["edges"]:
        edge["interface_address"] = "10.9.9." + edge["source"].split(".")[-1]
    engine.process(with_addr)
    assert (
        engine.load_graph(engine.current_path).edges["10.0.0.1", "10.0.0.2"][
            "interface_address"
        ]
        == "10.9.9.1"
    )

    shrunk = deepcopy(with_addr)
    shrunk["edges"] = [
        e
        for e in shrunk["edges"]
        if {e["source"], e["target"]} != {"10.0.0.1", "10.0.0.2"}
    ]
    _, diff = engine.process(shrunk)
    assert {e["interface_address"] for e in diff.removed_edges} == {
        "10.9.9.1",
        "10.9.9.2",
    }
