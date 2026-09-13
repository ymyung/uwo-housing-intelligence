"""Atomic, resumable run artifacts for bounded accessibility work."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from backend.accessibility_inputs import ReviewedProperty, VerifiedHotspot
from backend.accessibility_worker import WorkerOutcome
from pipeline.run_context import atomic_write_json, generate_run_id, get_git_metadata, validate_run_id


def _utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (2**attempt))
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _csv_text(fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> str:
    import io

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name) for name in fieldnames})
    return output.getvalue()


@dataclass(frozen=True)
class AccessibilityRunPaths:
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def selected_properties(self) -> Path:
        return self.root / "selected-properties.csv"

    @property
    def selected_hotspots(self) -> Path:
        return self.root / "selected-hotspots.json"

    @property
    def route_results(self) -> Path:
        return self.root / "route-results.jsonl"

    @property
    def quality_json(self) -> Path:
        return self.root / "quality-report.json"

    @property
    def quality_csv(self) -> Path:
        return self.root / "quality-report.csv"

    @property
    def review_csv(self) -> Path:
        return self.root / "review-required.csv"


class AccessibilityRunStore:
    def __init__(self, run_root: Path, run_id: str) -> None:
        self.run_id = validate_run_id(run_id)
        self.paths = AccessibilityRunPaths(run_root / self.run_id)

    @classmethod
    def create(
        cls,
        run_root: Path,
        *,
        properties: list[ReviewedProperty],
        hotspots: list[VerifiedHotspot],
        modes: list[str],
        provider: str,
        provider_profile: str,
        graph_metadata: dict[str, str],
        input_fingerprints: dict[str, str],
        config_path: Path,
        properties_path: Path,
    ) -> "AccessibilityRunStore":
        git_commit, git_dirty = get_git_metadata()
        run_id = generate_run_id(git_commit=git_commit)
        store = cls(run_root, run_id)
        store.paths.root.mkdir(parents=True, exist_ok=False)
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "git_commit": git_commit,
            "git_dirty": git_dirty,
            "started_at": _utc_text(),
            "completed_at": None,
            "status": "running",
            "selected_property_count": len(properties),
            "selected_hotspot_count": len(hotspots),
            "requested_modes": modes,
            "routing_provider": provider,
            "provider_profile": provider_profile,
            **graph_metadata,
            "input_fingerprints": input_fingerprints,
            "config_path": str(config_path.resolve()),
            "properties_path": str(properties_path.resolve()),
            "completed_unit_keys": [],
            "metrics": {},
            "success_count": 0,
            "failure_count": 0,
            "quality_warnings": 0,
        }
        atomic_write_json(store.paths.manifest, manifest)
        _atomic_text(
            store.paths.selected_properties,
            _csv_text(
                [
                    "property_id",
                    "normalized_address",
                    "latitude",
                    "longitude",
                    "review_status",
                    "origin_fingerprint",
                ],
                (value.to_dict() for value in properties),
            ),
        )
        atomic_write_json(
            store.paths.selected_hotspots,
            {"schema_version": 1, "hotspots": [value.to_dict() for value in hotspots]},
        )
        _atomic_text(store.paths.route_results, "")
        return store

    @classmethod
    def open(cls, run_root: Path, run_id: str) -> "AccessibilityRunStore":
        store = cls(run_root, run_id)
        if not store.paths.manifest.is_file():
            raise ValueError(f"Unknown accessibility run: {run_id}")
        return store

    def manifest(self) -> dict[str, Any]:
        try:
            value = json.loads(self.paths.manifest.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Accessibility run manifest is invalid: {self.run_id}") from exc
        if not isinstance(value, dict) or value.get("run_id") != self.run_id:
            raise ValueError(f"Accessibility run manifest identity mismatch: {self.run_id}")
        return value

    def outcomes(self) -> list[dict[str, Any]]:
        if not self.paths.route_results.exists():
            return []
        output: list[dict[str, Any]] = []
        for line_number, line in enumerate(
            self.paths.route_results.read_text(encoding="utf-8-sig").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid route result JSONL at line {line_number}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError("Route result JSONL must contain objects")
            output.append(value)
        return output

    def record(self, outcome: WorkerOutcome, metrics: dict[str, int]) -> None:
        rows = self.outcomes()
        if any(row.get("unit_key") == outcome.unit_key for row in rows):
            return
        serialized = outcome.to_dict()
        # Keep the counter snapshot beside the durable outcome. If Windows
        # briefly locks the manifest after the JSONL replacement succeeds,
        # resume can recover exact counters without repeating provider work.
        serialized["metrics_after_unit"] = dict(metrics)
        rows.append(serialized)
        _atomic_text(
            self.paths.route_results,
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        )
        manifest = self.manifest()
        manifest["completed_unit_keys"] = [row["unit_key"] for row in rows]
        manifest["metrics"] = metrics
        manifest["success_count"] = sum(row.get("action") != "failed" for row in rows)
        manifest["failure_count"] = sum(row.get("action") == "failed" for row in rows)
        manifest["quality_warnings"] = sum(
            row.get("quality", {}).get("decision")
            in {"accepted_with_warning", "manual_review_required", "failed"}
            for row in rows
        )
        atomic_write_json(self.paths.manifest, manifest)

    def finalize(self, *, interrupted: bool = False) -> dict[str, Any]:
        rows = self.outcomes()
        quality_rows = [
            {
                "property_id": row.get("property_id"),
                "address": row.get("address"),
                "hotspot": row.get("hotspot_name"),
                "mode": row.get("mode"),
                "time_period": row.get("time_period"),
                "representative_duration_seconds": (row.get("profile") or {}).get(
                    "representative_duration_seconds"
                ),
                "minimum_duration_seconds": (row.get("profile") or {}).get(
                    "minimum_duration_seconds"
                ),
                "maximum_duration_seconds": (row.get("profile") or {}).get(
                    "maximum_duration_seconds"
                ),
                "distance_meters": (row.get("profile") or {}).get("distance_meters"),
                "transfer_count": (row.get("profile") or {}).get("transfer_count"),
                "walking_duration_seconds": (row.get("profile") or {}).get(
                    "walking_duration_seconds"
                ),
                "quality_status": row.get("quality", {}).get("quality_status"),
                "decision": row.get("quality", {}).get("decision"),
                "reason_codes": ",".join(row.get("quality", {}).get("reason_codes") or []),
                "provider": (row.get("profile") or {}).get("provider"),
                "network_version": (row.get("profile") or {}).get("network_version"),
                "schedule_version": (row.get("profile") or {}).get("schedule_version"),
            }
            for row in rows
        ]
        review = [
            row
            for row in quality_rows
            if row["decision"] in {"manual_review_required", "failed"}
        ]
        fields = list(quality_rows[0]) if quality_rows else [
            "property_id", "address", "hotspot", "mode", "time_period",
            "representative_duration_seconds", "minimum_duration_seconds",
            "maximum_duration_seconds", "distance_meters", "transfer_count",
            "walking_duration_seconds", "quality_status", "decision",
            "reason_codes", "provider", "network_version", "schedule_version",
        ]
        atomic_write_json(
            self.paths.quality_json,
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "profile_count": len(quality_rows),
                "review_count": len(review),
                "results": quality_rows,
            },
        )
        _atomic_text(self.paths.quality_csv, _csv_text(fields, quality_rows))
        _atomic_text(self.paths.review_csv, _csv_text(fields, review))
        manifest = self.manifest()
        manifest["completed_unit_keys"] = [row["unit_key"] for row in rows]
        latest_metrics = (rows[-1].get("metrics_after_unit") if rows else None)
        if isinstance(latest_metrics, dict):
            manifest["metrics"] = latest_metrics
        manifest["success_count"] = sum(row.get("action") != "failed" for row in rows)
        manifest["failure_count"] = sum(row.get("action") == "failed" for row in rows)
        manifest["quality_warnings"] = sum(
            row.get("quality", {}).get("decision")
            in {"accepted_with_warning", "manual_review_required", "failed"}
            for row in rows
        )
        manifest["completed_at"] = _utc_text()
        if interrupted:
            manifest["status"] = "interrupted"
        elif manifest.get("failure_count"):
            manifest["status"] = "completed_with_failures"
        elif manifest.get("quality_warnings"):
            manifest["status"] = "completed_with_warnings"
        else:
            manifest["status"] = "completed"
        atomic_write_json(self.paths.manifest, manifest)
        return manifest
