"""Graph layer: build the NetworkX graph, diff it against the last run, persist state.

State handling (``GraphEngine.process``)::

    build new graph
    baseline = data/current_state.json (last accepted run), else data/previous_state.json
    diff + safety guard (partial-LSDB detection)
    if the guard is happy (or --accept-changes):
        data/current_state.json  --move-->  data/previous_state.json
        write the new graph to data/current_state.json              (atomic)
    else:
        baseline is left untouched; the observed graph goes to data/suspect_state.json

Why the guard exists: when the seed router loses an adjacency its LSDB can shrink
or split, and without protection that degraded view would be saved as the new
baseline - the next healthy run would then report a wave of "new" nodes and links.
A run is flagged *suspect* when too few of the previously known nodes remain, or
when the graph falls apart into more disconnected pieces than before.

The rotation happens only after a graph was built successfully, so a failed poll
or parse never replaces the baseline. If the process dies between the rotation and
the write, the next run finds no current_state.json and falls back to
previous_state.json.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

from src import TrackerError, node_sort_key

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class GraphEngineError(TrackerError):
    """Raised when the graph cannot be built or its state cannot be saved."""


def display_name(node_id: str, attrs: dict[str, Any]) -> str:
    """Human-friendly node name: the router ID, or ``net <prefix>`` for transit networks."""
    if attrs.get("type") == "network":
        return f"net {attrs.get('prefix') or attrs.get('address') or node_id}"
    return str(node_id)


@dataclass(frozen=True)
class GuardConfig:
    """Thresholds for the partial-LSDB safety guard (``guard:`` in settings.yaml)."""

    enabled: bool = True
    #: Fraction of previously known nodes that must still be present (0.7 = at most 30% may vanish).
    min_node_retention: float = 0.7
    #: Flag runs where the graph has more disconnected pieces than the baseline.
    flag_partition: bool = True

    @classmethod
    def from_settings(cls, section: dict[str, Any] | None) -> "GuardConfig":
        """Build from the ``guard`` mapping of settings.yaml (missing keys use the defaults)."""
        section = section or {}
        try:
            cfg = cls(
                enabled=bool(section.get("enabled", True)),
                min_node_retention=float(section.get("min_node_retention", 0.7)),
                flag_partition=bool(section.get("flag_partition", True)),
            )
        except (TypeError, ValueError) as exc:
            raise GraphEngineError(f"Invalid 'guard' settings: {exc}") from exc
        if not 0.0 <= cfg.min_node_retention <= 1.0:
            raise GraphEngineError("guard.min_node_retention must be between 0 and 1")
        return cfg


@dataclass
class TopologyDiff:
    """Difference between the previous and the current topology.

    Every entry is a plain dict (JSON-friendly) carrying the node/edge attributes,
    so downstream consumers - e.g. the visualizer, which must still draw removed
    elements - need nothing but this report.
    """

    baseline_available: bool = False
    added_nodes: list[dict] = field(default_factory=list)
    removed_nodes: list[dict] = field(default_factory=list)
    added_edges: list[dict] = field(default_factory=list)
    removed_edges: list[dict] = field(default_factory=list)
    changed_metrics: list[dict] = field(default_factory=list)
    #: Why the run looks like a partial LSDB (empty = trustworthy).
    suspect_reasons: list[str] = field(default_factory=list)
    #: False when the guard kept the previous baseline instead of saving this run.
    baseline_updated: bool = True

    @property
    def has_changes(self) -> bool:
        return any((self.added_nodes, self.removed_nodes, self.added_edges, self.removed_edges, self.changed_metrics))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GraphEngine:
    """Builds, compares and persists the OSPF topology graph."""

    def __init__(self, data_dir: str | os.PathLike = "data", guard: GuardConfig | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.guard = guard or GuardConfig()
        self.current_path = self.data_dir / "current_state.json"
        self.previous_path = self.data_dir / "previous_state.json"
        self.suspect_path = self.data_dir / "suspect_state.json"

    # ------------------------------------------------------------------- build
    def build_graph(self, parsed: dict[str, Any]) -> nx.DiGraph:
        """Create a directed graph from the parser's standardized dictionary.

        Raises:
            GraphEngineError: If the dictionary is malformed or contains no nodes.
        """
        try:
            graph = nx.DiGraph(**dict(parsed.get("metadata") or {}))
            for node in parsed["nodes"]:
                attrs = dict(node)
                graph.add_node(attrs.pop("id"), **attrs)
            for edge in parsed["edges"]:
                attrs = dict(edge)
                graph.add_edge(attrs.pop("source"), attrs.pop("target"), **attrs)
        except (KeyError, TypeError, AttributeError) as exc:
            raise GraphEngineError(f"Parsed topology has an unexpected shape: {exc!r}") from exc

        if graph.number_of_nodes() == 0:
            raise GraphEngineError("Parsed topology contains no nodes; refusing to use it as a baseline")
        if not nx.is_weakly_connected(graph):
            logger.warning(
                "Topology is split into %d disconnected pieces - the LSDB may be partial "
                "(adjacency down on the seed router, or multiple areas)",
                nx.number_weakly_connected_components(graph),
            )
        return graph

    # ------------------------------------------------------------- persistence
    def load_graph(self, path: str | os.PathLike) -> nx.DiGraph | None:
        """Load a saved state file; return ``None`` if it is missing or unusable."""
        path = Path(path)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise ValueError(f"unsupported schema_version {payload.get('schema_version')!r}")
            graph = nx.DiGraph(**dict(payload.get("metadata") or {}))
            for node in payload["nodes"]:
                attrs = dict(node)
                graph.add_node(attrs.pop("id"), **attrs)
            for edge in payload["edges"]:
                attrs = dict(edge)
                graph.add_edge(attrs.pop("source"), attrs.pop("target"), **attrs)
            return graph
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("Ignoring unreadable state file %s (%s); treating this run as a fresh baseline", path, exc)
            return None

    def save_graph(self, graph: nx.DiGraph, path: str | os.PathLike | None = None) -> Path:
        """Write the graph as JSON (nodes + edges + metadata) atomically."""
        path = Path(path) if path else self.current_path
        graph.graph["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "metadata": dict(graph.graph),
            "nodes": [{"id": n, **graph.nodes[n]} for n in sorted(graph.nodes, key=node_sort_key)],
            "edges": [
                {"source": u, "target": v, **graph.edges[u, v]}
                for u, v in sorted(graph.edges, key=lambda e: (node_sort_key(e[0]), node_sort_key(e[1])))
            ],
        }
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)  # atomic: readers never see a half-written file
        except OSError as exc:
            raise GraphEngineError(f"Could not write state file {path}: {exc}") from exc
        logger.info("Saved topology state to %s", path)
        return path

    def rotate_state(self) -> bool:
        """Move current_state.json to previous_state.json. Returns True if a move happened."""
        if not self.current_path.exists():
            return False
        try:
            os.replace(self.current_path, self.previous_path)
        except OSError as exc:
            raise GraphEngineError(f"Could not move {self.current_path} to {self.previous_path}: {exc}") from exc
        return True

    # ------------------------------------------------------------------- compare
    @staticmethod
    def _node_record(graph: nx.DiGraph, node: str) -> dict[str, Any]:
        attrs = dict(graph.nodes[node])
        return {"id": node, "label": display_name(node, attrs), **attrs}

    @staticmethod
    def _edge_record(graph: nx.DiGraph, source: str, target: str) -> dict[str, Any]:
        return {
            "source": source,
            "target": target,
            "source_label": display_name(source, graph.nodes[source]),
            "target_label": display_name(target, graph.nodes[target]),
            **graph.edges[source, target],
        }

    def compare(self, old: nx.DiGraph | None, new: nx.DiGraph) -> TopologyDiff:
        """Diff two graphs. With no baseline, nothing is reported as changed."""
        if old is None:
            return TopologyDiff(baseline_available=False)

        def by_node(ids) -> list:
            return sorted(ids, key=node_sort_key)

        def by_edge(pairs) -> list:
            return sorted(pairs, key=lambda e: (node_sort_key(e[0]), node_sort_key(e[1])))

        old_nodes, new_nodes = set(old.nodes), set(new.nodes)
        old_edges, new_edges = set(old.edges), set(new.edges)

        diff = TopologyDiff(baseline_available=True)
        diff.added_nodes = [self._node_record(new, n) for n in by_node(new_nodes - old_nodes)]
        diff.removed_nodes = [self._node_record(old, n) for n in by_node(old_nodes - new_nodes)]
        diff.added_edges = [self._edge_record(new, u, v) for u, v in by_edge(new_edges - old_edges)]
        diff.removed_edges = [self._edge_record(old, u, v) for u, v in by_edge(old_edges - new_edges)]
        for u, v in by_edge(old_edges & new_edges):
            old_metric, new_metric = old.edges[u, v].get("metric"), new.edges[u, v].get("metric")
            if old_metric != new_metric:
                record = self._edge_record(new, u, v)
                record.update(old_metric=old_metric, new_metric=new_metric)
                record.pop("metric", None)
                diff.changed_metrics.append(record)
        return diff

    # --------------------------------------------------------------------- guard
    @staticmethod
    def _described(graph: nx.DiGraph) -> set[str]:
        """Nodes that have an LSA of their own in the database (not just a reference to them)."""
        return {n for n, attrs in graph.nodes(data=True) if attrs.get("resolved", True)}

    def assess(self, old: nx.DiGraph | None, new: nx.DiGraph) -> list[str]:
        """Return reasons why ``new`` looks like a partial LSDB (empty list = looks fine).

        Retention counts only nodes that still have their *own* LSA. In a partial LSDB the
        surviving LSAs keep referring to the missing routers, so those routers stay in the
        graph as placeholders; counting them would hide exactly the failure being detected.
        """
        guard = self.guard
        if not guard.enabled or old is None:
            return []
        reasons = []
        known = self._described(old)
        if known:
            kept = len(known & self._described(new))
            retention = kept / len(known)
            if retention < guard.min_node_retention:
                reasons.append(
                    f"only {kept} of {len(known)} previously known nodes still have an LSA in the database "
                    f"({retention:.0%}, minimum {guard.min_node_retention:.0%})"
                )
        if guard.flag_partition:
            before = nx.number_weakly_connected_components(old)
            after = nx.number_weakly_connected_components(new)
            if after > before:
                reasons.append(f"the topology split into {after} disconnected parts (was {before})")
        return reasons

    # ------------------------------------------------------------------ pipeline
    def process(self, parsed: dict[str, Any], accept_changes: bool = False) -> tuple[nx.DiGraph, TopologyDiff]:
        """Build the graph, diff it against the baseline and (if trustworthy) save it.

        Args:
            parsed: The parser's standardized dictionary.
            accept_changes: Commit this run as the new baseline even if the guard flagged it.

        Returns:
            ``(graph, diff)``. ``diff.suspect_reasons`` / ``diff.baseline_updated`` tell
            whether the guard held the baseline back.
        """
        graph = self.build_graph(parsed)
        baseline = self.load_graph(self.current_path)
        if baseline is None:
            baseline = self.load_graph(self.previous_path)  # crash recovery / first run after rotation
        diff = self.compare(baseline, graph)
        diff.suspect_reasons = self.assess(baseline, graph)

        if diff.suspect_reasons and not accept_changes:
            diff.baseline_updated = False
            self.save_graph(graph, self.suspect_path)
            logger.warning("Run looks like a partial LSDB; baseline kept. Observed state saved to %s",
                           self.suspect_path)
            return graph, diff

        if self.rotate_state():
            logger.debug("Rotated %s -> %s", self.current_path, self.previous_path)
        self.save_graph(graph)
        self.suspect_path.unlink(missing_ok=True)  # any earlier suspect snapshot is now stale
        return graph, diff
