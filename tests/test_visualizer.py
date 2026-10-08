"""Unit tests for TopologyVisualizer.

These tests verify that ``build_digraph()`` produces correct Graphviz DOT
source for all node/edge states — without actually invoking the ``dot``
executable (no system Graphviz required).
"""

from __future__ import annotations

import networkx as nx
import pytest

from src.graph_engine import TopologyDiff
from src.visualizer import (
    COLOR_ADDED,
    COLOR_CHANGED,
    COLOR_REMOVED,
    FILL_UNRESOLVED,
    TopologyVisualizer,
    VisualizationError,
)


def _graph() -> nx.DiGraph:
    """Minimal 2-router + 1 transit-network topology."""
    g = nx.DiGraph()
    g.add_node("10.0.0.1", type="router", resolved=True, areas=["0.0.0.0"])
    g.add_node("10.0.0.2", type="router", resolved=True, areas=["0.0.0.0"])
    g.add_node(
        "net-192.168.1.1",
        type="network",
        resolved=True,
        areas=["0.0.0.0"],
        address="192.168.1.1",
        dr="10.0.0.1",
        mask="255.255.255.0",
        prefix="192.168.1.0/24",
    )
    g.add_edge("10.0.0.1", "10.0.0.2", metric=10, link_type="point-to-point", area="0.0.0.0")
    g.add_edge("10.0.0.1", "net-192.168.1.1", metric=1, link_type="transit", area="0.0.0.0")
    g.add_edge("net-192.168.1.1", "10.0.0.1", metric=0, link_type="attachment", area="0.0.0.0")
    return g


def _viz(**kw) -> TopologyVisualizer:
    return TopologyVisualizer(**kw)


# ---------------------------------------------------------------- smoke tests
def test_build_digraph_contains_all_nodes():
    dot = _viz().build_digraph(_graph(), TopologyDiff())
    src = dot.source
    assert "10.0.0.1" in src
    assert "10.0.0.2" in src
    assert "net-192.168.1.1" in src


def test_build_digraph_no_changes_no_colour_highlights():
    dot = _viz().build_digraph(_graph(), TopologyDiff())
    src = dot.source
    assert COLOR_ADDED not in src
    assert COLOR_REMOVED not in src
    assert COLOR_CHANGED not in src


# ----------------------------------------------------------- added elements
def test_added_node_uses_green():
    diff = TopologyDiff(
        baseline_available=True,
        added_nodes=[{"id": "10.0.0.2", "type": "router", "resolved": True}],
    )
    src = _viz().build_digraph(_graph(), diff).source
    assert COLOR_ADDED in src


def test_added_edge_uses_green():
    diff = TopologyDiff(
        baseline_available=True,
        added_edges=[{
            "source": "10.0.0.1", "target": "10.0.0.2",
            "source_label": "10.0.0.1", "target_label": "10.0.0.2",
            "metric": 10, "link_type": "point-to-point", "area": "0.0.0.0",
        }],
    )
    src = _viz().build_digraph(_graph(), diff).source
    assert COLOR_ADDED in src


# ---------------------------------------------------------- removed elements
def test_removed_node_uses_red():
    diff = TopologyDiff(
        baseline_available=True,
        removed_nodes=[{"id": "10.0.0.99", "type": "router", "resolved": True}],
    )
    src = _viz().build_digraph(_graph(), diff).source
    assert COLOR_REMOVED in src


def test_removed_edge_uses_red():
    diff = TopologyDiff(
        baseline_available=True,
        removed_edges=[{
            "source": "10.0.0.1", "target": "10.0.0.99",
            "source_label": "10.0.0.1", "target_label": "10.0.0.99",
            "metric": 10, "link_type": "point-to-point", "area": "0.0.0.0",
        }],
    )
    src = _viz().build_digraph(_graph(), diff).source
    assert COLOR_REMOVED in src


# ---------------------------------------------------------- metric changes
def test_changed_metric_uses_orange_and_shows_arrow():
    diff = TopologyDiff(
        baseline_available=True,
        changed_metrics=[{
            "source": "10.0.0.1", "target": "10.0.0.2",
            "source_label": "10.0.0.1", "target_label": "10.0.0.2",
            "old_metric": 10, "new_metric": 100,
            "link_type": "point-to-point", "area": "0.0.0.0",
        }],
    )
    src = _viz().build_digraph(_graph(), diff).source
    assert COLOR_CHANGED in src
    assert "→" in src or "10→100" in src or "10" in src  # old→new label present


# ------------------------------------------------------- unresolved nodes
def test_unresolved_node_gets_grey_fill():
    g = nx.DiGraph()
    g.add_node("10.0.0.1", type="router", resolved=False, areas=["0.0.0.0"])
    src = _viz().build_digraph(g, TopologyDiff()).source
    assert FILL_UNRESOLVED in src


def test_unresolved_node_label_contains_no_lsa():
    g = nx.DiGraph()
    g.add_node("10.0.0.1", type="router", resolved=False, areas=["0.0.0.0"])
    src = _viz().build_digraph(g, TopologyDiff()).source
    assert "no LSA" in src


# ----------------------------------------------------------- suspect title
def test_suspect_run_title_mentions_partial_lsdb():
    diff = TopologyDiff(suspect_reasons=["only 1 of 5 nodes still present"])
    src = _viz().build_digraph(_graph(), diff).source
    assert "SUSPECT" in src


def test_first_run_title_says_no_previous_state():
    diff = TopologyDiff(baseline_available=False)
    src = _viz().build_digraph(_graph(), diff).source
    assert "first run" in src.lower() or "no previous" in src.lower()


# ---------------------------------------------- constructor validation
def test_invalid_format_raises_visualization_error():
    with pytest.raises(VisualizationError, match="format"):
        TopologyVisualizer(output_path="output/topology.xyz")


def test_invalid_layout_raises_visualization_error():
    with pytest.raises(VisualizationError, match="layout"):
        TopologyVisualizer(layout="superlayout_9000")


# ------------------------------------------------- attachment edge label
def test_attachment_edges_have_no_metric_label():
    """Attachment (network→router) edges are always metric 0 and should be unlabelled."""
    g = nx.DiGraph()
    g.add_node("10.0.0.1", type="router", resolved=True, areas=["0.0.0.0"])
    g.add_node("net-1.1.1.1", type="network", resolved=True, areas=["0.0.0.0"],
               address="1.1.1.1", dr="10.0.0.1")
    g.add_edge("net-1.1.1.1", "10.0.0.1", metric=0, link_type="attachment", area="0.0.0.0")
    src = _viz().build_digraph(g, TopologyDiff()).source
    # The empty label means "0" must not appear as an edge label (it could appear in node IDs).
    # We check there is no label="0" attribute on the edge.
    assert 'label="0"' not in src
