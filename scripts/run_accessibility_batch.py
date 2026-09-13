"""Bounded, resumable orchestration for accessibility worker batches."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

from backend.accessibility_inputs import AccessibilityInputError, load_worker_config
from backend.accessibility_runs import _atomic_text
from pipeline.remote_executor import redact_secrets
from pipeline.run_context import (
    atomic_write_json,
    generate_run_id,
    get_git_metadata,
    validate_run_id,
)
from scripts import run_accessibility_worker as worker_cli


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "accessibility-worker.toml"
DEFAULT_RUN_ROOT = PROJECT_ROOT / "data" / "accessibility-batches"
MAX_BATCH_SIZE = 20
METRIC_FIELDS = (
    "cache_hits",
    "sample_cache_hits",
    "provider_calls",
    "provider_retries",
    "profiles_created",
    "profiles_refreshed",
    "profiles_skipped",
    "profiles_failed",
    "database_writes",
)
LOGGER = logging.getLogger(__name__)


def _utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_error(error: BaseException) -> str:
    return redact_secrets(str(error)).strip()[:2000]


def load_property_ids(path: Path) -> list[int]:
    """Load unique positive property IDs while preserving reviewed CSV order."""

    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if "property_id" not in (reader.fieldnames or []):
            raise AccessibilityInputError("Property CSV is missing property_id")
        output: list[int] = []
        seen: set[int] = set()
        for number, row in enumerate(reader, start=2):
            try:
                property_id = int(str(row.get("property_id") or "").strip())
            except ValueError as exc:
                raise AccessibilityInputError(
                    f"Property CSV row {number} has an invalid property_id"
                ) from exc
            if property_id <= 0:
                raise AccessibilityInputError("Property IDs must be positive")
            if property_id in seen:
                raise AccessibilityInputError(
                    f"Property CSV contains duplicate property_id {property_id}"
                )
            seen.add(property_id)
            output.append(property_id)
    if not output:
        raise AccessibilityInputError("Property CSV contains no properties")
    return output


def build_batch_plan(property_ids: list[int], batch_size: int) -> list[dict[str, Any]]:
    """Create deterministic input-order batches without bypassing worker limits."""

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise AccessibilityInputError(
            f"batch_size must be between 1 and {MAX_BATCH_SIZE}"
        )
    return [
        {
            "batch_id": f"batch-{index // batch_size + 1:03d}",
            "property_ids": property_ids[index : index + batch_size],
            "property_count": len(property_ids[index : index + batch_size]),
            "status": "pending",
            "worker_run_id": None,
            "started_at": None,
            "completed_at": None,
            "duration_seconds": None,
            "metrics": None,
            "success_count": 0,
            "failure_count": 0,
            "quality_warnings": 0,
            "review_required_count": 0,
            "error_type": None,
            "error_message": None,
        }
        for index in range(0, len(property_ids), batch_size)
    ]


@dataclass(frozen=True)
class BatchPaths:
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def properties(self) -> Path:
        return self.root / "selected-properties.csv"

    @property
    def quality(self) -> Path:
        return self.root / "quality-report.csv"

    @property
    def warnings(self) -> Path:
        return self.root / "warning-report.csv"

    @property
    def review(self) -> Path:
        return self.root / "review-required.csv"


class AccessibilityBatchStore:
    """Atomic parent manifest and aggregate report storage."""

    def __init__(self, run_root: Path, batch_run_id: str) -> None:
        self.batch_run_id = validate_run_id(batch_run_id)
        self.paths = BatchPaths(run_root / self.batch_run_id)

    @classmethod
    def create(
        cls,
        run_root: Path,
        *,
        properties_path: Path,
        property_ids: list[int],
        config_path: Path,
        hotspot_ids: list[str],
        modes: list[str],
        batch_size: int,
        continue_on_failure: bool,
    ) -> "AccessibilityBatchStore":
        git_commit, git_dirty = get_git_metadata()
        store = cls(run_root, generate_run_id(git_commit=git_commit))
        store.paths.root.mkdir(parents=True, exist_ok=False)
        _atomic_text(store.paths.properties, properties_path.read_text(encoding="utf-8-sig"))
        manifest = {
            "schema_version": 1,
            "batch_run_id": store.batch_run_id,
            "git_commit": git_commit,
            "git_dirty": git_dirty,
            "status": "running",
            "started_at": _utc_text(),
            "completed_at": None,
            "source_properties_path": str(properties_path),
            "source_properties_sha256": _fingerprint(properties_path),
            "properties_snapshot": str(store.paths.properties),
            "config_path": str(config_path),
            "hotspot_ids": hotspot_ids,
            "modes": modes,
            "batch_size": batch_size,
            "continue_on_failure": continue_on_failure,
            "property_count": len(property_ids),
            "batch_count": len(build_batch_plan(property_ids, batch_size)),
            "batches": build_batch_plan(property_ids, batch_size),
            "aggregate": None,
        }
        store.write(manifest)
        return store

    @classmethod
    def open(cls, run_root: Path, batch_run_id: str) -> "AccessibilityBatchStore":
        store = cls(run_root, batch_run_id)
        if not store.paths.manifest.is_file():
            raise AccessibilityInputError(
                f"Unknown accessibility batch run {batch_run_id}"
            )
        return store

    def read(self) -> dict[str, Any]:
        return json.loads(self.paths.manifest.read_text(encoding="utf-8-sig"))

    def write(self, manifest: dict[str, Any]) -> None:
        atomic_write_json(self.paths.manifest, manifest)


def _worker_arguments(
    manifest: dict[str, Any],
    batch: dict[str, Any],
    *,
    dry_run: bool,
) -> argparse.Namespace:
    return argparse.Namespace(
        config=manifest["config_path"],
        properties=manifest["properties_snapshot"],
        property_ids=",".join(str(value) for value in batch["property_ids"]),
        hotspot_ids=",".join(manifest["hotspot_ids"]),
        modes=",".join(manifest["modes"]),
        allow_larger_run=False,
        dry_run=dry_run,
    )


def _resume_arguments(manifest: dict[str, Any], worker_run_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        config=manifest["config_path"],
        properties=None,
        property_ids=None,
        hotspot_ids=None,
        modes=",".join(manifest["modes"]),
        allow_larger_run=False,
        dry_run=False,
        run_id=worker_run_id,
    )


def _snapshot_property_ids(path: Path) -> list[int]:
    return load_property_ids(path)


def _discover_worker_run(
    worker_run_root: Path,
    parent: dict[str, Any],
    batch: dict[str, Any],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for path in worker_run_root.glob("*/manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8-sig"))
            selected_path = path.parent / "selected-properties.csv"
            if (
                manifest.get("started_at", "") >= parent["started_at"]
                and manifest.get("requested_modes") == parent["modes"]
                and selected_path.is_file()
                # The worker deliberately canonicalizes its reviewed selection by
                # property ID, while the parent preserves the reviewed batch order.
                and sorted(_snapshot_property_ids(selected_path))
                == sorted(batch["property_ids"])
            ):
                candidates.append(manifest)
        except (OSError, ValueError, json.JSONDecodeError, AccessibilityInputError):
            continue
    return max(candidates, key=lambda value: value.get("started_at", ""), default=None)


def _review_count(worker_run_root: Path, worker_run_id: str) -> int:
    path = worker_run_root / worker_run_id / "review-required.csv"
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8-sig", newline="") as source:
        return sum(1 for _ in csv.DictReader(source))


def _apply_worker_result(
    batch: dict[str, Any], result: dict[str, Any], worker_run_root: Path
) -> None:
    batch.update(
        {
            "status": "completed",
            "worker_run_id": result["run_id"],
            "completed_at": result.get("completed_at") or _utc_text(),
            "metrics": result.get("metrics") or {},
            "success_count": int(result.get("success_count") or 0),
            "failure_count": int(result.get("failure_count") or 0),
            "quality_warnings": int(result.get("quality_warnings") or 0),
            "review_required_count": _review_count(worker_run_root, result["run_id"]),
            "error_type": None,
            "error_message": None,
        }
    )


def _aggregate(manifest: dict[str, Any]) -> dict[str, Any]:
    metrics = {name: 0 for name in METRIC_FIELDS}
    output = {
        "properties_requested": int(manifest["property_count"]),
        "profiles_requested": 0,
        "success_count": 0,
        "failure_count": 0,
        "quality_warnings": 0,
        "review_required_count": 0,
        "metrics": metrics,
    }
    for batch in manifest["batches"]:
        output["success_count"] += int(batch.get("success_count") or 0)
        output["failure_count"] += int(batch.get("failure_count") or 0)
        output["quality_warnings"] += int(batch.get("quality_warnings") or 0)
        output["review_required_count"] += int(
            batch.get("review_required_count") or 0
        )
        for name in METRIC_FIELDS:
            metrics[name] += int((batch.get("metrics") or {}).get(name) or 0)
    output["profiles_requested"] = output["success_count"] + output["failure_count"]
    return output


def _csv_text(rows: Iterable[dict[str, Any]], fields: list[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows({field: row.get(field, "") for field in fields} for row in rows)
    return output.getvalue()


def _combine_reports(
    store: AccessibilityBatchStore,
    manifest: dict[str, Any],
    worker_run_root: Path,
) -> None:
    quality_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    for batch in manifest["batches"]:
        worker_run_id = batch.get("worker_run_id")
        if not worker_run_id or batch.get("status") != "completed":
            continue
        root = worker_run_root / worker_run_id
        quality_path = root / "quality-report.csv"
        if quality_path.is_file():
            with quality_path.open(encoding="utf-8-sig", newline="") as source:
                for row in csv.DictReader(source):
                    quality_rows.append(
                        {
                            "batch_id": batch["batch_id"],
                            "worker_run_id": worker_run_id,
                            **row,
                        }
                    )
        review_path = root / "review-required.csv"
        if review_path.is_file():
            with review_path.open(encoding="utf-8-sig", newline="") as source:
                for row in csv.DictReader(source):
                    review_rows.append(
                        {
                            "batch_id": batch["batch_id"],
                            "worker_run_id": worker_run_id,
                            **row,
                        }
                    )
    if quality_rows:
        fields = list(quality_rows[0])
        _atomic_text(store.paths.quality, _csv_text(quality_rows, fields))
        warnings = [
            row
            for row in quality_rows
            if row.get("reason_codes")
            or row.get("decision") in {"accepted_with_warning", "manual_review_required", "failed"}
        ]
        _atomic_text(store.paths.warnings, _csv_text(warnings, fields))
    if review_rows:
        _atomic_text(store.paths.review, _csv_text(review_rows, list(review_rows[0])))
    else:
        _atomic_text(
            store.paths.review,
            "batch_id,worker_run_id,unit_key,property_id,address,mode,time_period,decision,reason_codes,error_type,error_message\n",
        )


def _validate_resume_source(manifest: dict[str, Any]) -> None:
    source = Path(manifest["source_properties_path"])
    if not source.is_file() or _fingerprint(source) != manifest["source_properties_sha256"]:
        raise AccessibilityInputError(
            "Source property CSV changed or is missing; resume refused"
        )


def execute_batches(
    store: AccessibilityBatchStore,
    *,
    resume: bool,
) -> dict[str, Any]:
    """Execute or resume pending worker batches, stopping safely on failure."""

    manifest = store.read()
    _validate_resume_source(manifest)
    config = load_worker_config(Path(manifest["config_path"]))
    worker_run_root = config.run_root
    for index, batch in enumerate(manifest["batches"], start=1):
        if batch["status"] == "completed":
            continue
        discovered = _discover_worker_run(worker_run_root, manifest, batch) if resume else None
        if discovered and discovered.get("status") in {
            "completed",
            "completed_with_warnings",
        }:
            _apply_worker_result(batch, discovered, worker_run_root)
            store.write(manifest)
            continue
        worker_run_id = (
            batch.get("worker_run_id")
            or (discovered or {}).get("run_id")
        )
        batch.update(
            {
                "status": "running",
                "started_at": batch.get("started_at") or _utc_text(),
                "worker_run_id": worker_run_id,
                "error_type": None,
                "error_message": None,
            }
        )
        store.write(manifest)
        LOGGER.info(
            "Running %s (%d/%d) with %d properties",
            batch["batch_id"],
            index,
            len(manifest["batches"]),
            batch["property_count"],
        )
        started = time.perf_counter()
        try:
            if resume and worker_run_id:
                result = worker_cli._run(
                    _resume_arguments(manifest, worker_run_id), resume=True
                )
            else:
                result = worker_cli._run(
                    _worker_arguments(manifest, batch, dry_run=False), resume=False
                )
            _apply_worker_result(batch, result, worker_run_root)
            batch["duration_seconds"] = round(time.perf_counter() - started, 3)
        except BaseException as error:
            discovered = _discover_worker_run(worker_run_root, manifest, batch)
            batch.update(
                {
                    "status": "failed",
                    "worker_run_id": (discovered or {}).get("run_id") or worker_run_id,
                    "completed_at": _utc_text(),
                    "duration_seconds": round(time.perf_counter() - started, 3),
                    "error_type": type(error).__name__,
                    "error_message": _safe_error(error),
                }
            )
            store.write(manifest)
            if not manifest["continue_on_failure"]:
                break
        store.write(manifest)

    manifest["aggregate"] = _aggregate(manifest)
    failed = [batch for batch in manifest["batches"] if batch["status"] == "failed"]
    incomplete = [batch for batch in manifest["batches"] if batch["status"] != "completed"]
    if failed:
        manifest["status"] = "failed"
    elif incomplete:
        manifest["status"] = "interrupted"
    elif manifest["aggregate"]["quality_warnings"]:
        manifest["status"] = "completed_with_warnings"
    else:
        manifest["status"] = "completed"
    if not incomplete:
        manifest["completed_at"] = _utc_text()
    store.write(manifest)
    _combine_reports(store, manifest, worker_run_root)
    return manifest


def dry_run_batches(
    *,
    properties_path: Path,
    config_path: Path,
    hotspot_ids: list[str],
    modes: list[str],
    batch_size: int,
) -> dict[str, Any]:
    """Estimate an arbitrary reviewed population through bounded worker dry-runs."""

    property_ids = load_property_ids(properties_path)
    batches = build_batch_plan(property_ids, batch_size)
    manifest = {
        "config_path": str(config_path),
        "properties_snapshot": str(properties_path),
        "hotspot_ids": hotspot_ids,
        "modes": modes,
    }
    aggregate: dict[str, int] = {name: 0 for name in METRIC_FIELDS}
    for batch in batches:
        result = worker_cli._run(
            _worker_arguments(manifest, batch, dry_run=True), resume=False
        )
        estimate = result["estimate"]
        batch["status"] = "estimated"
        batch["estimate"] = estimate
        for name, value in estimate.items():
            aggregate[name] = aggregate.get(name, 0) + int(value or 0)
    return {
        "schema_version": 1,
        "status": "dry_run",
        "properties_path": str(properties_path),
        "properties_sha256": _fingerprint(properties_path),
        "property_count": len(property_ids),
        "batch_size": batch_size,
        "batch_count": len(batches),
        "profile_count": len(property_ids)
        * len(hotspot_ids)
        * sum(6 if mode == "transit" else 1 for mode in modes),
        "hotspot_ids": hotspot_ids,
        "modes": modes,
        "batches": batches,
        "estimate": aggregate,
        "provider_calls_made": 0,
        "database_writes_made": 0,
    }


def _summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        key: manifest.get(key)
        for key in (
            "batch_run_id",
            "status",
            "started_at",
            "completed_at",
            "property_count",
            "batch_count",
            "batch_size",
            "aggregate",
            "batches",
        )
    }


def _csv_values(value: str) -> list[str]:
    output = [item.strip() for item in value.split(",") if item.strip()]
    if not output:
        raise AccessibilityInputError("At least one value is required")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_execution(command: argparse.ArgumentParser) -> None:
        command.add_argument("--properties", required=True)
        command.add_argument("--config", default=str(DEFAULT_CONFIG))
        command.add_argument("--hotspots", default="western-main-campus")
        command.add_argument("--modes", default="walking,cycling,transit")
        command.add_argument("--batch-size", type=int, default=MAX_BATCH_SIZE)

    run = subparsers.add_parser("run")
    add_execution(run)
    run.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    run.add_argument("--continue-on-failure", action="store_true")

    dry_run = subparsers.add_parser("dry-run")
    add_execution(dry_run)
    dry_run.add_argument("--output")

    resume = subparsers.add_parser("resume")
    resume.add_argument("--batch-run-id", required=True)
    resume.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))

    for name in ("status", "summary"):
        command = subparsers.add_parser(name)
        command.add_argument("--batch-run-id", required=True)
        command.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    try:
        if args.command in {"run", "dry-run"}:
            properties_path = Path(args.properties).resolve()
            config_path = Path(args.config).resolve()
            hotspot_ids = _csv_values(args.hotspots)
            modes = _csv_values(args.modes)
            if args.command == "dry-run":
                result = dry_run_batches(
                    properties_path=properties_path,
                    config_path=config_path,
                    hotspot_ids=hotspot_ids,
                    modes=modes,
                    batch_size=args.batch_size,
                )
                if args.output:
                    atomic_write_json(Path(args.output).resolve(), result)
            else:
                property_ids = load_property_ids(properties_path)
                store = AccessibilityBatchStore.create(
                    Path(args.run_root).resolve(),
                    properties_path=properties_path,
                    property_ids=property_ids,
                    config_path=config_path,
                    hotspot_ids=hotspot_ids,
                    modes=modes,
                    batch_size=args.batch_size,
                    continue_on_failure=args.continue_on_failure,
                )
                result = execute_batches(store, resume=False)
        else:
            store = AccessibilityBatchStore.open(
                Path(args.run_root).resolve(), args.batch_run_id
            )
            if args.command == "resume":
                result = execute_batches(store, resume=True)
            else:
                result = store.read()
        if args.command == "summary":
            result = _summary(result)
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return 1 if result.get("status") in {"failed", "interrupted"} else 0
    except (AccessibilityInputError, ValueError, RuntimeError, OSError) as error:
        LOGGER.error("Accessibility batch failed: %s", _safe_error(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
