from __future__ import annotations
"""Unit tests for TopologyVisualizer.

These tests verify that ``build_digraph()`` produces correct Graphviz DOT
source for all node/edge states — without actually invoking the ``dot``
executable (no system Graphviz required).
"""


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


# ======================================================================================
# Drawing improvements: one line per link, per-end labels, layout, clusters, options
# ======================================================================================
import re  # noqa: E402

from src.visualizer import VisualOptions  # noqa: E402


def _p2p_graph(fwd: int, back: int, addr: bool = False) -> nx.DiGraph:
    g = nx.DiGraph()
    for r in ("10.0.0.1", "10.0.0.2"):
        g.add_node(r, type="router", resolved=True, areas=["0.0.0.0"])
    for (u, v, cost, ip) in (("10.0.0.1", "10.0.0.2", fwd, "10.12.0.1"), ("10.0.0.2", "10.0.0.1", back, "10.12.0.2")):
        extra = {"interface_address": ip} if addr else {}
        g.add_edge(u, v, metric=cost, link_type="point-to-point", area="0.0.0.0", **extra)
    return g


def _hub_graph(spokes: int) -> nx.DiGraph:
    """One transit network with ``spokes`` routers attached (each with a cost and an attachment edge)."""
    g = nx.DiGraph()
    g.add_node("net-10.1.1.1", type="network", resolved=True, areas=["0.0.0.0"], dr="10.0.0.1", prefix="10.1.1.0/24")
    for i in range(1, spokes + 1):
        r = f"10.0.0.{i}"
        g.add_node(r, type="router", resolved=True, areas=["0.0.0.0"])
        g.add_edge(r, "net-10.1.1.1", metric=1, link_type="transit", area="0.0.0.0", interface_address=f"10.1.1.{i}")
        g.add_edge("net-10.1.1.1", r, metric=0, link_type="attachment", area="0.0.0.0")
    return g


def _src(graph, diff=None, **opts) -> str:
    return _viz(options=VisualOptions(**opts)).build_digraph(graph, diff or TopologyDiff()).source


# ------------------------------------------------------------ one line per link
def test_both_directions_of_a_link_are_one_line():
    src = _src(_p2p_graph(10, 10))
    assert src.count("--") == 1 and "->" not in src


def test_segment_attachments_are_not_drawn_as_extra_lines():
    # 5 routers on one segment = 5 links, not 10 arrows (the cost-0 attachments add nothing)
    assert _src(_hub_graph(5)).count("--") == 5


def test_title_counts_links_not_directed_edges():
    src = _src(_hub_graph(5))
    assert "5 routers, 1 transit networks, 5 links" in src


# ------------------------------------------------------------------ cost labels
def test_symmetric_p2p_gets_a_single_centred_label():
    src = _src(_p2p_graph(10, 10), show_interfaces=False)
    assert 'label=10' in src and "taillabel" not in src and "headlabel" not in src


def test_asymmetric_p2p_labels_each_end_with_its_own_cost():
    src = _src(_p2p_graph(10, 20), show_interfaces=False)
    assert "taillabel=10" in src and "headlabel=20" in src


def test_interface_address_follows_the_cost_at_each_end():
    src = _src(_p2p_graph(10, 10, addr=True))
    assert 'taillabel="10\\n10.12.0.1"' in src and 'headlabel="10\\n10.12.0.2"' in src


def test_segment_link_shows_cost_and_interface_address_in_the_middle():
    assert 'label="1\\n10.1.1.3"' in _src(_hub_graph(3))


def test_interface_addresses_can_be_switched_off():
    src = _src(_hub_graph(3), show_interfaces=False)
    assert "10.1.1.3" not in src.replace('"net-10.1.1.1"', "")  # address would only appear via a label


