"""Rebuild Stage 3 canonical data from reviewed rows and cached geocodes only."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

import pandas as pd

from pipeline.operator_summary import derive_current_metrics
from pipeline.run_approval import sha256_file
from pipeline.run_context import (
    RunContext,
    RunPaths,
    atomic_write_json,
    determine_run_completion,
    manifest_lock,
    utc_text,
    validate_run_id,
)
from scripts.apply_geocode_qc import evaluate_geocode


IDENTITY_FIELD = "listing_id"
SOURCE_IDENTIFIER_FIELDS = ("listing_url", "source_url", "item_page_link")
REQUIRED_GEOCODE_FIELDS = (
    "geocode_query",
    "latitude",
    "longitude",
    "geocode_status",
    "geocode_confidence",
    "geocode_match_type",
    "geocode_result_type",
    "geocode_formatted",
    "geocode_city",
    "geocode_postcode",
    "geocode_country_code",
    "geocode_error",
    "distance_to_western_km",
)
QC_FIELDS = ("map_ready", "geocode_quality_issue")
REBUILD_VERSION = 1


class CanonicalRebuildError(RuntimeError):
    """Current run artifacts cannot be joined into a trustworthy canonical file."""


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"", "none", "null", "nan", "nat"} else text


def _truth(value: Any) -> bool:
    return _clean(value).casefold() in {"true", "1", "yes", "y"}


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise CanonicalRebuildError(f"Required artifact is missing: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames:
                raise CanonicalRebuildError(f"CSV has no header: {path}")
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CanonicalRebuildError(f"Could not read CSV {path}: {exc}") from exc
    if any(None in row for row in rows):
        raise CanonicalRebuildError(f"CSV contains rows wider than its header: {path}")
    return list(reader.fieldnames), rows


def _index_rows(
    rows: Iterable[dict[str, str]], *, artifact: str
) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row_number, row in enumerate(rows, start=2):
        listing_id = _clean(row.get(IDENTITY_FIELD))
        if not listing_id:
            raise CanonicalRebuildError(
                f"{artifact} has a missing listing_id at CSV row {row_number}"
            )
        if listing_id in indexed:
            raise CanonicalRebuildError(
                f"{artifact} has duplicate listing_id {listing_id!r}"
            )
        indexed[listing_id] = row
    return indexed


def _csv_bytes(fieldnames: list[str], rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=fieldnames,
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _artifact_metadata(path: Path, row_count: int) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "row_count": row_count,
    }


def _input_fingerprint(
    reviewed_hash: str,
    geocoded_hash: str,
    review_queue_hash: str,
    *,
    confidence_threshold: float,
    allow_low_confidence: bool,
) -> str:
    payload = json.dumps(
        {
            "version": REBUILD_VERSION,
            "reviewed_sha256": reviewed_hash,
            "geocoded_sha256": geocoded_hash,
            "review_queue_sha256": review_queue_hash,
            "confidence_threshold": confidence_threshold,
            "allow_low_confidence": allow_low_confidence,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _review_index(
    paths: RunPaths,
    canonical_rows: list[dict[str, Any]],
    *,
    rebuilt_at_utc: str,
    validation_warnings: list[str],
) -> dict[str, Any]:
    _, reviewed_rows = _read_csv(paths.stage2_reviewed)
    _, ai_review_rows = _read_csv(paths.stage2_review_queue)

    def ids(rows: Iterable[dict[str, Any]]) -> list[str]:
        return [_clean(row.get(IDENTITY_FIELD)) for row in rows]

    unresolved = [
        row
        for row in reviewed_rows
        if _truth(row.get("needs_manual_review"))
        and not _truth(row.get("manual_reviewed"))
    ]
    geocode_review = [row for row in canonical_rows if not _truth(row.get("map_ready"))]
    missing_price = [row for row in canonical_rows if not _clean(row.get("price_monthly"))]
    return {
        "version": 1,
        "run_id": validate_run_id(paths.root.name),
        "generated_at_utc": rebuilt_at_utc,
        "identity_field": IDENTITY_FIELD,
        "categories": {
            "ai_review": {"count": len(ai_review_rows), "listing_ids": ids(ai_review_rows)},
            "unresolved_manual_review": {
                "count": len(unresolved),
                "listing_ids": ids(unresolved),
            },
            "geocoding_review": {
                "count": len(geocode_review),
                "listing_ids": ids(geocode_review),
            },
            "missing_monthly_price": {
                "count": len(missing_price),
                "listing_ids": ids(missing_price),
            },
            "not_map_ready": {
                "count": len(geocode_review),
                "listing_ids": ids(geocode_review),
            },
        },
        "canonical_rebuild_validation_warnings": validation_warnings,
    }


def rebuild_canonical(
    run_dir: Path,
    *,
    confidence_threshold: float | None = None,
    allow_low_confidence: bool | None = None,
) -> dict[str, Any]:
    """Rebuild canonical/review artifacts without running any external stage."""

    root = run_dir.resolve()
    paths = RunPaths(root)
    validate_run_id(root.name)
    with manifest_lock(paths.manifest):
        if not paths.manifest.is_file():
            raise CanonicalRebuildError(f"Run manifest is missing: {paths.manifest}")
        try:
            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CanonicalRebuildError("Run manifest is invalid") from exc
        if not isinstance(manifest, dict) or manifest.get("run_id") != root.name:
            raise CanonicalRebuildError("Run manifest identity does not match its directory")
        stages = manifest.get("stages")
        stage = stages.get("stage3_qc") if isinstance(stages, dict) else None
        if not isinstance(stage, dict):
            raise CanonicalRebuildError("Manifest is missing stage3_qc metadata")

        reviewed_fields, reviewed_rows = _read_csv(paths.stage2_reviewed)
        geocoded_fields, geocoded_rows = _read_csv(paths.stage3_geocoded)
        for artifact, fields in (
            ("stage2/reviewed.csv", reviewed_fields),
            ("stage3/geocoded.csv", geocoded_fields),
        ):
            if IDENTITY_FIELD not in fields:
                raise CanonicalRebuildError(f"{artifact} is missing listing_id column")
        missing_geocode_fields = sorted(set(REQUIRED_GEOCODE_FIELDS) - set(geocoded_fields))
        if missing_geocode_fields:
            raise CanonicalRebuildError(
                "stage3/geocoded.csv is missing geocoding columns: "
                + ", ".join(missing_geocode_fields)
            )
        if len(reviewed_rows) != len(geocoded_rows):
            raise CanonicalRebuildError(
                "Reviewed/geocoded row-count mismatch: "
                f"{len(reviewed_rows)} != {len(geocoded_rows)}"
            )

        reviewed_by_id = _index_rows(reviewed_rows, artifact="stage2/reviewed.csv")
        geocoded_by_id = _index_rows(geocoded_rows, artifact="stage3/geocoded.csv")
        reviewed_ids = set(reviewed_by_id)
        geocoded_ids = set(geocoded_by_id)
        if reviewed_ids != geocoded_ids:
            missing = sorted(reviewed_ids - geocoded_ids)
            extra = sorted(geocoded_ids - reviewed_ids)
            raise CanonicalRebuildError(
                "Unmatched listing IDs between reviewed and geocoded artifacts; "
                f"missing geocodes={missing[:10]}, unexpected geocodes={extra[:10]}"
            )

        for listing_id, reviewed in reviewed_by_id.items():
            geocoded = geocoded_by_id[listing_id]
            for field in SOURCE_IDENTIFIER_FIELDS:
                reviewed_value = _clean(reviewed.get(field))
                geocoded_value = _clean(geocoded.get(field))
                if reviewed_value and geocoded_value and reviewed_value != geocoded_value:
                    raise CanonicalRebuildError(
                        f"Conflicting {field} for listing_id {listing_id!r}"
                    )

        geocode_fields = [
            field
            for field in geocoded_fields
            if field in {"latitude", "longitude", "distance_to_western_km"}
            or field.startswith("geocode_")
        ]
        geocode_fields = [field for field in geocode_fields if field not in QC_FIELDS]
        output_fields = [
            field for field in reviewed_fields if field not in set(geocode_fields) | set(QC_FIELDS)
        ] + geocode_fields + list(QC_FIELDS)

        configuration = manifest.get("configuration", {})
        qc_configuration = (
            configuration.get("stage3_qc", {}) if isinstance(configuration, dict) else {}
        )
        if not isinstance(qc_configuration, dict):
            qc_configuration = {}
        selected_threshold = float(
            confidence_threshold
            if confidence_threshold is not None
            else qc_configuration.get("confidence_threshold", 0.8)
        )
        configured_allow_low = (
            allow_low_confidence
            if allow_low_confidence is not None
            else qc_configuration.get("allow_low_confidence", False)
        )
        selected_allow_low = (
            configured_allow_low
            if isinstance(configured_allow_low, bool)
            else _truth(configured_allow_low)
        )
        if not 0 <= selected_threshold <= 1:
            raise CanonicalRebuildError("Geocode confidence threshold must be between 0 and 1")

        canonical_rows: list[dict[str, Any]] = []
        issue_counts: dict[str, int] = {}
        for reviewed in reviewed_rows:
            listing_id = _clean(reviewed[IDENTITY_FIELD])
            geocoded = geocoded_by_id[listing_id]
            row: dict[str, Any] = {
                field: value
                for field, value in reviewed.items()
                if field not in set(geocode_fields) | set(QC_FIELDS)
            }
            row.update({field: geocoded.get(field, "") for field in geocode_fields})
            ready, issues = evaluate_geocode(
                pd.Series(row),
                confidence_threshold=selected_threshold,
                allow_low_confidence=selected_allow_low,
            )
            if _truth(row.get("geocode_manual_override")):
                latitude = pd.to_numeric(row.get("latitude"), errors="coerce")
                longitude = pd.to_numeric(row.get("longitude"), errors="coerce")
                override_reason = _clean(row.get("geocode_manual_override_reason"))
                if (
                    pd.notna(latitude)
                    and pd.notna(longitude)
                    and -90 <= float(latitude) <= 90
                    and -180 <= float(longitude) <= 180
                    and override_reason
                ):
                    ready, issues = True, []
                else:
                    ready = False
                    issues = [*issues, "invalid_manual_geocode_override"]
            row["map_ready"] = ready
            row["geocode_quality_issue"] = ";".join(issues)
            for issue in issues:
                issue_counts[issue] = issue_counts.get(issue, 0) + 1
            canonical_rows.append(row)
        review_rows = [row for row in canonical_rows if not row["map_ready"]]

        reviewed_hash = sha256_file(paths.stage2_reviewed)
        geocoded_hash = sha256_file(paths.stage3_geocoded)
        review_queue_hash = sha256_file(paths.stage2_review_queue)
        rebuild_fingerprint = _input_fingerprint(
            reviewed_hash,
            geocoded_hash,
            review_queue_hash,
            confidence_threshold=selected_threshold,
            allow_low_confidence=selected_allow_low,
        )
        canonical_content = _csv_bytes(output_fields, canonical_rows)
        review_content = _csv_bytes(output_fields, review_rows)
        canonical_hash = hashlib.sha256(canonical_content).hexdigest()
        review_hash = hashlib.sha256(review_content).hexdigest()
        histories = manifest.get("canonical_rebuild_history", [])
        if not isinstance(histories, list):
            histories = []
        latest = histories[-1] if histories and isinstance(histories[-1], dict) else {}
        unchanged = (
            latest.get("input_fingerprint") == rebuild_fingerprint
            and paths.stage3_canonical.is_file()
            and paths.stage3_geocode_review.is_file()
            and sha256_file(paths.stage3_canonical) == canonical_hash
            and sha256_file(paths.stage3_geocode_review) == review_hash
        )
        rebuilt_at = str(latest.get("at_utc")) if unchanged else utc_text()
        validation_warnings: list[str] = []
        review_index = _review_index(
            paths,
            canonical_rows,
            rebuilt_at_utc=rebuilt_at,
            validation_warnings=validation_warnings,
        )
        review_index_path = root / "review-index.json"

        if not unchanged:
            discrepancies_before = derive_current_metrics(root, manifest).get(
                "metric_discrepancies", []
            )
            previous_canonical_hash = (
                sha256_file(paths.stage3_canonical)
                if paths.stage3_canonical.is_file()
                else None
            )
            _atomic_write_bytes(paths.stage3_canonical, canonical_content)
            _atomic_write_bytes(paths.stage3_geocode_review, review_content)
            event = {
                "version": REBUILD_VERSION,
                "at_utc": rebuilt_at,
                "identity_field": IDENTITY_FIELD,
                "input_fingerprint": rebuild_fingerprint,
                "inputs": {
                    "stage2/reviewed.csv": {
                        "sha256": reviewed_hash,
                        "row_count": len(reviewed_rows),
                    },
                    "stage3/geocoded.csv": {
                        "sha256": geocoded_hash,
                        "row_count": len(geocoded_rows),
                    },
                    "stage2/review_queue.csv": {
                        "sha256": review_queue_hash,
                        "row_count": review_index["categories"]["ai_review"]["count"],
                    },
                },
                "previous_canonical_sha256": previous_canonical_hash,
                "historical_metric_discrepancies": discrepancies_before,
                "configuration": {
                    "confidence_threshold": selected_threshold,
                    "allow_low_confidence": selected_allow_low,
                },
                "outputs": {
                    "stage3/canonical.csv": {
                        "sha256": canonical_hash,
                        "row_count": len(canonical_rows),
                    },
                    "stage3/geocode_review.csv": {
                        "sha256": review_hash,
                        "row_count": len(review_rows),
                    },
                },
                "validation_warnings": validation_warnings,
            }
            histories.append(event)
            manifest["canonical_rebuild_history"] = histories
        atomic_write_json(review_index_path, review_index)

        metrics = {
            "map_ready_count": len(canonical_rows) - len(review_rows),
            "review_required_count": len(review_rows),
            "missing_address_count": sum(
                _clean(row.get("geocode_status")).casefold() == "missing_address"
                for row in canonical_rows
            ),
            "failed_geocode_count": sum(
                _clean(row.get("geocode_status")).casefold()
                in {"error", "not_found", "cache_miss"}
                for row in canonical_rows
            ),
            "low_confidence_count": issue_counts.get("low_confidence", 0),
        }
        stage["input_rows"] = len(canonical_rows)
        stage["output_rows"] = len(canonical_rows)
        stage["metrics"] = metrics
        stage["output_metadata"] = {
            "stage3/canonical.csv": _artifact_metadata(
                paths.stage3_canonical, len(canonical_rows)
            ),
            "stage3/geocode_review.csv": _artifact_metadata(
                paths.stage3_geocode_review, len(review_rows)
            ),
            "review-index.json": _artifact_metadata(review_index_path, 1),
        }
        stage["canonical_rebuild"] = {
            "version": REBUILD_VERSION,
            "at_utc": rebuilt_at,
            "input_fingerprint": rebuild_fingerprint,
            "idempotent": unchanged,
        }
        manifest["canonical_for_import"] = False
        context = RunContext(paths, manifest)
        status, completion_warnings = determine_run_completion(context)
        manifest["status"] = status
        manifest["completion_warnings"] = completion_warnings
        manifest["updated_at_utc"] = utc_text()
        atomic_write_json(paths.manifest, manifest)

    return {
        "ok": True,
        "run_id": root.name,
        "idempotent": unchanged,
        "row_count": len(canonical_rows),
        "map_ready_count": metrics["map_ready_count"],
        "geocode_review_rows": len(review_rows),
        "canonical_sha256": canonical_hash,
        "geocode_review_sha256": review_hash,
        "review_index": str(review_index_path),
        "run_status": status,
        "external_services_used": False,
    }
