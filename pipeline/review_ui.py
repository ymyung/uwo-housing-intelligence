"""Loopback-only local review dashboard and audited human-decision storage."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse
import webbrowser

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from pipeline.geocoder import normalize_address
from pipeline.review_workflow import (
    IMMUTABLE_FIELDS,
    REQUIRED_DECISION_FIELDS,
    ReviewWorkflowError,
    _canonical_json,
    _index,
    _read_csv,
    _read_jsonl,
    _write_jsonl,
    listing_fingerprint,
)
from pipeline.run_approval import sha256_file
from pipeline.run_context import RunContext, manifest_lock, validate_run_id


ASSET_ROOT = Path(__file__).resolve().parent / "review_ui_assets"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
WESTERN_COORDINATES = {"latitude": 43.0096, "longitude": -81.2737}
HUMAN_STATUSES = {
    "human_approved",
    "accepted_as_unknown",
    "excluded",
    "human_review_required",
}
COMPLETED_HUMAN_STATUSES = {
    "human_approved",
    "accepted_as_unknown",
    "excluded",
}
SAFE_BULK_UNKNOWN_REASONS = {
    "genuinely_missing",
    "period_ambiguous",
    "non_monthly_convertible",
    "legitimately_unknown",
}
SAFE_BULK_CURRENT_REASONS = {
    "current_manual_correction_preserved",
    "current_manual_correction_confirmed_by_explicit_text",
    "structured_website_value_preserved",
    "current_value_matches_deterministic_rule",
}
BOOLEAN_REVIEW_FIELDS = {
    "is_sublet",
    "furnished",
    "utilities_included",
    "parking_available",
    "laundry",
    "dishwasher",
    "air_conditioning",
}
STRUCTURED_FIELDS = (
    "listing_url",
    "title",
    "address",
    "price_text",
    "price_numeric",
    "price_period",
    "price_monthly",
    "housing_type",
    "bedrooms",
    "date_available",
    "availability_text",
    "available_from",
    "available_to",
    "availability_category",
    "lease_term_raw",
    "description",
    "amenities",
    "landlord_name",
    "landlord_phone",
)


class ReviewUIError(RuntimeError):
    """The selected review bundle or submitted decision is invalid."""


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _truth(value: Any) -> bool:
    return _clean(value).casefold() in {"true", "1", "yes", "y"}


def _safe_external_url(value: Any) -> Optional[str]:
    text = _clean(value)
    if not text:
        return None
    parsed = urlparse(text)
    return text if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc else None


def _number(value: Any) -> Optional[float]:
    text = _clean(value).replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_object(path: Path, *, artifact: str) -> dict[str, Any]:
    if not path.is_file():
        raise ReviewUIError(f"Required review artifact is missing: {artifact}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewUIError(f"Review artifact is invalid: {artifact}") from exc
    if not isinstance(value, dict):
        raise ReviewUIError(f"Review artifact must contain an object: {artifact}")
    return value


def resolve_run_directory(run_id: str, roots: Iterable[Path]) -> Path:
    """Resolve one safe run ID below configured roots without path traversal."""

    selected = validate_run_id(run_id)
    checked: list[Path] = []
    for root in roots:
        configured_root = root.resolve()
        checked.append(configured_root)
        candidate = (configured_root / selected).resolve()
        if candidate.parent != configured_root:
            raise ReviewUIError("Selected run escapes its configured review root")
        if candidate.is_dir() and (candidate / "manifest.json").is_file():
            return candidate
    locations = ", ".join(str(path) for path in checked)
    raise ReviewUIError(f"Run {selected!r} was not found under configured roots: {locations}")


def validate_bind_host(host: str, *, unsafe_development_bind: bool = False) -> str:
    selected = _clean(host)
    if not selected or any(character in selected for character in "\r\n\0"):
        raise ReviewUIError("Review UI host is blank or unsafe")
    if selected.casefold() not in LOOPBACK_HOSTS and not unsafe_development_bind:
        raise ReviewUIError(
            "Non-loopback review UI binding requires --unsafe-development-bind"
        )
    return selected


def _validate_bundle(run_dir: Path) -> dict[str, Any]:
    root = run_dir.resolve()
    validate_run_id(root.name)
    context = RunContext.resume(root)
    manifest = context.manifest
    if manifest.get("run_id") != root.name:
        raise ReviewUIError("Manifest run_id does not match the selected directory")
    required = {
        "stage2/reviewed.csv": context.paths.stage2_reviewed,
        "stage3/geocoded.csv": context.paths.stage3_geocoded,
        "stage3/canonical.csv": context.paths.stage3_canonical,
        "review/review-decisions.jsonl": root / "review" / "review-decisions.jsonl",
        "review/review-auto-summary.json": root / "review" / "review-auto-summary.json",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        raise ReviewUIError("Required review artifacts are missing: " + ", ".join(missing))

    summary = _json_object(
        required["review/review-auto-summary.json"],
        artifact="review/review-auto-summary.json",
    )
    if summary.get("run_id") != root.name:
        raise ReviewUIError("Review summary run_id does not match the selected run")
    canonical_sha = sha256_file(context.paths.stage3_canonical)
    if summary.get("canonical_sha256") != canonical_sha:
        raise ReviewUIError("Review summary fingerprint is stale; rerun review-auto")

    canonical = _index(
        _read_csv(context.paths.stage3_canonical)[1], artifact="stage3/canonical.csv"
    )
    reviewed = _index(
        _read_csv(context.paths.stage2_reviewed)[1], artifact="stage2/reviewed.csv"
    )
    geocoded = _index(
        _read_csv(context.paths.stage3_geocoded)[1], artifact="stage3/geocoded.csv"
    )
    if set(canonical) != set(reviewed) or set(canonical) != set(geocoded):
        raise ReviewUIError("Canonical, reviewed, and geocoded listing IDs do not match")

    decisions = _read_jsonl(required["review/review-decisions.jsonl"])
    for record in decisions:
        if record.get("run_id") != root.name:
            raise ReviewUIError("Review decisions contain a different run_id")
    return {
        "root": root,
        "context": context,
        "manifest": manifest,
        "summary": summary,
        "canonical_sha256": canonical_sha,
        "canonical": canonical,
        "reviewed": reviewed,
        "geocoded": geocoded,
        "decisions": decisions,
    }


def _is_human_record(record: dict[str, Any]) -> bool:
    return record.get("reviewer_type") == "human" and bool(
        _clean(record.get("base_decision_id"))
    )


def _active_human_records(
    decisions: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in decisions:
        if not _is_human_record(record) or record.get("decision_state") == "superseded":
            continue
        base_id = str(record["base_decision_id"])
        if base_id in result and result[base_id].get("decision_id") != record.get(
            "decision_id"
        ):
            raise ReviewUIError(
                f"Conflicting active human decisions exist for {base_id}"
            )
        result[base_id] = record
    return result


def _base_decisions(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    canonical = bundle["canonical"]
    for record in bundle["decisions"]:
        listing_id = _clean(record.get("listing_id"))
        if (
            _is_human_record(record)
            or record.get("decision_status") != "human_review_required"
            or listing_id not in canonical
            or record.get("input_fingerprint")
            != listing_fingerprint(canonical[listing_id])
        ):
            continue
        decision_id = _clean(record.get("decision_id"))
        if not decision_id:
            raise ReviewUIError("A current review decision has no decision_id")
        if decision_id in result:
            raise ReviewUIError(f"Duplicate current decision_id: {decision_id}")
        result[decision_id] = record
    return result


def _inside_london(latitude: Any, longitude: Any, config: dict[str, Any]) -> bool:
    lat = _number(latitude)
    lon = _number(longitude)
    return bool(
        lat is not None
        and lon is not None
        and float(config.get("london_min_latitude", 42.75))
        <= lat
        <= float(config.get("london_max_latitude", 43.25))
        and float(config.get("london_min_longitude", -81.55))
        <= lon
        <= float(config.get("london_max_longitude", -80.85))
    )


def _manual_corrections(row: dict[str, str]) -> dict[str, Any]:
    corrections: dict[str, Any] = {}
    for field, source in row.items():
        if field.endswith("_source") and "manual" in _clean(source).casefold():
            value_field = field.removesuffix("_source")
            corrections[value_field] = {
                "value": row.get(value_field),
                "source": source,
            }
    if _clean(row.get("manual_review_note")):
        corrections["review_note"] = row.get("manual_review_note")
    return corrections


def _history_for_listing(
    listing_id: str,
    decisions: list[dict[str, Any]],
    manifest: dict[str, Any],
    current_fingerprint: str,
) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    stale_human = False
    registry = (
        manifest.get("review_workflow", {}).get("applied_decisions", {})
        if isinstance(manifest.get("review_workflow"), dict)
        else {}
    )
    registry = registry if isinstance(registry, dict) else {}
    for record in decisions:
        if _clean(record.get("listing_id")) != listing_id:
            continue
        event = "human_decision" if _is_human_record(record) else "automated_decision"
        timeline.append(
            {
                "at_utc": record.get("reviewed_at_utc")
                or record.get("created_at_utc"),
                "event": event,
                "decision_id": record.get("decision_id"),
                "field": record.get("field"),
                "status": record.get("human_decision_status")
                or record.get("decision_status"),
                "reason": record.get("reason_code"),
                "reviewer": record.get("reviewer_name"),
                "state": record.get("decision_state", "active"),
            }
        )
        if _is_human_record(record) and record.get("input_fingerprint") != current_fingerprint:
            stale_human = True
            timeline.append(
                {
                    "at_utc": record.get("reviewed_at_utc")
                    or record.get("created_at_utc"),
                    "event": "fingerprint_invalidation",
                    "decision_id": record.get("decision_id"),
                    "status": "stale",
                }
            )
        application = registry.get(str(record.get("decision_id")))
        if isinstance(application, dict):
            timeline.append(
                {
                    "at_utc": application.get("applied_at_utc"),
                    "event": "application_result",
                    "decision_id": record.get("decision_id"),
                    "status": "applied",
                }
            )
    for rebuild in manifest.get("canonical_rebuild_history", []) or []:
        if isinstance(rebuild, dict):
            timeline.append(
                {
                    "at_utc": rebuild.get("at_utc"),
                    "event": "canonical_rebuild",
                    "status": "completed",
                    "fingerprint": rebuild.get("input_fingerprint"),
                }
            )
    reopened = [
        record
        for record in decisions
        if not _is_human_record(record)
        and record.get("listing_id") == listing_id
        and record.get("input_fingerprint") == current_fingerprint
        and record.get("decision_status") == "human_review_required"
    ]
    if stale_human and reopened:
        timeline.append(
            {
                "at_utc": max(
                    _clean(record.get("created_at_utc")) for record in reopened
                ),
                "event": "reopened_issue",
                "status": "human_review_required",
            }
        )
    return sorted(
        timeline,
        key=lambda item: (_clean(item.get("at_utc")), _clean(item.get("event"))),
    )


def _decision_state(
    base: dict[str, Any],
    human: Optional[dict[str, Any]],
    row: dict[str, str],
    registry: dict[str, Any],
) -> str:
    if human is None:
        return "unresolved"
    if human.get("input_fingerprint") != listing_fingerprint(row):
        return "stale"
    if str(human.get("decision_id")) in registry:
        return "applied"
    status = human.get("human_decision_status")
    if status in COMPLETED_HUMAN_STATUSES:
        return "decided"
    if status == "human_review_required":
        return "draft"
    return "conflict"


def load_dashboard(
    run_dir: Path,
    *,
    category: Optional[str] = None,
    field: Optional[str] = None,
    reason: Optional[str] = None,
    minimum_confidence: Optional[float] = None,
    maximum_confidence: Optional[float] = None,
    map_ready: Optional[bool] = None,
    missing_price: Optional[bool] = None,
    decision_state: Optional[str] = None,
    search: Optional[str] = None,
) -> dict[str, Any]:
    """Load, validate, group, and optionally filter one review bundle."""

    bundle = _validate_bundle(run_dir)
    manifest = bundle["manifest"]
    base_by_id = _base_decisions(bundle)
    active_human = _active_human_records(bundle["decisions"])
    registry = (
        manifest.get("review_workflow", {}).get("applied_decisions", {})
        if isinstance(manifest.get("review_workflow"), dict)
        else {}
    )
    registry = registry if isinstance(registry, dict) else {}
    issues_by_listing: dict[str, list[dict[str, Any]]] = {}
    for base_id, base in base_by_id.items():
        listing_id = str(base["listing_id"])
        human = active_human.get(base_id)
        issue = copy.deepcopy(base)
        issue["base_decision_id"] = base_id
        issue["human_decision"] = copy.deepcopy(human)
        issue["ui_state"] = _decision_state(
            base, human, bundle["canonical"][listing_id], registry
        )
        issues_by_listing.setdefault(listing_id, []).append(issue)

    # Keep historical/applied human decisions visible even after their issue closes.
    for base_id, human in active_human.items():
        listing_id = _clean(human.get("listing_id"))
        if listing_id not in bundle["canonical"] or base_id in base_by_id:
            continue
        issue = copy.deepcopy(human)
        issue["base_decision_id"] = base_id
        issue["human_decision"] = copy.deepcopy(human)
        issue["ui_state"] = (
            "applied" if str(human.get("decision_id")) in registry else "stale"
        )
        issues_by_listing.setdefault(listing_id, []).append(issue)

    config = bundle["summary"].get("configuration", {})
    listings: list[dict[str, Any]] = []
    for listing_id, issues in issues_by_listing.items():
        row = bundle["canonical"][listing_id]
        reviewed = bundle["reviewed"][listing_id]
        geocoded = bundle["geocoded"][listing_id]
        categories = sorted(
            {
                item
                for issue in issues
                for item in (issue.get("review_categories") or [])
            }
        )
        confidence = _number(row.get("geocode_confidence"))
        current_states = [str(issue["ui_state"]) for issue in issues]
        listing_complete = bool(current_states) and all(
            state in {"decided", "applied"} for state in current_states
        )
        geocode_reasons = sorted(
            {
                str(issue.get("reason_code"))
                for issue in issues
                if "geocoding_review" in (issue.get("review_categories") or [])
            }
        )
        cache_evidence = any(
            issue.get("reason_code") == "cached_geocode_verified"
            or issue.get("evidence", {}).get("cache_sha256")
            for issue in issues
            if isinstance(issue.get("evidence"), dict)
        )
        price_issue = next(
            (issue for issue in issues if issue.get("field") == "price_monthly"),
            None,
        )
        listing = {
            "listing_id": listing_id,
            "source_url": _safe_external_url(
                row.get("listing_url") or row.get("source_url")
            ),
            "title": row.get("title"),
            "address": row.get("address"),
            "review_categories": categories,
            "review_status": "complete" if listing_complete else "unresolved",
            "last_decision_timestamp": max(
                (
                    _clean(issue.get("human_decision", {}).get("reviewed_at_utc"))
                    for issue in issues
                    if isinstance(issue.get("human_decision"), dict)
                ),
                default="",
            ),
            "input_fingerprint": listing_fingerprint(row),
            "input_fingerprint_status": (
                "current"
                if all(issue["ui_state"] != "stale" for issue in issues)
                else "stale"
            ),
            "issues": sorted(
                issues,
                key=lambda issue: (
                    _clean(issue.get("field")),
                    _clean(issue.get("reason_code")),
                ),
            ),
            "description": row.get("description"),
            "structured_fields": {
                name: row.get(name) for name in STRUCTURED_FIELDS if name in row
            },
            "parsed_fields": {
                name: value for name, value in row.items() if name.endswith("_rule")
            },
            "current_reviewed_fields": reviewed,
            "manual_corrections": _manual_corrections(reviewed),
            "ai_fields": {
                name: value for name, value in reviewed.items() if name.startswith("ai_")
            },
            "pricing": {
                "price_text": row.get("price_text"),
                "price_numeric": row.get("price_numeric"),
                "price_period": row.get("price_period"),
                "price_monthly": row.get("price_monthly"),
                "classification": (
                    price_issue.get("evidence", {}).get("price_classification")
                    if price_issue and isinstance(price_issue.get("evidence"), dict)
                    else None
                ),
                "conversion_rule": (
                    price_issue.get("reason_code") if price_issue else None
                ),
                "conflicting_text": (
                    price_issue.get("conflicting_text") if price_issue else []
                ),
            },
            "availability": {
                "availability_text": row.get("availability_text"),
                "date_available": row.get("date_available"),
                "available_from": row.get("available_from"),
                "available_to": row.get("available_to"),
                "lease_term_raw": row.get("lease_term_raw"),
                "lease_term_months": row.get("lease_term_months"),
                "summer_availability": row.get("availability_category")
                == "summer_only",
                "is_sublet": row.get("is_sublet"),
                "sublet_evidence": row.get("ai_evidence_sublet"),
            },
            "geocoding": {
                "original_address": row.get("address_original") or row.get("address"),
                "normalized_address": normalize_address(row.get("address")),
                "geocode_query": row.get("geocode_query"),
                "provider": row.get("geocode_provider")
                or ("geoapify" if row.get("geocode_query") else None),
                "latitude": _number(row.get("latitude")),
                "longitude": _number(row.get("longitude")),
                "confidence": confidence,
                "result_type": row.get("geocode_result_type"),
                "match_type": row.get("geocode_match_type"),
                "formatted_address": row.get("geocode_formatted"),
                "review_reason": geocode_reasons,
                "quality_issue": row.get("geocode_quality_issue"),
                "cache_evidence_qualifies": bool(cache_evidence),
                "inside_london_bounds": _inside_london(
                    row.get("latitude"), row.get("longitude"), config
                ),
                "map_ready": _truth(row.get("map_ready")),
                "distance_to_western_km": _number(
                    row.get("distance_to_western_km")
                ),
            },
            "history": _history_for_listing(
                listing_id,
                bundle["decisions"],
                manifest,
                listing_fingerprint(row),
            ),
        }
        listings.append(listing)

    query = _clean(search).casefold()
    filtered: list[dict[str, Any]] = []
    for listing in listings:
        issues = listing["issues"]
        geo_confidence = listing["geocoding"]["confidence"]
        if category and category not in listing["review_categories"]:
            continue
        if field and not any(issue.get("field") == field for issue in issues):
            continue
        if reason and not any(issue.get("reason_code") == reason for issue in issues):
            continue
        if minimum_confidence is not None and (
            geo_confidence is None or geo_confidence < minimum_confidence
        ):
            continue
        if maximum_confidence is not None and (
            geo_confidence is None or geo_confidence > maximum_confidence
        ):
            continue
        if map_ready is not None and listing["geocoding"]["map_ready"] is not map_ready:
            continue
        if missing_price is not None and (
            not bool(_clean(listing["pricing"]["price_monthly"]))
        ) is not missing_price:
            continue
        if decision_state and not any(
            issue.get("ui_state") == decision_state for issue in issues
        ):
            continue
        if query and query not in " ".join(
            _clean(listing.get(name)).casefold()
            for name in ("listing_id", "address", "source_url", "title")
        ):
            continue
        filtered.append(listing)
    filtered.sort(key=lambda item: (item["review_status"] == "complete", item["listing_id"]))

    all_issues = [issue for listing in listings for issue in listing["issues"]]
    completed_issues = sum(
        issue["ui_state"] in {"decided", "applied"} for issue in all_issues
    )
    completed_listings = sum(
        listing["review_status"] == "complete" for listing in listings
    )
    eligibility_blockers = list(
        bundle["summary"].get("approval_blocking_conditions", [])
    )
    operator_summary_path = bundle["root"] / "operator-summary.json"
    operator_summary = (
        _json_object(operator_summary_path, artifact="operator-summary.json")
        if operator_summary_path.is_file()
        else {}
    )
    dashboard_summary = {
        "total_listings": len(bundle["canonical"]),
        "human_review_listings": len(listings),
        "visible_listings": len(filtered),
        "completed_listings": completed_listings,
        "unresolved_issues": len(all_issues) - completed_issues,
        "completed_issues": completed_issues,
        "total_issues": len(all_issues),
        "ai_review_issues": sum(
            "ai_review" in (issue.get("review_categories") or [])
            for issue in all_issues
        ),
        "manual_review_issues": sum(
            "unresolved_manual_review" in (issue.get("review_categories") or [])
            for issue in all_issues
        ),
        "geocoding_review_issues": sum(
            "geocoding_review" in (issue.get("review_categories") or [])
            for issue in all_issues
        ),
        "missing_price_issues": sum(
            "missing_monthly_price" in (issue.get("review_categories") or [])
            for issue in all_issues
        ),
        "map_ready_rows": sum(
            _truth(row.get("map_ready")) for row in bundle["canonical"].values()
        ),
        "not_map_ready_rows": sum(
            not _truth(row.get("map_ready"))
            for row in bundle["canonical"].values()
        ),
        "accepted_unknowns": bundle["summary"].get("accepted_unknown_count", 0)
        + sum(
            record.get("human_decision_status") == "accepted_as_unknown"
            and str(record.get("decision_id")) not in registry
            for record in active_human.values()
        ),
        "excluded_rows": sum(
            issue.get("human_decision", {}).get("human_decision_status") == "excluded"
            for issue in all_issues
            if isinstance(issue.get("human_decision"), dict)
        ),
        "pending_drafts": sum(issue["ui_state"] == "draft" for issue in all_issues),
        "applied_human_decisions": sum(
            _is_human_record(record)
            and str(record.get("decision_id")) in registry
            for record in bundle["decisions"]
        ),
        "active_errors": len(manifest.get("errors", []))
        if isinstance(manifest.get("errors"), list)
        else 1,
        "metric_discrepancies": bundle["summary"].get("metric_discrepancies", []),
        "canonical_eligibility": not eligibility_blockers,
        "canonical_eligibility_blockers": eligibility_blockers,
        "operator_ready_for_approval": bundle["summary"].get(
            "ready_for_approval", False
        ),
        "recommended_next_action": operator_summary.get(
            "recommended_next_action", "review_required"
        ),
    }
    return {
        "run_id": bundle["root"].name,
        "canonical_sha256": bundle["canonical_sha256"],
        "summary": dashboard_summary,
        "configuration": config,
        "western": WESTERN_COORDINATES,
        "listings": filtered,
        "filters": {
            "categories": sorted(
                {category for item in listings for category in item["review_categories"]}
            ),
            "fields": sorted(
                {_clean(issue.get("field")) for issue in all_issues if issue.get("field")}
            ),
            "reasons": sorted(
                {
                    _clean(issue.get("reason_code"))
                    for issue in all_issues
                    if issue.get("reason_code")
                }
            ),
            "states": ["unresolved", "draft", "decided", "applied", "stale"],
        },
    }


def _validate_coordinate_pair(
    latitude: Any,
    longitude: Any,
    *,
    config: dict[str, Any],
    out_of_bounds_reason: Any,
) -> tuple[float, float, bool]:
    lat = _number(latitude)
    lon = _number(longitude)
    if lat is None or lon is None:
        raise ReviewUIError("Latitude and longitude must both be numeric")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ReviewUIError("Latitude or longitude is outside the global valid range")
    inside = _inside_london(lat, lon, config)
    if not inside and not _clean(out_of_bounds_reason):
        raise ReviewUIError(
            "Out-of-bounds coordinates require an explicit override reason"
        )
    return lat, lon, inside


def _human_decision_id(record: dict[str, Any]) -> str:
    identity = {
        "version": 1,
        "run_id": record.get("run_id"),
        "base_decision_id": record.get("base_decision_id"),
        "listing_id": record.get("listing_id"),
        "field": record.get("field"),
        "human_decision_status": record.get("human_decision_status"),
        "selected_value": record.get("selected_value"),
        "apply_updates": record.get("apply_updates"),
        "reviewer_name": record.get("reviewer_name"),
        "review_note": record.get("review_note"),
        "supporting_text": record.get("supporting_text"),
        "out_of_bounds_reason": record.get("out_of_bounds_reason"),
        "input_fingerprint": record.get("input_fingerprint"),
    }
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _build_human_record(
    base: dict[str, Any],
    row: dict[str, str],
    payload: dict[str, Any],
    *,
    reviewer_name: str,
    reviewed_at_utc: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    status = _clean(payload.get("status"))
    if status not in HUMAN_STATUSES:
        raise ReviewUIError(f"Unsupported human review status: {status!r}")
    action = _clean(payload.get("action")) or "leave_unresolved"
    note = _clean(payload.get("review_note"))
    supporting = payload.get("supporting_text") or []
    if isinstance(supporting, str):
        supporting = [supporting]
    if not isinstance(supporting, list) or any(
        not isinstance(item, str) for item in supporting
    ):
        raise ReviewUIError("supporting_text must be a list of strings")
    if len(note) > 2000 or any(len(item) > 2000 for item in supporting):
        raise ReviewUIError("Review note or evidence is too long")
    if status == "excluded" and not note:
        raise ReviewUIError("Exclusion requires a reviewer reason")

    field = _clean(base.get("field"))
    original = base.get("original_value")
    selected: Any = payload.get("selected_value", original)
    updates: dict[str, Any] = {}
    evidence = {
        "base_decision_id": base.get("decision_id"),
        "base_reason_code": base.get("reason_code"),
        "original_evidence": base.get("evidence"),
    }

    if action == "leave_unresolved":
        status = "human_review_required"
    elif action == "accept_current":
        status = "human_approved"
        selected = original
        if field not in {"", "__listing__", "map_ready"}:
            updates[field] = original
    elif action == "correct_value":
        if field in {"", "__listing__", "map_ready"}:
            raise ReviewUIError("This issue does not support a scalar correction")
        if isinstance(selected, (dict, list)):
            raise ReviewUIError("Corrected scalar value cannot be an object or array")
        if field in BOOLEAN_REVIEW_FIELDS:
            if isinstance(selected, str) and selected.casefold() in {"true", "false"}:
                selected = selected.casefold() == "true"
            if selected is not None and not isinstance(selected, bool):
                raise ReviewUIError(f"{field} correction must be true, false, or null")
        if field in {"lease_term_months", "bathrooms", "parking_spaces"}:
            if selected is not None and _number(selected) is None:
                raise ReviewUIError(f"{field} correction must be numeric or null")
        if selected == original and not note and not supporting:
            raise ReviewUIError("A material correction requires evidence or a note")
        status = "human_approved"
        updates[field] = selected
    elif action == "accepted_as_unknown":
        status = "accepted_as_unknown"
        selected = None
        if field == "map_ready":
            updates["geocode_review_disposition"] = _clean(
                payload.get("unknown_kind")
            ) or "coordinates_unknown"
            if payload.get("unknown_kind") == "address_unknown":
                updates.update(
                    {
                        "address_original": row.get("address_original")
                        or row.get("address"),
                        "address": None,
                        "address_source": "manual_review",
                        "geocode_query": "",
                        "latitude": None,
                        "longitude": None,
                        "geocode_status": "missing_address",
                        "geocode_formatted": "",
                        "geocode_error": "Address accepted as unknown by reviewer",
                    }
                )
            elif payload.get("unknown_kind") == "coordinates_unknown":
                updates.update(
                    {
                        "latitude": None,
                        "longitude": None,
                        "geocode_status": "manual_coordinates_unknown",
                        "geocode_error": "Coordinates accepted as unknown by reviewer",
                    }
                )
        elif field == "price_monthly":
            updates["price_review_disposition"] = _clean(
                payload.get("unknown_kind")
            ) or "period_ambiguous"
        elif field not in {"", "__listing__"}:
            updates[field] = None
            updates[f"{field}_review_disposition"] = "accepted_unknown"
    elif action in {"accept_current_geocode", "correct_geocode"}:
        if field != "map_ready":
            raise ReviewUIError("Geocode actions require a map-readiness issue")
        status = "human_approved"
        if action == "accept_current_geocode":
            lat, lon = row.get("latitude"), row.get("longitude")
        else:
            lat, lon = payload.get("latitude"), payload.get("longitude")
        latitude, longitude, inside = _validate_coordinate_pair(
            lat,
            lon,
            config=config,
            out_of_bounds_reason=payload.get("out_of_bounds_reason"),
        )
        if action == "correct_geocode" and not note and not supporting:
            raise ReviewUIError("Coordinate corrections require evidence or a note")
        selected = {"latitude": latitude, "longitude": longitude}
        accepting_current = action == "accept_current_geocode"
        updates.update(
            {
                "latitude": latitude,
                "longitude": longitude,
                "geocode_status": row.get("geocode_status")
                if accepting_current
                else "ok",
                "geocode_provider": row.get("geocode_provider")
                or ("geoapify" if accepting_current else "manual"),
                "geocode_match_type": row.get("geocode_match_type")
                if accepting_current
                else "manual_review",
                "geocode_result_type": row.get("geocode_result_type")
                if accepting_current
                else "manual",
                "geocode_error": "",
                "geocode_manual_override": True,
                "geocode_manual_override_reason": note
                or "Reviewer accepted current coordinates",
                "geocode_review_disposition": "human_approved",
            }
        )
        evidence["inside_london_bounds"] = inside
    elif action == "correct_address":
        if field != "map_ready":
            raise ReviewUIError("Address correction requires a map-readiness issue")
        corrected = _clean(payload.get("corrected_address"))
        if not corrected:
            raise ReviewUIError("Corrected address is required")
        if not note and not supporting:
            raise ReviewUIError("Address corrections require evidence or a note")
        status = "human_approved"
        selected = corrected
        updates.update(
            {
                "address_original": row.get("address_original") or row.get("address"),
                "address": corrected,
                "address_source": "manual_review",
                "geocode_query": normalize_address(corrected),
                "geocode_review_disposition": "address_corrected_geocode_stale",
            }
        )
        if payload.get("coordinates_validated"):
            latitude, longitude, inside = _validate_coordinate_pair(
                payload.get("latitude"),
                payload.get("longitude"),
                config=config,
                out_of_bounds_reason=payload.get("out_of_bounds_reason"),
            )
            updates.update(
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "geocode_status": "ok",
                    "geocode_provider": "manual",
                    "geocode_match_type": "manual_review",
                    "geocode_result_type": "manual",
                    "geocode_error": "",
                    "geocode_manual_override": True,
                    "geocode_manual_override_reason": note,
                    "geocode_review_disposition": "human_approved",
                }
            )
            evidence["inside_london_bounds"] = inside
        else:
            updates.update(
                {
                    "latitude": None,
                    "longitude": None,
                    "geocode_status": "stale_address_correction",
                    "geocode_confidence": None,
                    "geocode_match_type": "",
                    "geocode_result_type": "",
                    "geocode_formatted": "",
                    "geocode_error": "Address changed; future geocoding required",
                    "distance_to_western_km": None,
                    "geocode_manual_override": False,
                }
            )
            evidence["future_geocoding_required"] = True
    elif action == "price_correction":
        if field != "price_monthly":
            raise ReviewUIError("Price correction requires a price issue")
        if not note and not supporting:
            raise ReviewUIError("Price corrections require evidence or a note")
        status = "human_approved"
        price_updates = payload.get("price_fields")
        if not isinstance(price_updates, dict) or not price_updates:
            raise ReviewUIError("price_fields must contain at least one correction")
        allowed = {"price_text", "price_numeric", "price_period", "price_monthly"}
        unknown = set(price_updates) - allowed
        if unknown:
            raise ReviewUIError("Unsupported price fields: " + ", ".join(sorted(unknown)))
        if any(isinstance(value, (dict, list)) for value in price_updates.values()):
            raise ReviewUIError("Price corrections must contain scalar values")
        for numeric_field in ("price_numeric", "price_monthly"):
            if numeric_field in price_updates and _clean(price_updates[numeric_field]):
                if _number(price_updates[numeric_field]) is None:
                    raise ReviewUIError(f"{numeric_field} must be numeric or blank")
        updates.update(price_updates)
        updates["price_review_disposition"] = "human_approved"
        selected = price_updates.get("price_monthly", original)
    elif action != "exclude":
        raise ReviewUIError(f"Unsupported review action: {action!r}")

    if status == "human_approved" and selected != original and not note and not supporting:
        raise ReviewUIError("A material manual correction requires evidence or a note")

    stored_status = (
        "accepted_as_unknown"
        if status == "accepted_as_unknown"
        else "excluded"
        if status == "excluded"
        else "human_review_required"
    )
    record = {
        "decision_id": "",
        "run_id": base.get("run_id"),
        "listing_id": base.get("listing_id"),
        "field": field,
        "original_value": original,
        "selected_value": selected,
        "decision_status": stored_status,
        "reason_code": f"human_ui_{action}",
        "evidence": evidence,
        "evidence_source": "human_review",
        "confidence": 1.0 if status in COMPLETED_HUMAN_STATUSES else 0.0,
        "reviewer_type": "human",
        "reviewer_name": reviewer_name,
        "created_at_utc": reviewed_at_utc,
        "reviewed_at_utc": reviewed_at_utc,
        "input_fingerprint": base.get("input_fingerprint"),
        "base_decision_id": base.get("decision_id"),
        "human_decision_status": status,
        "human_approved": status in COMPLETED_HUMAN_STATUSES,
        "human_reviewer": reviewer_name,
        "human_approved_at_utc": (
            reviewed_at_utc if status in COMPLETED_HUMAN_STATUSES else None
        ),
        "supporting_text": supporting,
        "conflicting_text": base.get("conflicting_text") or [],
        "review_note": note,
        "reasoning_summary": note or f"Reviewer selected {status}.",
        "apply_updates": updates,
        "decision_state": "active",
        "supersedes_decision_id": payload.get("supersedes_decision_id"),
        "out_of_bounds_reason": _clean(payload.get("out_of_bounds_reason")),
        "review_categories": base.get("review_categories") or [],
        "review_flag": base.get("review_flag"),
    }
    record["decision_id"] = _human_decision_id(record)
    return record


def _validate_human_record_for_merge(
    record: dict[str, Any],
    *,
    run_id: str,
    base_by_id: dict[str, dict[str, Any]],
    canonical: dict[str, dict[str, str]],
    config: dict[str, Any],
) -> None:
    missing = [field for field in REQUIRED_DECISION_FIELDS if field not in record]
    if missing:
        raise ReviewUIError(f"Human decision lacks required fields: {missing}")
    if not _is_human_record(record) or record.get("run_id") != run_id:
        raise ReviewUIError("Incoming record is not a human decision for this run")
    base = base_by_id.get(_clean(record.get("base_decision_id")))
    if base is None:
        raise ReviewUIError("Incoming human decision has no current base issue")
    listing_id = _clean(record.get("listing_id"))
    if listing_id not in canonical or listing_id != _clean(base.get("listing_id")):
        raise ReviewUIError("Incoming human decision listing identity is invalid")
    fingerprint = listing_fingerprint(canonical[listing_id])
    if record.get("input_fingerprint") != fingerprint:
        raise ReviewUIError("Incoming human decision fingerprint is stale")
    if not _clean(record.get("reviewer_name")):
        raise ReviewUIError("Incoming human decision has no reviewer name")
    if _clean(record.get("human_reviewer")) != _clean(record.get("reviewer_name")):
        raise ReviewUIError("Incoming reviewer provenance is inconsistent")
    human_status = record.get("human_decision_status")
    if human_status not in HUMAN_STATUSES:
        raise ReviewUIError("Incoming human decision status is invalid")
    if not isinstance(record.get("review_note", ""), str):
        raise ReviewUIError("Incoming review note must be a string")
    if human_status == "excluded" and not _clean(record.get("review_note")):
        raise ReviewUIError("Incoming exclusion requires a reviewer reason")
    updates = record.get("apply_updates")
    if not isinstance(updates, dict):
        raise ReviewUIError("Incoming apply_updates must be an object")
    unsafe_fields = set(updates) & IMMUTABLE_FIELDS
    if unsafe_fields:
        raise ReviewUIError(
            "Incoming decision modifies identity fields: "
            + ", ".join(sorted(unsafe_fields))
        )
    if any(
        not isinstance(field, str) or any(character in field for character in "\r\n\0")
        for field in updates
    ):
        raise ReviewUIError("Incoming update field name is unsafe")
    if "latitude" in updates or "longitude" in updates:
        if updates.get("latitude") is not None or updates.get("longitude") is not None:
            _validate_coordinate_pair(
                updates.get("latitude"),
                updates.get("longitude"),
                config=config,
                out_of_bounds_reason=record.get("out_of_bounds_reason"),
            )
    if _truth(updates.get("geocode_manual_override")) and not _clean(
        updates.get("geocode_manual_override_reason")
    ):
        raise ReviewUIError("Incoming manual geocode override requires a reason")
    if record.get("decision_id") != _human_decision_id(record):
        raise ReviewUIError("Incoming human decision ID does not match its content")


def _merge_records(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str], bool]:
    result = copy.deepcopy(existing)
    by_id = {_clean(record.get("decision_id")): record for record in result}
    active = _active_human_records(result)
    added: list[str] = []
    changed = False
    for record in incoming:
        decision_id = _clean(record.get("decision_id"))
        if decision_id in by_id:
            if (
                not _is_human_record(by_id[decision_id])
                or _human_decision_id(by_id[decision_id]) != decision_id
                or _human_decision_id(record) != decision_id
            ):
                raise ReviewUIError(f"Decision ID collision: {decision_id}")
            continue
        base_id = _clean(record.get("base_decision_id"))
        prior = active.get(base_id)
        if prior is not None:
            supersedes = _clean(record.get("supersedes_decision_id"))
            if supersedes != _clean(prior.get("decision_id")):
                raise ReviewUIError(
                    "Duplicate conflicting decision requires explicit supersedes_decision_id"
                )
            prior["decision_state"] = "superseded"
            prior["superseded_by"] = decision_id
        copied = copy.deepcopy(record)
        result.append(copied)
        by_id[decision_id] = copied
        active[base_id] = copied
        added.append(decision_id)
        changed = True
    result.sort(
        key=lambda item: (
            _clean(item.get("created_at_utc")),
            _clean(item.get("listing_id")),
            _clean(item.get("decision_id")),
        )
    )
    return result, added, changed


def save_human_decisions(
    run_dir: Path,
    payloads: list[dict[str, Any]],
    *,
    default_reviewer: Optional[str] = None,
    read_only: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Validate and atomically save one all-or-nothing batch of human decisions."""

    if read_only:
        raise ReviewUIError("Review UI is read-only; decision writes are disabled")
    if not isinstance(payloads, list) or not payloads or len(payloads) > 100:
        raise ReviewUIError("Decision batch must contain between 1 and 100 records")
    at = now or datetime.now(timezone.utc)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ReviewUIError("Review timestamp must be timezone-aware")
    reviewed_at = at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    decisions_path = run_dir.resolve() / "review" / "review-decisions.jsonl"
    with manifest_lock(decisions_path):
        bundle = _validate_bundle(run_dir)
        base_by_id = _base_decisions(bundle)
        incoming: list[dict[str, Any]] = []
        seen_bases: set[str] = set()
        for payload in payloads:
            if not isinstance(payload, dict):
                raise ReviewUIError("Every decision payload must be an object")
            for text_field in (
                "status",
                "action",
                "review_note",
                "corrected_address",
                "unknown_kind",
                "out_of_bounds_reason",
                "supersedes_decision_id",
            ):
                if payload.get(text_field) is not None and not isinstance(
                    payload.get(text_field), str
                ):
                    raise ReviewUIError(f"{text_field} must be a string")
            base_id = _clean(payload.get("base_decision_id"))
            base = base_by_id.get(base_id)
            if base is None:
                raise ReviewUIError("Decision references a missing or stale base issue")
            if base_id in seen_bases:
                raise ReviewUIError("A batch cannot contain two decisions for one issue")
            seen_bases.add(base_id)
            listing_id = _clean(payload.get("listing_id"))
            if listing_id != _clean(base.get("listing_id")):
                raise ReviewUIError("Decision listing_id does not match its base issue")
            row = bundle["canonical"][listing_id]
            fingerprint = listing_fingerprint(row)
            if (
                payload.get("input_fingerprint") != fingerprint
                or base.get("input_fingerprint") != fingerprint
            ):
                raise ReviewUIError("Decision fingerprint is stale; reload the dashboard")
            raw_reviewer = payload.get("reviewer_name") or default_reviewer
            if raw_reviewer is not None and not isinstance(raw_reviewer, str):
                raise ReviewUIError("reviewer_name must be a string")
            reviewer = _clean(raw_reviewer)
            if not reviewer:
                raise ReviewUIError("Reviewer name is required before saving decisions")
            if len(reviewer) > 100:
                raise ReviewUIError("Reviewer name is too long")
            incoming.append(
                _build_human_record(
                    base,
                    row,
                    payload,
                    reviewer_name=reviewer,
                    reviewed_at_utc=reviewed_at,
                    config=bundle["summary"].get("configuration", {}),
                )
            )
        merged, added, changed = _merge_records(bundle["decisions"], incoming)
        if changed:
            _write_jsonl(decisions_path, merged)
    return {
        "ok": True,
        "run_id": run_dir.resolve().name,
        "saved_decision_ids": added,
        "saved_count": len(added),
        "idempotent": not changed,
        "applied": False,
        "approval_changed": False,
        "external_services_used": False,
    }


