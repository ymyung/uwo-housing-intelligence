"""Explicit, auditable approval for completed Stage 0-to-Stage 3 runs."""

from __future__ import annotations

import copy
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from pipeline.run_context import (
    COMPLETED,
    COMPLETED_WITH_WARNINGS,
    REQUIRED_STAGES,
    SKIPPED,
    RunContext,
    RunPaths,
    atomic_write_json,
    determine_run_completion,
    manifest_lock,
    utc_text,
)


APPROVED = "approved"
UNAPPROVED = "unapproved"
APPROVAL_VERSION = 1
APPROVAL_MANIFEST_EXCLUDED_FIELDS = frozenset(
    {"approval", "canonical_for_import", "updated_at_utc"}
)
WESTERN_PATH_RE = re.compile(r"^/Listings/Details/([1-9]\d*)/?$", re.IGNORECASE)
LISTING_ID_RE = re.compile(r"^[1-9]\d*$")
BLOCKING_DISCOVERY_WARNING_FRAGMENTS = (
    "returned zero",
    "below minimum",
    "fell substantially",
    "maximum page limit",
    "incomplete discovery",
)


class RunApprovalError(ValueError):
    """Approval cannot proceed safely."""


@dataclass(frozen=True)
class ApprovalSummary:
    discovered_listings: Optional[int] = None
    canonical_rows: Optional[int] = None
    stage1_failures: Optional[int] = None
    ai_review_rows: Optional[int] = None
    unresolved_manual_review_rows: Optional[int] = None
    ai_errors: Optional[int] = None
    missing_addresses: Optional[int] = None
    geocode_failures: Optional[int] = None
    low_confidence_geocodes: Optional[int] = None
    geocode_review_rows: Optional[int] = None
    missing_coordinates: Optional[int] = None
    map_ready_rows: Optional[int] = None
    not_map_ready_rows: Optional[int] = None
    suspicious_price_rows: Optional[int] = None
    missing_monthly_price_rows: Optional[int] = None
    sublet_review_rows: Optional[int] = None
    manifest_warnings: Optional[int] = None


@dataclass(frozen=True)
class ApprovalEvaluation:
    run_dir: Path
    run_id: Optional[str]
    run_status: Optional[str]
    approval_status: str
    canonical_for_import: bool
    approved_by: Optional[str]
    approved_at_utc: Optional[str]
    approval_version: Optional[int]
    warnings_acknowledged: Optional[bool]
    history_event_count: int
    canonical_fingerprint_valid: Optional[bool]
    manifest_fingerprint_valid: Optional[bool]
    manifest_file_fingerprint: Optional[str]
    canonical_csv_fingerprint: Optional[str]
    approval_manifest_fingerprint: Optional[str]
    stored_canonical_csv_fingerprint: Optional[str]
    stored_manifest_fingerprint: Optional[str]
    summary: ApprovalSummary
    blocking_conditions: tuple[str, ...]
    material_warning_conditions: tuple[str, ...]
    invalid_approval_reasons: tuple[str, ...]
    warnings: tuple[str, ...]


def canonical_json(value: Any) -> str:
    """Serialize JSON-compatible approval data deterministically."""

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def sha256_file(path: Path) -> str:
    """Return a SHA-256 fingerprint of exact file bytes."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def approval_manifest_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return manifest content covered by approval, excluding self-mutating fields."""

    return {
        key: copy.deepcopy(value)
        for key, value in manifest.items()
        if key not in APPROVAL_MANIFEST_EXCLUDED_FIELDS
    }


def approval_manifest_fingerprint(manifest: dict[str, Any]) -> str:
    """Fingerprint approval-relevant manifest content."""

    payload = approval_manifest_payload(manifest)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise RunApprovalError(f"CSV has no header: {path.name}")
        duplicates = {
            name for name in reader.fieldnames if reader.fieldnames.count(name) > 1
        }
        if duplicates:
            raise RunApprovalError(
                f"CSV contains duplicate columns: {sorted(duplicates)}"
            )
        return list(reader.fieldnames), [dict(row) for row in reader]


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.casefold() in {"none", "null", "nan", "nat"}:
        return None
    return text


