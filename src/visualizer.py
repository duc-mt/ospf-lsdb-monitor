
"""
==============================================================================
Module Name:   visualizer.py
Description:   Visualization layer: NetworkX graph + diff report -> Graphviz diagram.  How the drawing is built (conventions follow the graphviz-diagrams skill for network topologies):  * **One line per link.** OSPF describes a link from both ends (router -> segment   cost, segment -> router cost 0, and one cost per direction on point-to-point   links). Drawing every direction as its own arrow doubles the clutter, so the   two directions are merged into a single undirected line and the *cost sits at   the end of the interface that advertises it* (``taillabel``/``headlabel``).   A symmetric point-to-point link gets one centred label instead. * **Interface labels.** Each cost is followed by the advertising router's own   interface address on that link (OSPF carries no interface names or speeds). * **Shapes and colour.** Routers are blue 3D boxes (``router_shape`` setting),   transit networks are diamonds showing prefix and DR, routers/networks that are   only referenced (no LSA of their own) are grey and dashed. Grey never means   "backup link" here. * **Diff colours.** New = green, removed = red + dashed (drawn for this run only),   metric change = orange ``old->new``. * **Weight.** Line thickness follows cost (cheaper = thicker) so the preferred   paths stand out. * **Layout.** ``rankdir`` is chosen automatically: a hub with many neighbours is   drawn left-to-right (compact), small topologies top-to-bottom. * **Areas.** With more than one OSPF area, each area is drawn as a dashed cluster;   ABRs (several areas) stay outside every cluster.
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 visualizer.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import graphviz
import networkx as nx

from src import NETWORK_ID_PREFIX, TrackerError, node_sort_key
from src.graph_engine import TopologyDiff, display_name

logger = logging.getLogger(__name__)

COLOR_NORMAL = "#2C3E50"
COLOR_ADDED = "#27AE60"
COLOR_REMOVED = "#E74C3C"
COLOR_CHANGED = "#E67E22"
COLOR_MUTED = "#95A5A6"

FILL_ROUTER = "#3498DB"
FILL_NETWORK = "#FFF3C4"
FILL_UNRESOLVED = "#ECF0F1"
FILL_REMOVED = "#FDEDEC"

#: Auto layout: a node with at least this many neighbours switches the drawing to left-to-right.
AUTO_LR_DEGREE = 6
#: Line thickness range used when drawing cost-weighted lines.
PEN_MIN, PEN_MAX, PEN_FLAT = 1.0, 2.6, 1.6

_RANKDIRS = ("auto", "TB", "LR", "BT", "RL")
_ROUTER_SHAPES = ("box3d", "box", "circle", "ellipse", "oval", "doublecircle")


class VisualizationError(TrackerError):
    """Raised when the diagram cannot be rendered."""


@dataclass(frozen=True)
class VisualOptions:
    """Look-and-feel settings (``visualization:`` in settings.yaml)."""

    rankdir: str = "auto"  # auto | TB | LR | BT | RL
    router_shape: str = "box3d"
    show_interfaces: bool = (
        True  # print the advertising router's interface address next to each cost
    )
    cost_weighted_lines: bool = True
    area_clusters: bool = (
        True  # dashed cluster per OSPF area when there is more than one
    )
    names: dict[str, str] = field(
        default_factory=dict
    )  # router-ID -> hostname, shown above the ID

    @classmethod
    def from_settings(cls, section: dict[str, Any] | None) -> VisualOptions:
        """Build from the ``visualization`` mapping of settings.yaml (missing keys use the defaults).

        Raises:
            VisualizationError: On an unknown rankdir/shape or a malformed ``names`` map.
        """
        section = section or {}
        if not isinstance(section, dict):
            raise VisualizationError(
                "'visualization' in settings.yaml must be a mapping"
            )
        names = section.get("names") or {}
        if not isinstance(names, dict):
            raise VisualizationError(
                "visualization.names must map router IDs to host names"
            )
        options = cls(
            rankdir=str(section.get("rankdir", "auto")),
            router_shape=str(section.get("router_shape", "box3d")),
            show_interfaces=bool(section.get("show_interfaces", True)),
            cost_weighted_lines=bool(section.get("cost_weighted_lines", True)),
            area_clusters=bool(section.get("area_clusters", True)),
            names={str(k): str(v) for k, v in names.items()},
        )
        if options.rankdir not in _RANKDIRS:
            raise VisualizationError(
                f"visualization.rankdir must be one of {', '.join(_RANKDIRS)}"
            )
        if options.router_shape not in _ROUTER_SHAPES:
            raise VisualizationError(
                f"visualization.router_shape must be one of {', '.join(_ROUTER_SHAPES)}"
            )
        return options


@dataclass
class _Side:
    """One direction of a link (a -> b or b -> a)."""

    attrs: dict[str, Any]
    status: str | None = None  # None | "added" | "removed" | "changed"
    change: dict[str, Any] | None = None


@dataclass
class _Link:
    """Both directions of a link merged. ``a`` is the router end (router<->router: the lower ID)."""

    a: str
    b: str
    ab: _Side | None = None
    ba: _Side | None = None

    @property
    def cost_bearing(self) -> list[_Side]:
        return [
            s
            for s in (self.ab, self.ba)
            if s and s.attrs.get("link_type") != "attachment"
        ]


class TopologyVisualizer:
    """Render the topology (and what changed since the last run) with Graphviz."""

    def __init__(
        self,
        output_path: str | os.PathLike = "output/topology.png",
        layout: str = "dot",
        options: VisualOptions | None = None,
    ) -> None:
        """
        Args:
            output_path: Target image; the extension selects the format (png, svg, pdf, ...).
            layout: Graphviz layout engine (``dot``, ``neato``, ``fdp``, ``sfdp``, ...).
            options: Look-and-feel settings (defaults if omitted).
        """
        self.output_path = Path(output_path)
        self.layout = layout
        self.options = options or VisualOptions()
        self.image_format = self.output_path.suffix.lstrip(".").lower() or "png"
        if self.image_format not in graphviz.FORMATS:
            raise VisualizationError(
                f"Unsupported output format '.{self.image_format}'"
            )
        if layout not in graphviz.ENGINES:
            raise VisualizationError(f"Unknown Graphviz layout engine '{layout}'")

    # ---------------------------------------------------------------- building
    def build_digraph(self, graph: nx.DiGraph, diff: TopologyDiff) -> graphviz.Graph:
        """Translate the graph and diff report into a Graphviz graph.

        (The name is historical: the result is undirected, one line per link.)
        """
        removed_nodes = {n["id"]: n for n in diff.removed_nodes}
        links = self._collect_links(graph, diff, removed_nodes)

        dot = graphviz.Graph(
            name="OSPF_Topology", engine=self.layout, format=self.image_format
        )
        dot.attr(
            label=self._title(graph, diff, links),
            labelloc="t",
            fontname="Helvetica",
            fontsize="13",
            fontcolor=COLOR_REMOVED if diff.suspect_reasons else "black",
            rankdir=self._rankdir(links),
            splines="true",
            overlap="false",
            nodesep="0.5",
            ranksep="1.1",
            dpi="150",
        )
        dot.attr("node", fontname="Helvetica", fontsize="11")
        dot.attr("edge", fontname="Helvetica", fontsize="10", labeldistance="2.4")

        drawn = self._draw_nodes(dot, graph, diff, removed_nodes)

        scale = self._cost_scale(links)
        for link in links:
            for end in (
                link.a,
                link.b,
            ):  # an endpoint missing from graph and diff still needs a node
                if end not in drawn:
                    dot.node(
                        end,
                        label=end,
                        style="filled,dashed",
                        fillcolor=FILL_UNRESOLVED,
                        color=COLOR_MUTED,
                    )
                    drawn.add(end)
            dot.edge(link.a, link.b, **self._edge_attrs(link, scale))
        return dot

    # ------------------------------------------------------------------- links
    def _collect_links(
        self, graph: nx.DiGraph, diff: TopologyDiff, removed_nodes: dict[str, dict]
    ) -> list[_Link]:
        """Merge directed edges (current + removed) into one ``_Link`` per node pair."""
        added = {(e["source"], e["target"]) for e in diff.added_edges}
        changed = {(e["source"], e["target"]): e for e in diff.changed_metrics}
        links: dict[frozenset, _Link] = {}

        def is_network(node: str) -> bool:
            if node in graph:
                return graph.nodes[node].get("type") == "network"
            if node in removed_nodes:
                return removed_nodes[node].get("type") == "network"
            return str(node).startswith(NETWORK_ID_PREFIX)

        def put(u: str, v: str, side: _Side) -> None:
            key = frozenset((u, v))
            if key not in links:
                swap = (is_network(u) and not is_network(v)) or (
                    is_network(u) == is_network(v)
                    and node_sort_key(v) < node_sort_key(u)
                )
                links[key] = _Link(*((v, u) if swap else (u, v)))
            link = links[key]
            if (u, v) == (link.a, link.b):
                link.ab = side
            else:
                link.ba = side

        for u, v, attrs in graph.edges(data=True):
            status = (
                "added" if (u, v) in added else "changed" if (u, v) in changed else None
            )
            put(u, v, _Side(attrs, status, changed.get((u, v))))
        for record in diff.removed_edges:
            if not graph.has_edge(record["source"], record["target"]):
                put(record["source"], record["target"], _Side(record, "removed"))
        return sorted(
            links.values(), key=lambda l: (node_sort_key(l.a), node_sort_key(l.b))
        )

    @staticmethod
    def _link_status(link: _Link) -> str | None:
        """Overall status of a link, judged from the directions that currently exist."""
        sides = [s for s in (link.ab, link.ba) if s]
        live = [s for s in sides if s.status != "removed"]
        if not live:
            return "removed"
        if all(s.status == "added" for s in live):
            return "added"
        if any(s.status == "changed" for s in live):
            return "changed"
        return None

    def _end_text(self, side: _Side | None) -> str:
        """Label for one link end: the cost (``old->new`` if changed) and the interface address."""
        if side is None or side.attrs.get("link_type") == "attachment":
            return ""
        if side.status == "changed" and side.change:
            lines = [f"{side.change['old_metric']}\u2192{side.change['new_metric']}"]
        else:
            lines = [str(side.attrs.get("metric", ""))]
        address = side.attrs.get("interface_address")
        if self.options.show_interfaces and address:
            lines.append(str(address))
        return "\\n".join(line for line in lines if line)

    def _edge_attrs(self, link: _Link, scale) -> dict[str, str]:
        """Graphviz attributes for one link (labels at the right ends, colour by status)."""
        status = self._link_status(link)
        attrs: dict[str, str] = {"color": COLOR_NORMAL, "fontcolor": COLOR_NORMAL}

        text_a, text_b = self._end_text(link.ab), self._end_text(link.ba)
        on_segment = any(
            s.attrs.get("link_type") in ("transit", "attachment")
            for s in (link.ab, link.ba)
            if s
        )
        if on_segment:
            # Router <-> transit network: only the router's side carries a cost, so a centred label is
            # unambiguous (and keeps labels clear of busy router boxes).
            if text_a or text_b:
                attrs["label"] = text_a or text_b
        elif link.ab and link.ba and text_a == text_b and text_a:
            attrs["label"] = text_a  # symmetric point-to-point: one centred label
        else:
            # Point-to-point: each router advertises its own cost, shown at its own end of the line.
            if text_a:
                attrs["taillabel"] = text_a
            if text_b:
                attrs["headlabel"] = text_b

        costs = [
            s.attrs["metric"]
            for s in link.cost_bearing
            if isinstance(s.attrs.get("metric"), int)
        ]
        attrs["penwidth"] = f"{scale(min(costs)) if costs else PEN_FLAT:.1f}"
        if status == "added":
            attrs.update(color=COLOR_ADDED, fontcolor=COLOR_ADDED, penwidth="2.8")
        elif status == "removed":
            attrs.update(
                color=COLOR_REMOVED,
                fontcolor=COLOR_REMOVED,
                style="dashed",
                penwidth="2.4",
            )
        elif status == "changed":
            attrs.update(color=COLOR_CHANGED, fontcolor=COLOR_CHANGED, penwidth="2.8")
        return attrs

    def _cost_scale(self, links: list[_Link]):
        """Return ``cost -> line thickness`` (cheaper = thicker, log scale across the observed range)."""
        costs = [
            s.attrs["metric"]
            for link in links
            for s in link.cost_bearing
            if isinstance(s.attrs.get("metric"), int) and s.attrs["metric"] > 0
        ]
        if (
            not self.options.cost_weighted_lines
            or not costs
            or max(costs) == min(costs)
        ):
            return lambda _cost: PEN_FLAT
        lo, hi = min(costs), max(costs)
        return lambda cost: (
            PEN_MAX
            - (PEN_MAX - PEN_MIN) * math.log(max(cost, lo) / lo) / math.log(hi / lo)
        )

    # ------------------------------------------------------------------- nodes
    def _draw_nodes(
        self,
        dot: graphviz.Graph,
        graph: nx.DiGraph,
        diff: TopologyDiff,
        removed_nodes: dict[str, dict],
    ) -> set[str]:
        """Draw every node (area clusters when there is more than one area); return the IDs drawn."""
        added = {n["id"] for n in diff.added_nodes}
        entries: list[tuple[str, dict, str | None]] = [
            (n, attrs, "added" if n in added else None)
            for n, attrs in graph.nodes(data=True)
        ]
        entries += [
            (n, rec, "removed") for n, rec in removed_nodes.items() if n not in graph
        ]
        entries.sort(key=lambda e: node_sort_key(e[0]))

        every_area = {a for _, attrs, _ in entries for a in attrs.get("areas") or []}
        clustered = self.options.area_clusters and len(every_area) > 1
        by_area: dict[str, list] = {}
        for entry in entries:
            areas = entry[1].get("areas") or []
            if (
                clustered and len(areas) == 1
            ):  # ABRs (several areas) stay outside every cluster
                by_area.setdefault(areas[0], []).append(entry)
            else:
                self._draw_node(dot, *entry)
        for area in sorted(by_area):
            with dot.subgraph(name="cluster_area_" + re.sub(r"\W+", "_", area)) as sub:
                sub.attr(
                    label=f"Area {area}",
                    labeljust="l",
                    style="dashed",
                    color=COLOR_MUTED,
                    fontcolor=COLOR_MUTED,
                    fontname="Helvetica",
                    fontsize="12",
                )
                for entry in by_area[area]:
                    self._draw_node(sub, *entry)
        return {e[0] for e in entries}

    def _draw_node(
        self, target: graphviz.Graph, node_id: str, attrs: dict, status: str | None
    ) -> None:
        """Draw one node: shape by role, fill by resolved/unresolved, outline by diff status."""
        if attrs.get("type") == "network":
            label = display_name(node_id, attrs)
            if attrs.get("dr"):
                label += f"\\nDR {attrs['dr']}"
            look = {"shape": "diamond", "fillcolor": FILL_NETWORK, "fontcolor": "black"}
        else:
            name = self.options.names.get(node_id)
            label = f"{name}\\n{node_id}" if name else node_id
            look = {
                "shape": self.options.router_shape,
                "fillcolor": FILL_ROUTER,
                "fontcolor": "white",
            }
        look.update(style="filled", color=COLOR_NORMAL, penwidth="1.2")

        if not attrs.get(
            "resolved", True
        ):  # referenced by a link, but no LSA of its own in the database
            look.update(
                fillcolor=FILL_UNRESOLVED,
                fontcolor=COLOR_NORMAL,
                color=COLOR_MUTED,
                style="filled,dashed",
            )
            label += "\\n(no LSA)"
        if status == "added":
            look.update(color=COLOR_ADDED, penwidth="3")
        elif status == "removed":
            look.update(
                color=COLOR_REMOVED,
                fontcolor=COLOR_REMOVED,
                fillcolor=FILL_REMOVED,
                style="filled,dashed",
                penwidth="2",
            )
        target.node(node_id, label=label, **look)

    # ------------------------------------------------------------ layout / text
    def _rankdir(self, links: list[_Link]) -> str:
        """Honour an explicit rankdir; otherwise go left-to-right when some node is a hub."""
        if self.options.rankdir != "auto":
            return self.options.rankdir
        neighbours: dict[str, set[str]] = {}
        for link in links:
            neighbours.setdefault(link.a, set()).add(link.b)
            neighbours.setdefault(link.b, set()).add(link.a)
        widest = max((len(n) for n in neighbours.values()), default=0)
        return "LR" if widest >= AUTO_LR_DEGREE else "TB"

    @staticmethod
    def _title(graph: nx.DiGraph, diff: TopologyDiff, links: list[_Link]) -> str:
        stamp = graph.graph.get("generated_at") or graph.graph.get("parsed_at") or ""
        kinds = [attrs.get("type") for _, attrs in graph.nodes(data=True)]
        live_links = sum(
            1 for link in links if TopologyVisualizer._link_status(link) != "removed"
        )
        lines = [
            (
                f"OSPF topology  |  {kinds.count('router')} routers, {kinds.count('network')} transit networks, "
                f"{live_links} links  |  {stamp}"
            )
        ]
        if diff.baseline_available:
            lines.append(
                "green = new   red dashed = removed   orange = metric changed   |   cost shown at each interface"
            )
        else:
            lines.append(
                "first run - no previous state to compare against   |   cost shown at each interface"
            )
        parts = (
            nx.number_weakly_connected_components(graph)
            if graph.number_of_nodes()
            else 0
        )
        if parts > 1:
            lines.append(f"WARNING: topology is split into {parts} disconnected parts")
        if diff.suspect_reasons:
            held = (
                "baseline NOT updated"
                if not diff.baseline_updated
                else "baseline updated (--accept-changes)"
            )
            lines.append(f"SUSPECT RUN - possible partial LSDB; {held}")
        return "\\n".join(lines)

    # ---------------------------------------------------------------- rendering
    def render(self, graph: nx.DiGraph, diff: TopologyDiff) -> Path:
        """Render the diagram to ``output_path`` and return the path of the image.

        On failure to run Graphviz, the DOT source is saved next to the target
        so it can be rendered later with ``dot -Tpng``.

        Raises:
            VisualizationError: If Graphviz is missing or fails.
        """
        dot = self.build_digraph(graph, diff)
        out_dir = self.output_path.parent
        stem = self.output_path.stem
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            rendered = dot.render(filename=stem, directory=str(out_dir), cleanup=True)
        except graphviz.ExecutableNotFound as exc:
            source_path = self._save_source(dot, out_dir, stem)
            raise VisualizationError(
                "The Graphviz 'dot' executable was not found (install the system package, e.g. "
                f"'apt install graphviz' or 'brew install graphviz'). DOT source saved to {source_path}"
            ) from exc
        except (graphviz.CalledProcessError, OSError) as exc:
            source_path = self._save_source(dot, out_dir, stem)
            raise VisualizationError(
                f"Graphviz rendering failed ({exc}); DOT source saved to {source_path}"
            ) from exc
        logger.info("Rendered topology diagram to %s", rendered)
        return Path(rendered)

    @staticmethod
    def _save_source(dot: graphviz.Graph, out_dir: Path, stem: str) -> Path:
        path = out_dir / f"{stem}.gv"
        try:
            path.write_text(dot.source, encoding="utf-8")
        except OSError:
            logger.warning("Could not save DOT source to %s", path)
        return path
