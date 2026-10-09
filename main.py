#!/usr/bin/env python3
"""OSPF LSDB Monitor - orchestrator.

Pipeline:  Poll (SSH) -> Parse (Genie / vendor adapter) -> Engine (NetworkX + diff) -> Visualize (Graphviz)

Usage:
    python main.py                           # poll the router in config/settings.yaml
    python main.py --config other.yaml       # use a different settings file
    python main.py --device-type vyos        # override device.device_type
    python main.py --replay samples/vyos     # skip SSH; parse saved CLI output instead
    python main.py --save-raw captures/r1    # also keep the raw CLI output (for bug reports / new vendors)
    python main.py --accept-changes          # commit this run even if the safety guard flagged it
    python main.py -v                        # debug logging

Exit codes: 0 ok | 1 expected failure (config, SSH, parse, ...) | 2 unexpected error
            3 run flagged as a possible partial LSDB (baseline kept, see --accept-changes)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src import TrackerError, __version__
from src.config import load_settings, parse_process_id
from src.graph_engine import GraphEngine, GuardConfig, TopologyDiff
from src.parser import OSPFParser
from src.poller import BasePoller, DevicePoller, FilePoller, RawLSDB
from src.vendors import supported_device_types
from src.visualizer import TopologyVisualizer, VisualOptions

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config" / "settings.yaml"
DATA_DIR = BASE_DIR / "data"
OUTPUT_IMAGE = BASE_DIR / "output" / "topology.png"

EXIT_OK, EXIT_ERROR, EXIT_UNEXPECTED, EXIT_SUSPECT = 0, 1, 2, 3

logger = logging.getLogger("ospf_tracker")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Track OSPF topology changes from the LSDB of a seed router.")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="settings file (default: %(default)s)")
    ap.add_argument("--device-type", metavar="TYPE",
                    help=f"Netmiko device type, overrides the settings file ({', '.join(supported_device_types())})")
    ap.add_argument("--replay", type=Path, metavar="DIR",
                    help="read router_lsdb.txt / network_lsdb.txt from DIR instead of connecting to a device")
    ap.add_argument("--save-raw", type=Path, metavar="DIR",
                    help="write the raw CLI output to DIR (router_lsdb.txt, network_lsdb.txt); replayable with --replay")
    ap.add_argument("--output", type=Path, default=OUTPUT_IMAGE, metavar="PATH",
                    help="diagram output path; file extension sets the format (png, svg, pdf, ...) (default: %(default)s)")
    ap.add_argument("--layout", default="dot", metavar="ENGINE",
                    help="Graphviz layout engine — dot, neato, fdp, sfdp, circo, ... (default: %(default)s)")
    ap.add_argument("--accept-changes", action="store_true",
                    help="commit this run as the new baseline even if flagged as a possible partial LSDB")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and diff the topology without saving the new baseline or writing state files")
    ap.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap.parse_args(argv)


def print_summary(raw: RawLSDB, graph, diff: TopologyDiff) -> None:
    """Print a human-readable change report to the console."""
    print()
    print("=" * 64)
    print(" OSPF Topology Summary")
    print("=" * 64)
    print(f" Source    : {raw.source}   ({raw.collected_at})")
    print(f" Topology  : {graph.number_of_nodes()} nodes, {graph.number_of_edges()} directed links")

    if diff.suspect_reasons:
        verdict = "baseline NOT updated" if not diff.baseline_updated else "baseline updated (--accept-changes)"
        print(f"\n !! SUSPECT RUN - possible partial LSDB; {verdict}")
        for reason in diff.suspect_reasons:
            print(f"    - {reason}")
        if not diff.baseline_updated:
            print("    The seed router may have lost an adjacency. Re-run once it has recovered, or use")
            print("    --accept-changes if this change is real.")

    if not diff.baseline_available:
        print("\n First run (or no usable previous state): baseline saved, nothing to compare.")
        return
    if not diff.has_changes:
        print("\n No topology changes detected since the previous run.")
        return

    def section(title: str, items: list, fmt) -> None:
        if items:
            print(f"\n {title} ({len(items)}):")
            for item in items:
                print(f"   {fmt(item)}")

    link = lambda e: f"{e['source_label']} -> {e['target_label']}  [metric {e.get('metric', '?')}, {e.get('link_type', '?')}]"
    section("+ Nodes added", diff.added_nodes, lambda n: f"{n['label']}  ({n['type']})")
    section("- Nodes removed", diff.removed_nodes, lambda n: f"{n['label']}  ({n['type']})")
    section("+ Links added", diff.added_edges, link)
    section("- Links removed", diff.removed_edges, link)
    section(
        "~ Metrics changed", diff.changed_metrics,
        lambda e: f"{e['source_label']} -> {e['target_label']}  [{e['old_metric']} -> {e['new_metric']}]",
    )


def save_raw(raw: RawLSDB, directory: Path) -> None:
    """Keep the raw CLI output so a run can be replayed or attached to a bug report."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / FilePoller.ROUTER_FILE).write_text(raw.router, encoding="utf-8")
        (directory / FilePoller.NETWORK_FILE).write_text(raw.network, encoding="utf-8")
    except OSError as exc:
        raise TrackerError(f"Could not write raw capture to {directory}: {exc}") from exc
    logger.info("Raw CLI output saved to %s", directory)


def build_poller(args: argparse.Namespace, settings: dict) -> BasePoller:
    """Pick the data source. Swap or extend here to add API polling or another vendor."""
    if args.replay:
        device = settings.get("device") if isinstance(settings.get("device"), dict) else {}
        return FilePoller(
            args.replay,
            device_type=args.device_type or device.get("device_type", "cisco_ios"),
            process_id=parse_process_id(settings.get("ospf_process_id")),
        )
    return DevicePoller(args.config, device_type=args.device_type, settings=settings)


def run(args: argparse.Namespace) -> int:
    """Execute the full pipeline once."""
    settings = load_settings(args.config, required=not args.replay)

    # Build every stage first so configuration/import problems surface before any SSH traffic.
    poller = build_poller(args, settings)
    parser = OSPFParser(device_type=poller.device_type, process_id=poller.process_id)
    engine = GraphEngine(data_dir=DATA_DIR, guard=GuardConfig.from_settings(settings.get("guard")))
    visualizer = TopologyVisualizer(
        output_path=args.output, layout=args.layout,
        options=VisualOptions.from_settings(settings.get("visualization")),
    )

    raw = poller.poll()                                                  # 1. Poll
    if args.save_raw:
        save_raw(raw, args.save_raw)
    parsed = parser.parse(raw.router, raw.network)                       # 2. Parse
    graph, diff = engine.process(                                        # 3. Engine
        parsed, accept_changes=args.accept_changes, dry_run=args.dry_run,
    )

    print_summary(raw, graph, diff)                                      # shown even if rendering fails below
    if args.dry_run:
        print("\n [DRY RUN] Topology diffed — no state files written.\n")
    image = visualizer.render(graph, diff)                               # 4. Visualize
    print(f"\n Diagram   : {image}\n")
    if args.dry_run:
        return EXIT_OK
    return EXIT_SUSPECT if not diff.baseline_updated else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return run(args)
    except TrackerError as exc:
        logger.error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        logger.error("Interrupted")
        return 130
    except Exception:  # last-resort guard: log the traceback, return a clean failure code
        logger.exception("Unexpected error")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
