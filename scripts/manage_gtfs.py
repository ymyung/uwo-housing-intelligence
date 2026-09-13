"""Inspect or safely stage an explicitly supplied static GTFS feed."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from backend.accessibility_inputs import load_worker_config
from backend.gtfs_freshness import GtfsValidationError, inspect_gtfs_feed
from pipeline.run_context import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "accessibility-worker.toml"
DEFAULT_STAGING_ROOT = PROJECT_ROOT / "data" / "routing" / "staging"
MAX_GTFS_BYTES = 100 * 1024 * 1024


def _safe_source_label(source: str) -> str:
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return str(Path(source).resolve())


def _copy_source(source: str, destination: Path) -> None:
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        request = urllib.request.Request(
            source,
            headers={"User-Agent": "uwo-housing-gtfs-refresh/1"},
        )
        with urllib.request.urlopen(request, timeout=60) as response, destination.open(
            "wb"
        ) as target:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_GTFS_BYTES:
                raise GtfsValidationError("GTFS download exceeds the 100 MB safety limit")
            copied = 0
            while block := response.read(1024 * 1024):
                copied += len(block)
                if copied > MAX_GTFS_BYTES:
                    raise GtfsValidationError(
                        "GTFS download exceeds the 100 MB safety limit"
                    )
                target.write(block)
        return
    windows_path = len(parsed.scheme) == 1 and len(source) >= 3 and source[1] == ":"
    if parsed.scheme and not windows_path:
        raise GtfsValidationError("GTFS source must be an HTTP(S) URL or local path")
    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise GtfsValidationError(f"GTFS source file is missing: {source_path}")
    if source_path.stat().st_size > MAX_GTFS_BYTES:
        raise GtfsValidationError("GTFS source exceeds the 100 MB safety limit")
    shutil.copyfile(source_path, destination)


def _report(config_path: Path, gtfs_path: Path, as_of: date | None) -> dict[str, Any]:
    config = load_worker_config(config_path.resolve(), PROJECT_ROOT)
    report = inspect_gtfs_feed(
        gtfs_path,
        reference_week_start=config.reference_service_week,
        as_of_date=as_of,
    ).to_dict()
    manifest: dict[str, Any] = {}
    if config.build_manifest_path.is_file():
        manifest = json.loads(config.build_manifest_path.read_text(encoding="utf-8-sig"))
    expected = (manifest.get("input_fingerprints") or {}).get("gtfs_sha256")
    report["configured_path"] = str(gtfs_path.resolve())
    report["graph_manifest_gtfs_sha256"] = expected
    report["graph_feed_fingerprint_matches"] = expected == report["feed_sha256"]
    report["graph_rebuild_required"] = bool(
        expected and expected != report["feed_sha256"]
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="Inspect the configured feed")
    status.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    status.add_argument("--gtfs", type=Path)
    status.add_argument("--as-of", type=date.fromisoformat)
    status.add_argument("--output", type=Path)

    stage = subparsers.add_parser(
        "stage", help="Copy/download and validate without replacing the active feed"
    )
    stage.add_argument("--source", required=True)
    stage.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    stage.add_argument("--staging-root", type=Path, default=DEFAULT_STAGING_ROOT)
    stage.add_argument("--as-of", type=date.fromisoformat)
    stage.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_worker_config(args.config.resolve(), PROJECT_ROOT)
    if args.command == "status":
        report = _report(args.config, (args.gtfs or config.gtfs_path).resolve(), args.as_of)
        if args.output:
            atomic_write_json(args.output.resolve(), report)
        print(json.dumps(report, sort_keys=True))
        return 0

    staging_root = args.staging_root.resolve()
    staging_root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="gtfs-candidate-", suffix=".zip", dir=staging_root
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _copy_source(args.source, temporary)
        report = _report(args.config, temporary, args.as_of)
        report.update(
            {
                "source": _safe_source_label(args.source),
                "staged_at": datetime.now(timezone.utc).isoformat(),
                "promotion_status": "not_promoted",
            }
        )
        if args.dry_run:
            report["staged_directory"] = None
            print(json.dumps(report, sort_keys=True))
            return 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        staged = staging_root / f"{stamp}_{report['feed_sha256'][:12]}"
        staged.mkdir(parents=False, exist_ok=False)
        candidate = staged / "london-transit.gtfs.zip"
        temporary.replace(candidate)
        report["staged_directory"] = str(staged)
        atomic_write_json(staged / "gtfs-validation.json", report)
        print(json.dumps(report, sort_keys=True))
        return 0
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
