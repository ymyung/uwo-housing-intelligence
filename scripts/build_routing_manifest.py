"""Fingerprint local routing inputs and write an auditable build manifest."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from backend.accessibility_inputs import load_worker_config, sha256_file
from backend.gtfs_freshness import inspect_gtfs_feed
from pipeline.run_context import atomic_write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--router-version", required=True)
    parser.add_argument("--network-version", required=True)
    parser.add_argument("--schedule-version", required=True)
    parser.add_argument("--graph-built-at")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_worker_config(Path(args.config).resolve())
    paths = {
        "osm_sha256": config.osm_path,
        "gtfs_sha256": config.gtfs_path,
        "router_config_sha256": config.router_config_path,
        "hotspot_config_sha256": config.hotspot_config_path,
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise SystemExit(f"Routing inputs are missing: {', '.join(missing)}")
    built_at = (
        datetime.fromisoformat(args.graph_built_at.replace("Z", "+00:00"))
        if args.graph_built_at
        else datetime.now(timezone.utc)
    )
    if built_at.tzinfo is None:
        raise SystemExit("--graph-built-at must include a timezone")
    gtfs = inspect_gtfs_feed(
        config.gtfs_path,
        reference_week_start=config.reference_service_week,
    )
    atomic_write_json(
        config.build_manifest_path,
        {
            "schema_version": 2,
            "graph_built_at": built_at.isoformat(),
            "router_version": args.router_version,
            "network_version": args.network_version,
            "schedule_version": args.schedule_version,
            "input_fingerprints": {
                name: sha256_file(path) for name, path in paths.items()
            },
            "gtfs_service": gtfs.to_dict(),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