# --------------------------------------------------------------- diff on merged lines
def test_removed_link_is_one_dashed_red_line_even_though_two_directions_were_removed():
    record = lambda s, t: {"source": s, "target": t, "source_label": s, "target_label": t,  # noqa: E731
                           "metric": 10, "link_type": "point-to-point", "area": "0.0.0.0"}
    g = nx.DiGraph()
    g.add_node("10.0.0.1", type="router", resolved=True, areas=["0.0.0.0"])
    diff = TopologyDiff(
        baseline_available=True,
        removed_nodes=[{"id": "10.0.0.2", "type": "router", "resolved": True, "areas": ["0.0.0.0"]}],
        removed_edges=[record("10.0.0.1", "10.0.0.2"), record("10.0.0.2", "10.0.0.1")],
    )
    src = _src(g, diff)
    assert src.count("--") == 1
    edge_line = next(l for l in src.splitlines() if "--" in l)
    assert COLOR_REMOVED in edge_line and "dashed" in edge_line


def test_metric_change_in_one_direction_marks_the_whole_line_orange():
    g = _p2p_graph(100, 10, addr=False)
    change = {"source": "10.0.0.1", "target": "10.0.0.2", "old_metric": 10, "new_metric": 100,
              "link_type": "point-to-point"}
    src = _src(g, TopologyDiff(baseline_available=True, changed_metrics=[change]), show_interfaces=False)
    edge_line = next(l for l in src.splitlines() if "--" in l)
    assert COLOR_CHANGED in edge_line and "10\u2192100" in edge_line and "headlabel=10" in edge_line


# --------------------------------------------------------------------- layout
def test_auto_layout_goes_left_to_right_for_a_hub_and_top_down_otherwise():
    assert "rankdir=LR" in _src(_hub_graph(8))
    assert "rankdir=TB" in _src(_hub_graph(3))


def test_explicit_rankdir_overrides_auto():
    assert "rankdir=TB" in _src(_hub_graph(8), rankdir="TB")
    assert "rankdir=LR" in _src(_hub_graph(3), rankdir="LR")


def test_split_topology_is_called_out_in_the_title():
    g = _p2p_graph(10, 10)
    g.add_node("10.9.9.9", type="router", resolved=True, areas=["0.0.0.0"])  # nothing connects to it
    assert "split into 2 disconnected parts" in _src(g)
    assert "disconnected" not in _src(_p2p_graph(10, 10))


# ----------------------------------------------------------- cost-weighted lines
def _penwidths(src: str) -> list[float]:
    return [float(m) for m in re.findall(r"penwidth=([\d.]+)", " ".join(l for l in src.splitlines() if "--" in l))]


def test_cheaper_links_are_drawn_thicker():
    g = _hub_graph(2)
    g.edges["10.0.0.2", "net-10.1.1.1"]["metric"] = 200
    cheap, dear = _penwidths(_src(g))
    assert cheap > dear


def test_cost_weighting_can_be_switched_off():
    g = _hub_graph(2)
    g.edges["10.0.0.2", "net-10.1.1.1"]["metric"] = 200
    a, b = _penwidths(_src(g, cost_weighted_lines=False))
    assert a == b


# --------------------------------------------------------------- area clusters
def _two_area_graph() -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_node("10.0.0.1", type="router", resolved=True, areas=["0.0.0.0"])
    g.add_node("10.0.0.2", type="router", resolved=True, areas=["0.0.0.0", "0.0.0.1"])  # ABR
    g.add_node("10.0.1.1", type="router", resolved=True, areas=["0.0.0.1"])
    for u, v in (("10.0.0.1", "10.0.0.2"), ("10.0.0.2", "10.0.1.1")):
        g.add_edge(u, v, metric=1, link_type="point-to-point", area="0.0.0.0")
        g.add_edge(v, u, metric=1, link_type="point-to-point", area="0.0.0.0")
    return g


def test_each_area_is_a_dashed_cluster_when_there_are_several():
    src = _src(_two_area_graph())
    assert "cluster_area_0_0_0_0" in src and "cluster_area_0_0_0_1" in src and 'label="Area 0.0.0.1"' in src