def _bool(value: Any) -> Optional[bool]:
    text = (_clean(value) or "").casefold()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _float(value: Any) -> Optional[float]:
    text = _clean(value)
    if text is None:
        return None
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _western_source_id(value: Any) -> str:
    text = _clean(value)
    if not text:
        raise RunApprovalError("source listing URL is missing")
    parts = urlsplit(text)
    if (parts.hostname or "").casefold() not in {
        "offcampus.uwo.ca",
        "www.offcampus.uwo.ca",
    }:
        raise RunApprovalError("source listing URL has an unsupported host")
    match = WESTERN_PATH_RE.fullmatch(parts.path)
    if not match:
        raise RunApprovalError("source listing URL has an invalid path")
    return match.group(1)


def _manifest_warning_texts(manifest: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for warning in manifest.get("warnings", []):
        if isinstance(warning, dict):
            text = _clean(warning.get("message"))
        else:
            text = _clean(warning)
        if text:
            texts.append(text)
    return texts


def _metric_or_none(manifest: dict[str, Any], stage: str, key: str) -> Optional[int]:
    stages = manifest.get("stages", {})
    stages = stages if isinstance(stages, dict) else {}
    stage_data = stages.get(stage, {})
    stage_data = stage_data if isinstance(stage_data, dict) else {}
    metrics = stage_data.get("metrics", {})
    metrics = metrics if isinstance(metrics, dict) else {}
    value = metrics.get(key)
    return int(value) if isinstance(value, (int, float)) else None


def _approval_summary(
    manifest: Optional[dict[str, Any]],
    canonical_fields: set[str],
    canonical_rows: list[dict[str, str]],
    discovered_count: Optional[int],
) -> ApprovalSummary:
    if manifest is None:
        return ApprovalSummary(discovered_listings=discovered_count)

    def count_bool(field: str, expected: bool) -> Optional[int]:
        if field not in canonical_fields:
            return None
        return sum(_bool(row.get(field)) is expected for row in canonical_rows)

    def count_missing(field: str) -> Optional[int]:
        if field not in canonical_fields:
            return None
        return sum(_clean(row.get(field)) is None for row in canonical_rows)

    if "scraped_ok" in canonical_fields:
        stage1_failures = sum(
            _bool(row.get("scraped_ok")) is False for row in canonical_rows
        )
    else:
        value = manifest.get("stages", {}).get("stage1", {}).get("error_count")
        stage1_failures = int(value) if isinstance(value, (int, float)) else None

    ai_review_rows = count_bool("needs_manual_review", True)
    unresolved_manual: Optional[int] = None
    if "manual_reviewed" in canonical_fields:
        unresolved_manual = sum(
            _bool(row.get("needs_manual_review")) is True
            and _bool(row.get("manual_reviewed")) is not True
            for row in canonical_rows
        )
    elif "manual_review_status" in canonical_fields:
        unresolved_manual = sum(
            (_clean(row.get("manual_review_status")) or "").casefold()
            not in {"resolved", "completed", "approved"}
            for row in canonical_rows
        )

    if "ai_error" in canonical_fields:
        ai_errors = sum(_clean(row.get("ai_error")) is not None for row in canonical_rows)
    else:
        ai_errors = _metric_or_none(manifest, "stage2", "ai_error_count")
        if ai_errors is None:
            value = manifest.get("stages", {}).get("stage2", {}).get("error_count")
            ai_errors = int(value) if isinstance(value, (int, float)) else None

    geocode_failures: Optional[int] = None
    if "geocode_status" in canonical_fields:
        geocode_failures = sum(
            (_clean(row.get("geocode_status")) or "missing").casefold() != "ok"
            for row in canonical_rows
        )
    low_confidence: Optional[int] = None
    if "geocode_confidence" in canonical_fields:
        low_confidence = sum(
            (value := _float(row.get("geocode_confidence"))) is not None
            and value < 0.8
            for row in canonical_rows
        )
    geocode_review: Optional[int] = None
    geocode_review_fields = {
        "needs_geocode_review",
        "geocode_quality_issue",
        "geocode_status",
        "geocode_confidence",
    } & canonical_fields
    if geocode_review_fields:
        geocode_review = sum(
            _bool(row.get("needs_geocode_review")) is True
            or _clean(row.get("geocode_quality_issue")) is not None
            or (
                "geocode_status" in canonical_fields
                and (_clean(row.get("geocode_status")) or "missing").casefold()
                != "ok"
            )
            or (
                (value := _float(row.get("geocode_confidence"))) is not None
                and value < 0.8
            )
            for row in canonical_rows
        )

    suspicious_price: Optional[int] = None
    if "price_monthly" in canonical_fields:
        suspicious_price = sum(
            (value := _float(row.get("price_monthly"))) is not None
            and (value < 100 or value > 10000)
            for row in canonical_rows
        )
    sublet_review: Optional[int] = None
    relevant_sublet_fields = {
        "review_flags",
        "consensus_disagreement_is_sublet",
        "is_sublet_ai_evidence_blocked",
    } & canonical_fields
    if relevant_sublet_fields:
        sublet_review = sum(
            "is_sublet" in (_clean(row.get("review_flags")) or "")
            or _bool(row.get("consensus_disagreement_is_sublet")) is True
            or _bool(row.get("is_sublet_ai_evidence_blocked")) is True
            for row in canonical_rows
        )

    return ApprovalSummary(
        discovered_listings=discovered_count,
        canonical_rows=len(canonical_rows),
        stage1_failures=stage1_failures,
        ai_review_rows=ai_review_rows,
        unresolved_manual_review_rows=unresolved_manual,
        ai_errors=ai_errors,
        missing_addresses=count_missing("address"),
        geocode_failures=geocode_failures,
        low_confidence_geocodes=low_confidence,
        geocode_review_rows=geocode_review,
        missing_coordinates=(
            sum(
                _clean(row.get("latitude")) is None
                or _clean(row.get("longitude")) is None
                for row in canonical_rows
            )
            if {"latitude", "longitude"} <= canonical_fields
            else None
        ),
        map_ready_rows=count_bool("map_ready", True),
        not_map_ready_rows=count_bool("map_ready", False),
        suspicious_price_rows=suspicious_price,
        missing_monthly_price_rows=count_missing("price_monthly"),
        sublet_review_rows=sublet_review,
        manifest_warnings=(
            len(manifest.get("warnings", []))
            if isinstance(manifest.get("warnings", []), list)
            else None
        ),
    )


def _material_warning_conditions(
    summary: ApprovalSummary, manifest: dict[str, Any]
) -> tuple[str, ...]:
    """Return unresolved conditions that require an explicit human acknowledgement."""

    labels = {
        "stage1_failures": "Stage 1 scrape failures",
        "ai_review_rows": "AI manual-review rows",
        "unresolved_manual_review_rows": "unresolved manual-review rows",
        "ai_errors": "AI errors",
        "missing_addresses": "missing addresses",
        "geocode_failures": "geocoding failures",
        "low_confidence_geocodes": "low-confidence geocodes",
        "geocode_review_rows": "geocoding review rows",
        "missing_coordinates": "rows with missing coordinates",
        "suspicious_price_rows": "suspicious price rows",
        "missing_monthly_price_rows": "rows with missing monthly prices",
        "sublet_review_rows": "sublet review rows",
    }
    conditions = [
        f"{label}: {value}"
        for field_name, label in labels.items()
        if (value := getattr(summary, field_name)) is not None and value > 0
    ]
    if (
        summary.discovered_listings is not None
        and summary.canonical_rows is not None
        and summary.discovered_listings != summary.canonical_rows
    ):
        conditions.append(
            "discovery/canonical row difference: "
            f"{summary.discovered_listings} discovered, {summary.canonical_rows} canonical"
        )
    conditions.extend(
        f"manifest warning: {warning}" for warning in _manifest_warning_texts(manifest)
    )
    return tuple(dict.fromkeys(conditions))


def approval_validation_errors(
    manifest: dict[str, Any], canonical_path: Path
) -> tuple[str, ...]:
    """Return reasons the current manifest is not a valid active approval."""

    errors: list[str] = []
    approval = manifest.get("approval")
    if manifest.get("canonical_for_import") is not True:
        errors.append("canonical_for_import is not true")
    if not isinstance(approval, dict):
        return tuple(errors + ["approval metadata is missing"])
    if approval.get("status") != APPROVED:
        errors.append("approval status is not approved")
    if (
        type(approval.get("approval_version")) is not int
        or approval.get("approval_version") != APPROVAL_VERSION
    ):
        errors.append("approval version is missing or unsupported")
    for field_name in (
        "approved_at_utc",
        "approved_by",
        "manifest_fingerprint",
        "canonical_csv_fingerprint",
    ):
        if not _clean(approval.get(field_name)):
            errors.append(f"approval metadata lacks {field_name}")
    history = approval.get("history")
    if not isinstance(history, list):
        errors.append("approval history is not an array")
    elif not history:
        errors.append("approval history is empty")
    if not isinstance(approval.get("warnings_acknowledged"), bool):
        errors.append("approval warning acknowledgement is missing or invalid")
    approved_at = _clean(approval.get("approved_at_utc"))
    if approved_at:
        try:
            parsed_approval_time = datetime.fromisoformat(
                approved_at.replace("Z", "+00:00")
            )
        except ValueError:
            parsed_approval_time = None
        if (
            parsed_approval_time is None
            or parsed_approval_time.tzinfo is None
            or parsed_approval_time.utcoffset() != timezone.utc.utcoffset(None)
        ):
            errors.append("approved_at_utc is not a valid UTC timestamp")
    recorded_manifest = _clean(approval.get("manifest_fingerprint"))
    if recorded_manifest and recorded_manifest != approval_manifest_fingerprint(manifest):
        errors.append("approved manifest fingerprint does not match")
    recorded_canonical = _clean(approval.get("canonical_csv_fingerprint"))
    if not canonical_path.is_file():
        errors.append("approved canonical CSV is missing")
    elif recorded_canonical:
        try:
            current_canonical = sha256_file(canonical_path)
        except OSError:
            errors.append("approved canonical CSV cannot be read")
        else:
            if recorded_canonical != current_canonical:
                errors.append("approved canonical CSV fingerprint does not match")
    if canonical_path.is_file():
        try:
            fields, rows = _read_csv(canonical_path)
        except (OSError, UnicodeError, RunApprovalError):
            pass
        else:
            stages = manifest.get("stages", {})
            stage0 = stages.get("stage0", {}) if isinstance(stages, dict) else {}
            discovered = stage0.get("output_rows")
            discovered_count = discovered if isinstance(discovered, int) else None
            summary = _approval_summary(manifest, set(fields), rows, discovered_count)
            material = _material_warning_conditions(summary, manifest)
            if material and approval.get("warnings_acknowledged") is not True:
                errors.append("material warnings were not acknowledged")
            acknowledged = approval.get("acknowledged_warning_conditions")
            if not isinstance(acknowledged, list):
                errors.append("acknowledged warning conditions are missing or invalid")
            elif acknowledged != list(material):
                errors.append(
                    "acknowledged warning conditions do not match current warnings"
                )
            if isinstance(history, list) and history:
                latest_event = history[-1]
                if not isinstance(latest_event, dict):
                    errors.append("latest approval history event is invalid")
                elif approval.get("status") == APPROVED:
                    if latest_event.get("event") not in {"approved", "reapproved"}:
                        errors.append("latest approval history event is not an approval")
                    if (
                        type(latest_event.get("approval_version")) is not int
                        or latest_event.get("approval_version") != APPROVAL_VERSION
                    ):
                        errors.append("latest approval history version is unsupported")
                    if latest_event.get("warnings_acknowledged") != bool(material):
                        errors.append(
                            "latest approval history acknowledgement is inconsistent"
                        )
                    if latest_event.get("acknowledged_warning_conditions") != list(
                        material
                    ):
                        errors.append(
                            "latest approval history warnings do not match current warnings"
                        )
    return tuple(errors)


def evaluate_run_approval(run_dir: Path) -> ApprovalEvaluation:
    """Evaluate approval eligibility and current approval validity without writes."""

    root = run_dir.resolve()
    paths = RunPaths(root)
    blockers: list[str] = []
    warnings: list[str] = []
    manifest: Optional[dict[str, Any]] = None
    manifest_file_hash: Optional[str] = None
    manifest_approval_hash: Optional[str] = None
    canonical_hash: Optional[str] = None
    canonical_rows: list[dict[str, str]] = []
    canonical_fields: set[str] = set()
    discovered_count: Optional[int] = None

    if not root.is_dir():
        blockers.append("Run directory does not exist")
    elif not paths.manifest.is_file():
        blockers.append("manifest.json is missing")
    else:
        try:
            manifest_file_hash = sha256_file(paths.manifest)
            loaded = json.loads(paths.manifest.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("manifest root is not an object")
            manifest = loaded
            manifest_approval_hash = approval_manifest_fingerprint(manifest)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            blockers.append("manifest.json is invalid")

    run_id = _clean(manifest.get("run_id")) if manifest else None
    run_status = _clean(manifest.get("status")) if manifest else None
    approval = manifest.get("approval") if manifest else None
    approval_dict = approval if isinstance(approval, dict) else {}
    approval_status = _clean(approval_dict.get("status")) or UNAPPROVED
    canonical_for_import = bool(
        manifest and manifest.get("canonical_for_import") is True
    )

    if manifest is not None:
        if run_id != root.name:
            blockers.append("Manifest run_id does not match the selected directory")
        if run_status not in {COMPLETED, COMPLETED_WITH_WARNINGS}:
            blockers.append("Run status must be completed or completed_with_warnings")
        manifest_errors = manifest.get("errors", [])
        if not isinstance(manifest_errors, list):
            blockers.append("Manifest errors must be an array")
        elif manifest_errors:
            blockers.append("Manifest contains fatal errors")
        stages = manifest.get("stages")
        if not isinstance(stages, dict):
            blockers.append("Manifest stages must be an object")
            stages = {}
        else:
            context = RunContext(paths, manifest)
            derived_status, completion_issues = determine_run_completion(context)
            if derived_status not in {COMPLETED, COMPLETED_WITH_WARNINGS}:
                blockers.extend(completion_issues)
            elif derived_status == COMPLETED_WITH_WARNINGS:
                warnings.extend(completion_issues)
        for stage_name in REQUIRED_STAGES:
            stage = stages.get(stage_name)
            if not isinstance(stage, dict):
                blockers.append(f"Required stage record is missing: {stage_name}")
                continue
            status = stage.get("status")
            allowed = {COMPLETED, COMPLETED_WITH_WARNINGS}
            if stage_name in {"stage2", "manual_fixes"}:
                allowed.add(SKIPPED)
            if status not in allowed:
                blockers.append(
                    f"Required stage is not successfully terminal: {stage_name}"
                )
            if stage_name != "stage0" and stage.get("input_rows") is None:
                blockers.append(f"Manifest lacks {stage_name} input row count")
            if stage.get("output_rows") is None:
                blockers.append(f"Manifest lacks {stage_name} output row count")

        configuration = manifest.get("configuration", {})
        configuration = configuration if isinstance(configuration, dict) else {}
        stage0_configuration = configuration.get("stage0", {}) or {}
        stage1_configuration = configuration.get("stage1", {}) or {}
        if stage1_configuration.get("limit") is not None:
            blockers.append("Stage 1 used a row limit")
        max_pages = stage0_configuration.get("max_pages")
        if isinstance(max_pages, (int, float)) and max_pages <= 1:
            blockers.append("Stage 0 used the one-page smoke-test limit")

        warning_texts = _manifest_warning_texts(manifest)
        warnings.extend(warning_texts)
        combined_warnings = " ".join(warning_texts).casefold()
        for fragment in BLOCKING_DISCOVERY_WARNING_FRAGMENTS:
            if fragment in combined_warnings:
                blockers.append(
                    f"Discovery warning indicates incomplete discovery: {fragment}"
                )

    discovered_ids: set[str] = set()
    if root.is_dir() and not paths.stage0_listing_links.is_file():
        blockers.append("Stage 0 discovery CSV is missing")
    elif paths.stage0_listing_links.is_file():
        try:
            _, discovered_rows = _read_csv(paths.stage0_listing_links)
            duplicate_discovered: set[str] = set()
            for row_number, row in enumerate(discovered_rows, start=2):
                raw_url = row.get("item_page_link") or row.get("listing_url") or row.get("url")
                try:
                    source_id = _western_source_id(raw_url)
                except RunApprovalError as error:
                    blockers.append(f"Invalid Stage 0 identity at row {row_number}: {error}")
                    continue
                if source_id in discovered_ids:
                    duplicate_discovered.add(source_id)
                discovered_ids.add(source_id)
            if duplicate_discovered:
                blockers.append("Stage 0 discovery CSV contains duplicate source IDs")
            discovered_count = len(discovered_ids)
            if discovered_count == 0:
                blockers.append("Stage 0 discovered zero listings")
        except (OSError, UnicodeError, RunApprovalError):
            blockers.append("Stage 0 discovery CSV is invalid")

    if not paths.stage3_canonical.is_file():
        blockers.append("Stage 3 canonical CSV is missing")
    else:
        try:
            canonical_hash = sha256_file(paths.stage3_canonical)
            fieldnames, canonical_rows = _read_csv(paths.stage3_canonical)
            canonical_fields = set(fieldnames)
            if not canonical_rows:
                blockers.append("Canonical CSV contains zero listings")
            seen_ids: set[str] = set()
            for row_number, row in enumerate(canonical_rows, start=2):
                source_id = _clean(row.get("listing_id"))
                if not source_id or not LISTING_ID_RE.fullmatch(source_id):
                    blockers.append(
                        f"Canonical row {row_number} has a missing or invalid source listing ID"
                    )
                    continue
                if source_id in seen_ids:
                    blockers.append(
                        f"Canonical CSV contains duplicate source listing ID {source_id}"
                    )
                seen_ids.add(source_id)
                try:
                    url_source_id = _western_source_id(row.get("listing_url"))
                except RunApprovalError as error:
                    blockers.append(f"Invalid canonical identity at row {row_number}: {error}")
                    continue
                if url_source_id != source_id:
                    blockers.append(
                        f"Canonical row {row_number} listing ID does not match its URL"
                    )
                if discovered_ids and source_id not in discovered_ids:
                    blockers.append(
                        f"Canonical source listing ID {source_id} was not discovered in Stage 0"
                    )
        except (OSError, UnicodeError, RunApprovalError):
            blockers.append("Stage 3 canonical CSV is invalid")

    if manifest is not None:
        stages = manifest.get("stages", {})
        stage0 = stages.get("stage0", {}) if isinstance(stages, dict) else {}
        stage3 = stages.get("stage3", {}) if isinstance(stages, dict) else {}
        stage3_qc = stages.get("stage3_qc", {}) if isinstance(stages, dict) else {}
        if discovered_count is not None and stage0.get("output_rows") != discovered_count:
            blockers.append("Stage 0 identity count does not match manifest output rows")
        metrics = stage0.get("metrics", {}) or {}
        if discovered_count is not None and metrics.get("discovered_listing_count") != discovered_count:
            blockers.append("Stage 0 discovery metric does not match its CSV")
        maximum_reached = metrics.get("maximum_page_limit_reached")
        if not isinstance(maximum_reached, bool):
            blockers.append("Stage 0 lacks a trustworthy maximum-page completion metric")
        elif maximum_reached:
            blockers.append("Stage 0 reached its configured maximum page limit")
        if canonical_rows and stage3.get("output_rows") != len(canonical_rows):
            blockers.append("Canonical row count does not match Stage 3 output rows")
        if canonical_rows and stage3_qc.get("output_rows") != len(canonical_rows):
            blockers.append("Canonical row count does not match Stage 3 QC output rows")

    summary = _approval_summary(
        manifest, canonical_fields, canonical_rows, discovered_count
    )
    material_warning_conditions = (
        _material_warning_conditions(summary, manifest) if manifest else ()
    )
    canonical_valid: Optional[bool] = None
    manifest_valid: Optional[bool] = None
    invalid_approval_reasons: tuple[str, ...] = ()
    if manifest is not None and isinstance(approval, dict):
        recorded_canonical = _clean(approval.get("canonical_csv_fingerprint"))
        recorded_manifest = _clean(approval.get("manifest_fingerprint"))
        canonical_valid = (
            recorded_canonical == canonical_hash
            if recorded_canonical is not None and canonical_hash is not None
            else False
        )
        manifest_valid = (
            recorded_manifest == manifest_approval_hash
            if recorded_manifest is not None
            else False
        )
        invalid_approval_reasons = approval_validation_errors(
            manifest, paths.stage3_canonical
        )
        if approval.get("status") == APPROVED:
            warnings.extend(
                f"Current approval invalid: {error}"
                for error in invalid_approval_reasons
            )
    elif manifest is not None:
        invalid_approval_reasons = approval_validation_errors(
            manifest, paths.stage3_canonical
        )

    history = approval_dict.get("history")
    return ApprovalEvaluation(
        run_dir=root,
        run_id=run_id,
        run_status=run_status,
        approval_status=approval_status,
        canonical_for_import=canonical_for_import,
        approved_by=_clean(approval_dict.get("approved_by")),
        approved_at_utc=_clean(approval_dict.get("approved_at_utc")),
        approval_version=(
            approval_dict.get("approval_version")
            if type(approval_dict.get("approval_version")) is int
            else None
        ),
        warnings_acknowledged=(
            approval_dict.get("warnings_acknowledged")
            if isinstance(approval_dict.get("warnings_acknowledged"), bool)
            else None
        ),
        history_event_count=len(history) if isinstance(history, list) else 0,
        canonical_fingerprint_valid=canonical_valid,
        manifest_fingerprint_valid=manifest_valid,
        manifest_file_fingerprint=manifest_file_hash,
        canonical_csv_fingerprint=canonical_hash,
        approval_manifest_fingerprint=manifest_approval_hash,
        stored_canonical_csv_fingerprint=_clean(
            approval_dict.get("canonical_csv_fingerprint")
        ),
        stored_manifest_fingerprint=_clean(approval_dict.get("manifest_fingerprint")),
        summary=summary,
        blocking_conditions=tuple(dict.fromkeys(blockers)),
        material_warning_conditions=material_warning_conditions,
        invalid_approval_reasons=invalid_approval_reasons,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RunApprovalError("manifest.json is invalid") from error
    if not isinstance(manifest, dict):
        raise RunApprovalError("manifest.json is invalid")
    return manifest


def _event_timestamp(now: Optional[datetime]) -> str:
    selected = now
    if selected is None:
        return utc_text()
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise RunApprovalError("Approval audit timestamps must be timezone-aware")
    return utc_text(selected.astimezone(timezone.utc))


def approve_run(
    run_dir: Path,
    *,
    approved_by: str,
    note: Optional[str] = None,
    acknowledge_warnings: bool = False,
    expected_manifest_file_fingerprint: Optional[str] = None,
    expected_canonical_csv_fingerprint: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Approve or reapprove a run after fresh validation under a manifest lock."""

    reviewer = approved_by.strip()
    review_note = (note.strip() or None) if note else None
    if not reviewer:
        raise RunApprovalError("approved_by is required")
    root = run_dir.resolve()
    paths = RunPaths(root)
    with manifest_lock(paths.manifest):
        if not paths.manifest.is_file():
            raise RunApprovalError("manifest.json is missing")
        if not paths.stage3_canonical.is_file():
            raise RunApprovalError("Stage 3 canonical CSV is missing")
        current_manifest_file_hash = sha256_file(paths.manifest)
        if (
            expected_manifest_file_fingerprint is not None
            and current_manifest_file_hash != expected_manifest_file_fingerprint
        ):
            raise RunApprovalError("Manifest changed after approval review; review again")
        current_canonical_hash = sha256_file(paths.stage3_canonical)
        if (
            expected_canonical_csv_fingerprint is not None
            and current_canonical_hash != expected_canonical_csv_fingerprint
        ):
            raise RunApprovalError(
                "Canonical CSV changed after approval review; review again"
            )
        evaluation = evaluate_run_approval(root)
        if evaluation.blocking_conditions:
            raise RunApprovalError(
                "Run is not approval-eligible: "
                + "; ".join(evaluation.blocking_conditions)
            )
        if evaluation.material_warning_conditions and not acknowledge_warnings:
            raise RunApprovalError(
                "Material warnings require acknowledge_warnings=True: "
                + "; ".join(evaluation.material_warning_conditions)
            )
        warnings_were_acknowledged = bool(evaluation.material_warning_conditions)
        manifest = _load_manifest(paths.manifest)
        prior = manifest.get("approval")
        prior = prior if isinstance(prior, dict) else {}
        history = copy.deepcopy(prior.get("history", []))
        if not isinstance(history, list):
            history = []
        timestamp = _event_timestamp(now)
        previous_status = _clean(prior.get("status"))
        event_name = "reapproved" if history or previous_status else "approved"
        event = {
            "event": event_name,
            "approval_version": APPROVAL_VERSION,
            "at_utc": timestamp,
            "by": reviewer,
            "note": review_note,
            "warnings_acknowledged": warnings_were_acknowledged,
            "acknowledged_warning_conditions": list(
                evaluation.material_warning_conditions
            ),
            "previous_status": previous_status,
            "previous_manifest_fingerprint": prior.get("manifest_fingerprint"),
            "previous_canonical_csv_fingerprint": prior.get(
                "canonical_csv_fingerprint"
            ),
            "manifest_fingerprint": evaluation.approval_manifest_fingerprint,
            "canonical_csv_fingerprint": evaluation.canonical_csv_fingerprint,
        }
        history.append(event)
        manifest["canonical_for_import"] = True
        manifest["approval"] = {
            "approval_version": APPROVAL_VERSION,
            "status": APPROVED,
            "approved_at_utc": timestamp,
            "approved_by": reviewer,
            "note": review_note,
            "warnings_acknowledged": warnings_were_acknowledged,
            "acknowledged_warning_conditions": list(
                evaluation.material_warning_conditions
            ),
            "manifest_fingerprint": evaluation.approval_manifest_fingerprint,
            "canonical_csv_fingerprint": evaluation.canonical_csv_fingerprint,
            "previous_status": previous_status,
            "history": history,
        }
        manifest["updated_at_utc"] = timestamp
        if sha256_file(paths.manifest) != current_manifest_file_hash:
            raise RunApprovalError("Manifest changed during approval; review again")
        if (
            sha256_file(paths.stage3_canonical)
            != evaluation.canonical_csv_fingerprint
        ):
            raise RunApprovalError(
                "Canonical CSV changed during approval; review again"
            )
        atomic_write_json(paths.manifest, manifest)
        return manifest


def unapprove_run(
    run_dir: Path,
    *,
    unapproved_by: str,
    reason: str,
    expected_manifest_file_fingerprint: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Revoke approval without deleting prior audit metadata or run data."""

    reviewer = unapproved_by.strip()
    selected_reason = reason.strip()
    if not reviewer:
        raise RunApprovalError("unapproved_by is required")
    if not selected_reason:
        raise RunApprovalError("reason is required")
    paths = RunPaths(run_dir.resolve())
    with manifest_lock(paths.manifest):
        current_hash = sha256_file(paths.manifest)
        if (
            expected_manifest_file_fingerprint is not None
            and current_hash != expected_manifest_file_fingerprint
        ):
            raise RunApprovalError("Manifest changed before unapproval; inspect again")
        manifest = _load_manifest(paths.manifest)
        prior = manifest.get("approval")
        prior = prior if isinstance(prior, dict) else {}
        if prior.get("status") != APPROVED or manifest.get("canonical_for_import") is not True:
            raise RunApprovalError("Run is already unapproved")
        history = copy.deepcopy(prior.get("history", []))
        if not isinstance(history, list):
            history = []
        timestamp = _event_timestamp(now)
        history.append(
            {
                "event": UNAPPROVED,
                "at_utc": timestamp,
                "by": reviewer,
                "reason": selected_reason,
                "previous_status": prior.get("status"),
                "manifest_fingerprint": prior.get("manifest_fingerprint"),
                "canonical_csv_fingerprint": prior.get(
                    "canonical_csv_fingerprint"
                ),
            }
        )
        approval = copy.deepcopy(prior)
        approval.update(
            {
                "status": UNAPPROVED,
                "unapproved_at_utc": timestamp,
                "unapproved_by": reviewer,
                "reason": selected_reason,
                "previous_status": prior.get("status"),
                "history": history,
            }
        )
        manifest["canonical_for_import"] = False
        manifest["approval"] = approval
        manifest["updated_at_utc"] = timestamp
        atomic_write_json(paths.manifest, manifest)
        return manifest


def print_approval_evaluation(evaluation: ApprovalEvaluation) -> None:
    """Print a row-safe approval report without listing-level values."""

    def display(value: Any) -> str:
        return "unavailable" if value is None else str(value)

    print(f"run_id: {display(evaluation.run_id)}")
    print(f"run_status: {display(evaluation.run_status)}")
    print(f"approval_status: {evaluation.approval_status}")
    print(f"canonical_for_import: {evaluation.canonical_for_import}")
    print(f"approved_by: {display(evaluation.approved_by)}")
    print(f"approved_at: {display(evaluation.approved_at_utc)}")
    print(f"approval_version: {display(evaluation.approval_version)}")
    print(
        "warnings_acknowledged: "
        f"{display(evaluation.warnings_acknowledged)}"
    )
    print(f"approval_history_events: {evaluation.history_event_count}")
    print(
        "stored_canonical_csv_fingerprint: "
        f"{display(evaluation.stored_canonical_csv_fingerprint)}"
    )
    print(
        "current_canonical_csv_fingerprint: "
        f"{display(evaluation.canonical_csv_fingerprint)}"
    )
    print(
        "stored_manifest_fingerprint: "
        f"{display(evaluation.stored_manifest_fingerprint)}"
    )
    print(
        "canonical_fingerprint_valid: "
        f"{display(evaluation.canonical_fingerprint_valid)}"
    )
    print(
        "manifest_fingerprint_valid: "
        f"{display(evaluation.manifest_fingerprint_valid)}"
    )
    print("review_summary:")
    for field_name, value in asdict(evaluation.summary).items():
        print(f"  {field_name}: {display(value)}")
    print("blocking_conditions:")
    if evaluation.blocking_conditions:
        for condition in evaluation.blocking_conditions:
            print(f"  - {condition}")
    else:
        print("  - none")
    print("material_warning_conditions:")
    if evaluation.material_warning_conditions:
        for condition in evaluation.material_warning_conditions:
            print(f"  - {condition}")
    else:
        print("  - none")
    print("invalid_approval_reasons:")
    if evaluation.invalid_approval_reasons:
        for reason in evaluation.invalid_approval_reasons:
            print(f"  - {reason}")
    else:
        print("  - none")
    print("warnings:")
    if evaluation.warnings:
        for warning in evaluation.warnings:
            print(f"  - {warning}")
    else:
        print("  - none")
