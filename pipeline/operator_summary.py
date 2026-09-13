"""Current-state metrics for operator reports, derived from stage artifacts."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Optional


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"", "none", "null", "nan", "nat"} else text


def _truth(value: Any) -> bool:
    return _clean(value).casefold() in {"true", "1", "yes", "y"}


def _read_csv(path: Path) -> Optional[tuple[list[str], list[dict[str, str]]]]:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                return None
            return list(reader.fieldnames), [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error):
        return None


def _stage_metric(
    manifest: dict[str, Any], stage_name: str, metric_name: str
) -> Optional[int]:
    stages = manifest.get("stages", {})
    stage = stages.get(stage_name, {}) if isinstance(stages, dict) else {}
    metrics = stage.get("metrics", {}) if isinstance(stage, dict) else {}
    value = metrics.get(metric_name) if isinstance(metrics, dict) else None
    return int(value) if isinstance(value, (int, float)) else None


def derive_current_metrics(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Derive review/error counts from their most stage-specific current files."""

    reviewed = _read_csv(root / "stage2" / "reviewed.csv")
    review_queue = _read_csv(root / "stage2" / "review_queue.csv")
    geocoded = _read_csv(root / "stage3" / "geocoded.csv")
    geocode_review = _read_csv(root / "stage3" / "geocode_review.csv")
    canonical = _read_csv(root / "stage3" / "canonical.csv")
    discrepancies: list[dict[str, Any]] = []
    sources: dict[str, str] = {}

    def choose(
        name: str,
        manifest_value: Optional[int],
        artifact_value: Optional[int],
        artifact_name: str,
    ) -> Optional[int]:
        if artifact_value is not None:
            sources[name] = artifact_name
            if manifest_value is not None and manifest_value != artifact_value:
                discrepancies.append(
                    {
                        "metric": name,
                        "manifest_value": manifest_value,
                        "artifact_value": artifact_value,
                        "used_source": artifact_name,
                    }
                )
            return artifact_value
        sources[name] = "manifest"
        return manifest_value

    reviewed_fields, reviewed_rows = reviewed or ([], [])
    reviewed_ai_errors = (
        sum(bool(_clean(row.get("ai_error"))) for row in reviewed_rows)
        if reviewed is not None
        else None
    )
    ai_errors = choose(
        "ai_errors",
        _stage_metric(manifest, "stage2", "ai_error_count"),
        reviewed_ai_errors,
        "stage2/reviewed.csv",
    )
    queue_count = len(review_queue[1]) if review_queue is not None else None
    ai_review_rows = choose(
        "ai_review_rows",
        _stage_metric(manifest, "stage2", "review_count"),
        queue_count,
        "stage2/review_queue.csv",
    )

    manual_review_rows: Optional[int] = None
    if reviewed is not None and "needs_manual_review" in reviewed_fields:
        manual_review_rows = sum(
            _truth(row.get("needs_manual_review"))
            and (
                "manual_reviewed" not in reviewed_fields
                or not _truth(row.get("manual_reviewed"))
            )
            for row in reviewed_rows
        )
        sources["manual_review_rows"] = "stage2/reviewed.csv"
    else:
        manual_review_rows = _stage_metric(manifest, "manual_fixes", "review_count")
        sources["manual_review_rows"] = "manifest"

    geocoded_fields, geocoded_rows = geocoded or ([], [])
    geocoded_failures = (
        sum(
            _clean(row.get("geocode_status")).casefold()
            in {"error", "not_found", "cache_miss"}
            for row in geocoded_rows
        )
        if geocoded is not None and "geocode_status" in geocoded_fields
        else None
    )
    geocode_failures = choose(
        "geocode_failures",
        _stage_metric(manifest, "stage3", "failed_geocode_count"),
        geocoded_failures,
        "stage3/geocoded.csv",
    )
    geocode_review_rows = choose(
        "geocode_review_rows",
        _stage_metric(manifest, "stage3_qc", "review_required_count"),
        len(geocode_review[1]) if geocode_review is not None else None,
        "stage3/geocode_review.csv",
    )

    if canonical is not None:
        canonical_fields, canonical_rows = canonical
        comparisons = {
            "ai_errors": (
                sum(bool(_clean(row.get("ai_error"))) for row in canonical_rows)
                if "ai_error" in canonical_fields
                else 0
            ),
            "ai_review_rows": (
                sum(_truth(row.get("needs_manual_review")) for row in canonical_rows)
                if "needs_manual_review" in canonical_fields
                else None
            ),
        }
        selected = {"ai_errors": ai_errors, "ai_review_rows": ai_review_rows}
        for metric, canonical_value in comparisons.items():
            if canonical_value is not None and canonical_value != selected[metric]:
                discrepancies.append(
                    {
                        "metric": metric,
                        "artifact": "stage3/canonical.csv",
                        "artifact_value": canonical_value,
                        "selected_value": selected[metric],
                        "used_source": sources[metric],
                    }
                )

    return {
        "ai_errors": ai_errors,
        "ai_review_rows": ai_review_rows,
        "manual_review_rows": manual_review_rows,
        "geocode_failures": geocode_failures,
        "geocode_review_rows": geocode_review_rows,
        "metric_sources": sources,
        "metric_discrepancies": discrepancies,
    }