def test_abr_stays_outside_every_cluster():
    src = _src(_two_area_graph())
    abr = re.search(r'^(\t+)"?10\.0\.0\.2"? \[', src, re.M)
    member = re.search(r'^(\t+)"?10\.0\.1\.1"? \[', src, re.M)
    assert abr.group(1) == "\t" and member.group(1) == "\t\t"  # indentation depth = inside a subgraph or not


def test_single_area_needs_no_cluster():
    assert "cluster" not in _src(_p2p_graph(10, 10))


def test_clusters_can_be_switched_off():
    assert "cluster" not in _src(_two_area_graph(), area_clusters=False)


# ------------------------------------------------------------ node styling/options
def test_router_shape_and_hostname_options():
    src = _src(_p2p_graph(10, 10), router_shape="circle", names={"10.0.0.1": "core-1"})
    assert "shape=circle" in src and "box3d" not in src and "core-1\\n10.0.0.1" in src


def test_routers_default_to_3d_boxes_and_networks_to_diamonds():
    src = _src(_hub_graph(1))
    assert "shape=box3d" in src and "shape=diamond" in src


def test_unresolved_node_is_dashed_not_a_diff_colour():
    g = nx.DiGraph()
    g.add_node("10.0.0.1", type="router", resolved=False, areas=["0.0.0.0"])
    node_line = next(l for l in _src(g).splitlines() if '"10.0.0.1"' in l or "10.0.0.1 [" in l)
    assert "dashed" in node_line and COLOR_REMOVED not in node_line


@pytest.mark.parametrize("section, message", [
    ({"rankdir": "sideways"}, "rankdir"),
    ({"router_shape": "hexagon-ish"}, "router_shape"),
    ({"names": ["not", "a", "map"]}, "names"),
    ("not a mapping", "mapping"),
])
def test_bad_visualization_settings_are_rejected(section, message):
    with pytest.raises(VisualizationError, match=message):
        VisualOptions.from_settings(section)


def test_visualization_settings_defaults_and_overrides():
    assert VisualOptions.from_settings(None) == VisualOptions()
    opts = VisualOptions.from_settings({"rankdir": "LR", "show_interfaces": False, "names": {1: "x"}})
    assert (opts.rankdir, opts.show_interfaces, opts.names) == ("LR", False, {"1": "x"})


# ---------------------------------------- links caught mid-change (only one direction differs)
def _attachment_record(removed: bool = True) -> dict:
    return {"source": "net-10.1.1.1", "target": "10.0.0.1", "source_label": "net 10.1.1.0/24",
            "target_label": "10.0.0.1", "metric": 0, "link_type": "attachment", "area": "0.0.0.0"}


def _only_edge_line(src: str) -> str:
    (line,) = [l for l in src.splitlines() if "--" in l]
    return line


def test_link_is_not_drawn_removed_while_the_router_still_advertises_it():
    """Mid-convergence the DR's attachment can vanish a moment before the router's own link does."""
    g = _hub_graph(1)
    g.remove_edge("net-10.1.1.1", "10.0.0.1")
    diff = TopologyDiff(baseline_available=True, removed_edges=[_attachment_record()])
    line = _only_edge_line(_src(g, diff))
    assert COLOR_REMOVED not in line and "dashed" not in line


def test_link_is_not_drawn_new_when_only_one_direction_is_new():
    g = _hub_graph(1)
    new_side = {"source": "10.0.0.1", "target": "net-10.1.1.1"}
    line = _only_edge_line(_src(g, TopologyDiff(baseline_available=True, added_edges=[new_side])))
    assert COLOR_ADDED not in line


def test_link_is_new_when_every_direction_that_exists_is_new():
    g = _hub_graph(1)
    both = [{"source": "10.0.0.1", "target": "net-10.1.1.1"}, {"source": "net-10.1.1.1", "target": "10.0.0.1"}]
    assert COLOR_ADDED in _only_edge_line(_src(g, TopologyDiff(baseline_available=True, added_edges=both)))