def merge_human_decision_file(run_dir: Path, incoming_path: Path) -> dict[str, Any]:
    """Merge validated human-only records into an authoritative run atomically."""

    incoming_records = [record for record in _read_jsonl(incoming_path) if _is_human_record(record)]
    if not incoming_records:
        raise ReviewUIError("Incoming decision file contains no human decisions")
    decisions_path = run_dir.resolve() / "review" / "review-decisions.jsonl"
    with manifest_lock(decisions_path):
        bundle = _validate_bundle(run_dir)
        base_by_id = _base_decisions(bundle)
        for record in incoming_records:
            _validate_human_record_for_merge(
                record,
                run_id=bundle["root"].name,
                base_by_id=base_by_id,
                canonical=bundle["canonical"],
                config=bundle["summary"].get("configuration", {}),
            )
        merged, added, changed = _merge_records(bundle["decisions"], incoming_records)
        if changed:
            _write_jsonl(decisions_path, merged)
    return {
        "ok": True,
        "run_id": run_dir.resolve().name,
        "merged_decision_ids": added,
        "merged_count": len(added),
        "idempotent": not changed,
        "applied": False,
        "external_services_used": False,
    }


def bulk_preview(run_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Preview only homogeneous, explicitly allowlisted bulk review actions."""

    dashboard = load_dashboard(run_dir)
    decision_ids = payload.get("base_decision_ids")
    action = _clean(payload.get("action"))
    if not isinstance(decision_ids, list) or not decision_ids:
        raise ReviewUIError("Bulk preview requires base_decision_ids")
    by_id = {
        issue["base_decision_id"]: issue
        for listing in dashboard["listings"]
        for issue in listing["issues"]
    }
    selected = []
    for decision_id in decision_ids:
        issue = by_id.get(_clean(decision_id))
        if issue is None:
            raise ReviewUIError("Bulk preview contains a missing or stale decision")
        selected.append(issue)
    fields = {_clean(issue.get("field")) for issue in selected}
    reasons = {_clean(issue.get("reason_code")) for issue in selected}
    safe = len(fields) == 1 and len(reasons) == 1
    criteria = "All decisions must share one field and one reason."
    if action == "accepted_as_unknown":
        safe = safe and reasons <= SAFE_BULK_UNKNOWN_REASONS
        criteria += " Reason must be an allowlisted deterministic unknown classification."
    elif action == "accept_current":
        safe = safe and reasons <= SAFE_BULK_CURRENT_REASONS and "map_ready" not in fields
        criteria += " Reason must prove an already-resolved non-geocode current value."
    else:
        safe = False
        criteria = "Bulk exclusion, coordinate correction, and this action are unavailable."
    return {
        "ok": True,
        "safe": safe,
        "action": action,
        "affected_listing_count": len(
            {_clean(issue.get("listing_id")) for issue in selected}
        ),
        "affected_issue_count": len(selected),
        "shared_reason": next(iter(reasons)) if len(reasons) == 1 else None,
        "fields": sorted(fields),
        "evidence_criteria": criteria,
        "base_decision_ids": decision_ids,
    }


def create_review_app(
    run_dir: Path,
    *,
    reviewer: Optional[str] = None,
    read_only: bool = False,
) -> FastAPI:
    """Create a local-only review application for one already-resolved run path."""

    root = run_dir.resolve()
    _validate_bundle(root)
    app = FastAPI(
        title="UWO Housing Local Review",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.run_dir = root
    app.state.default_reviewer = _clean(reviewer)
    app.state.read_only = bool(read_only)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://unpkg.com; "
            "style-src 'self' https://unpkg.com; img-src 'self' data: "
            "https://*.tile.openstreetmap.org https://unpkg.com; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    def translate_error(exc: Exception) -> HTTPException:
        status_code = 403 if "read-only" in str(exc).casefold() else 409
        return HTTPException(status_code=status_code, detail=str(exc))

    @app.get("/")
    def index():
        return FileResponse(ASSET_ROOT / "index.html", media_type="text/html")

    @app.get("/assets/review.js")
    def javascript():
        return FileResponse(ASSET_ROOT / "review.js", media_type="text/javascript")

    @app.get("/assets/review.css")
    def stylesheet():
        return FileResponse(ASSET_ROOT / "review.css", media_type="text/css")

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "run_id": root.name,
            "read_only": app.state.read_only,
            "external_services_used": False,
        }

    @app.get("/api/state")
    def state(
        category: Optional[str] = None,
        field: Optional[str] = None,
        reason: Optional[str] = None,
        minimum_confidence: Optional[float] = Query(None, ge=0, le=1),
        maximum_confidence: Optional[float] = Query(None, ge=0, le=1),
        map_ready: Optional[bool] = None,
        missing_price: Optional[bool] = None,
        decision_state: Optional[str] = None,
        search: Optional[str] = None,
    ):
        try:
            result = load_dashboard(
                root,
                category=category,
                field=field,
                reason=reason,
                minimum_confidence=minimum_confidence,
                maximum_confidence=maximum_confidence,
                map_ready=map_ready,
                missing_price=missing_price,
                decision_state=decision_state,
                search=search,
            )
        except (ReviewUIError, ReviewWorkflowError, ValueError) as exc:
            raise translate_error(exc) from exc
        result["read_only"] = app.state.read_only
        result["default_reviewer"] = app.state.default_reviewer
        return result

    @app.post("/api/decisions")
    def save(payload: dict[str, Any] = Body(...)):
        try:
            records = payload.get("decisions")
            return save_human_decisions(
                root,
                records,
                default_reviewer=payload.get("reviewer_name")
                or app.state.default_reviewer,
                read_only=app.state.read_only,
            )
        except (ReviewUIError, ReviewWorkflowError, ValueError) as exc:
            raise translate_error(exc) from exc

    @app.post("/api/bulk-preview")
    def preview(payload: dict[str, Any] = Body(...)):
        try:
            return bulk_preview(root, payload)
        except (ReviewUIError, ReviewWorkflowError, ValueError) as exc:
            raise translate_error(exc) from exc

    @app.post("/api/bulk-decisions")
    def save_bulk(payload: dict[str, Any] = Body(...)):
        if app.state.read_only:
            raise HTTPException(status_code=403, detail="Review UI is read-only")
        try:
            preview = bulk_preview(root, payload)
            if not preview["safe"] or payload.get("confirm") is not True:
                raise ReviewUIError(
                    "Safe bulk preview and explicit confirm=true are required"
                )
            dashboard = load_dashboard(root)
            issues = {
                issue["base_decision_id"]: issue
                for listing in dashboard["listings"]
                for issue in listing["issues"]
            }
            action = (
                "accepted_as_unknown"
                if preview["action"] == "accepted_as_unknown"
                else "accept_current"
            )
            decisions = [
                {
                    "base_decision_id": decision_id,
                    "listing_id": issues[decision_id]["listing_id"],
                    "input_fingerprint": issues[decision_id]["input_fingerprint"],
                    "status": (
                        "accepted_as_unknown"
                        if action == "accepted_as_unknown"
                        else "human_approved"
                    ),
                    "action": action,
                    "review_note": payload.get("review_note", ""),
                    "reviewer_name": payload.get("reviewer_name"),
                }
                for decision_id in preview["base_decision_ids"]
            ]
            return save_human_decisions(
                root,
                decisions,
                default_reviewer=payload.get("reviewer_name")
                or app.state.default_reviewer,
            )
        except (ReviewUIError, ReviewWorkflowError, ValueError) as exc:
            raise translate_error(exc) from exc

    return app


def serve_review_ui(
    run_dir: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    reviewer: Optional[str] = None,
    read_only: bool = False,
    open_browser: bool = True,
    unsafe_development_bind: bool = False,
) -> dict[str, Any]:
    """Validate and serve one review bundle until the local server is stopped."""

    selected_host = validate_bind_host(
        host, unsafe_development_bind=unsafe_development_bind
    )
    if not 1 <= port <= 65535:
        raise ReviewUIError("Review UI port must be between 1 and 65535")
    app = create_review_app(run_dir, reviewer=reviewer, read_only=read_only)
    display_host = "127.0.0.1" if selected_host in {"0.0.0.0", "::"} else selected_host
    url = f"http://{display_host}:{port}/"
    if open_browser:
        webbrowser.open(url)
    import uvicorn

    uvicorn.run(app, host=selected_host, port=port, log_level="info")
    return {
        "ok": True,
        "run_id": run_dir.resolve().name,
        "url": url,
        "read_only": read_only,
        "external_services_used": False,
    }
