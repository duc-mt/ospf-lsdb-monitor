"""Visualization layer: NetworkX graph + diff report -> Graphviz diagram.

Styling
    routers            circles
    transit networks   diamonds
    new link/node      green, bold
    removed link/node  red, dashed (re-added to the drawing for this run only)
    metric change      orange, label ``old -> new``
    no LSA of its own  grey fill (referenced by a link, but not in the polled database)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import graphviz
import networkx as nx

from src import TrackerError
from src.graph_engine import TopologyDiff, display_name

logger = logging.getLogger(__name__)

COLOR_NORMAL = "#37474f"
COLOR_ADDED = "#2e7d32"
COLOR_REMOVED = "#c62828"
COLOR_CHANGED = "#ef6c00"
COLOR_ATTACHMENT = "#90a4ae"

FILL_ROUTER = "#bbdefb"
FILL_NETWORK = "#fff3c4"
FILL_UNRESOLVED = "#e0e0e0"
FILL_REMOVED = "#ffebee"


class VisualizationError(TrackerError):
    """Raised when the diagram cannot be rendered."""


class TopologyVisualizer:
    """Render the topology (and what changed since the last run) with Graphviz."""

    def __init__(self, output_path: str | os.PathLike = "output/topology.png", layout: str = "dot") -> None:
        """
        Args:
            output_path: Target image; the extension selects the format (png, svg, pdf, ...).
            layout: Graphviz layout engine (``dot``, ``neato``, ``fdp``, ``sfdp``, ...).
        """
        self.output_path = Path(output_path)
        self.layout = layout
        self.image_format = self.output_path.suffix.lstrip(".").lower() or "png"
        if self.image_format not in graphviz.FORMATS:
            raise VisualizationError(f"Unsupported output format '.{self.image_format}'")
        if layout not in graphviz.ENGINES:
            raise VisualizationError(f"Unknown Graphviz layout engine '{layout}'")

    # ---------------------------------------------------------------- building
    def build_digraph(self, graph: nx.DiGraph, diff: TopologyDiff) -> graphviz.Digraph:
        """Translate the graph and diff report into a Graphviz ``Digraph``."""
        dot = graphviz.Digraph(name="OSPF_Topology", engine=self.layout, format=self.image_format)
        dot.attr(
            label=self._title(graph, diff), labelloc="t", fontname="Helvetica", fontsize="13",
            fontcolor=COLOR_REMOVED if diff.suspect_reasons else "black",
            splines="true", overlap="false", nodesep="0.6", ranksep="0.9", dpi="150",
        )
        dot.attr("node", fontname="Helvetica", fontsize="11")
        dot.attr("edge", fontname="Helvetica", fontsize="10")

        added_nodes = {n["id"] for n in diff.added_nodes}
        added_edges = {(e["source"], e["target"]) for e in diff.added_edges}
        changed = {(e["source"], e["target"]): e for e in diff.changed_metrics}

        for node_id, attrs in graph.nodes(data=True):
            status = "added" if node_id in added_nodes else None
            self._draw_node(dot, node_id, attrs, status)
        # Removed elements exist only in the previous graph; draw them for this run.
        for record in diff.removed_nodes:
            self._draw_node(dot, record["id"], record, "removed")

        for source, target, attrs in graph.edges(data=True):
            key = (source, target)
            if key in added_edges:
                status = "added"
            elif key in changed:
                status = "changed"
            else:
                status = None
            self._draw_edge(dot, source, target, attrs, status, changed.get(key))
        for record in diff.removed_edges:
            self._draw_edge(dot, record["source"], record["target"], record, "removed")
        return dot

    @staticmethod
    def _title(graph: nx.DiGraph, diff: TopologyDiff) -> str:
        stamp = graph.graph.get("generated_at") or graph.graph.get("parsed_at") or ""
        lines = [f"OSPF topology  |  {graph.number_of_nodes()} nodes, {graph.number_of_edges()} links  |  {stamp}"]
        if diff.baseline_available:
            lines.append("green = new   red dashed = removed   orange = metric changed")
        else:
            lines.append("first run - no previous state to compare against")
        if diff.suspect_reasons:
            held = "baseline NOT updated" if not diff.baseline_updated else "baseline updated (--accept-changes)"
            lines.append(f"SUSPECT RUN - possible partial LSDB; {held}")
        return "\\n".join(lines)

    @staticmethod
    def _draw_node(dot: graphviz.Digraph, node_id: str, attrs: dict, status: str | None) -> None:
        is_network = attrs.get("type") == "network"
        label = display_name(node_id, attrs)
        if is_network and attrs.get("dr"):
            label += f"\\nDR {attrs['dr']}"

        style = {"style": "filled", "color": COLOR_NORMAL, "penwidth": "1.2", "fontcolor": "black",
                 "fillcolor": FILL_NETWORK if is_network else FILL_ROUTER}
        if not attrs.get("resolved", True):
            style["fillcolor"] = FILL_UNRESOLVED
            label += "\\n(no LSA)"
        if status == "added":
            style.update(color=COLOR_ADDED, penwidth="3")
        elif status == "removed":
            style.update(color=COLOR_REMOVED, fontcolor=COLOR_REMOVED, fillcolor=FILL_REMOVED,
                         style="filled,dashed", penwidth="2")

        dot.node(node_id, label=label, shape="diamond" if is_network else "circle", **style)

    @staticmethod
    def _draw_edge(
        dot: graphviz.Digraph, source: str, target: str, attrs: dict, status: str | None, change: dict | None = None
    ) -> None:
        # Network -> router attachments are always metric 0; leave them unlabelled.
        label = "" if attrs.get("link_type") == "attachment" else str(attrs.get("metric", ""))
        style = {"color": COLOR_ATTACHMENT if attrs.get("link_type") == "attachment" else COLOR_NORMAL,
                 "fontcolor": COLOR_NORMAL, "style": "solid", "penwidth": "1.2"}
        if status == "added":
            style.update(color=COLOR_ADDED, fontcolor=COLOR_ADDED, penwidth="2.6")
        elif status == "removed":
            style.update(color=COLOR_REMOVED, fontcolor=COLOR_REMOVED, style="dashed", penwidth="2.2")
        elif status == "changed" and change:
            label = f"{change['old_metric']}\u2192{change['new_metric']}"
            style.update(color=COLOR_CHANGED, fontcolor=COLOR_CHANGED, penwidth="2.6")
        dot.edge(source, target, label=label, **style)

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
            raise VisualizationError(f"Graphviz rendering failed ({exc}); DOT source saved to {source_path}") from exc
        logger.info("Rendered topology diagram to %s", rendered)
        return Path(rendered)

    @staticmethod
    def _save_source(dot: graphviz.Digraph, out_dir: Path, stem: str) -> Path:
        path = out_dir / f"{stem}.gv"
        try:
            path.write_text(dot.source, encoding="utf-8")
        except OSError:
            logger.warning("Could not save DOT source to %s", path)
        return path
