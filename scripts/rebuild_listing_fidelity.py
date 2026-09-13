"""Rebuild one listing run from captured artifacts with bounded external work.

This command is deliberately narrower than the normal Stage 0-to-Stage 3
operator. It is for semantic migrations where captured website evidence and
compatible geocodes remain authoritative, but deterministic rules have
changed. It never scrapes or calls AI. An explicit bound may permit newly
recovered civic addresses through the normal cache-aware Stage 3 geocoder.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from dotenv import load_dotenv

from pipeline.ai_enricher import merge_ai_row, promote_rule_fields, safe_val, validate_row
from pipeline.canonical_rebuild import REQUIRED_GEOCODE_FIELDS, rebuild_canonical
from pipeline.database_importer import prepare_canonical_listing
from pipeline.geocoder import apply_geocoding, normalize_address
from pipeline.run_approval import sha256_file
from pipeline.run_context import RunContext, finalize_run
from pipeline.uwo_listing_enricher import (
    PRICE_RE,
    PRICE_TEXT_RE,
    ListingRecord,
    UWOListingScraper,
    build_website_ready,
    merge_with_original_rows,
)


AI_FIELD_TARGETS = {
    "ai_is_sublet": "is_sublet",
    "ai_bathrooms": "bathrooms",
    "ai_bathroom_type": "bathroom_type",
    "ai_parking_available": "parking_available",
    "ai_parking_spaces": "parking_spaces",
    "ai_laundry": "laundry",
    "ai_furnished": "furnished",
    "ai_air_conditioning": "air_conditioning",
    "ai_dishwasher": "dishwasher",
    "ai_tenant_type": "tenant_type",
    "ai_preferred_gender": "preferred_gender",
    "ai_lease_term_months": "lease_term_months",
    "ai_lease_type": "lease_type",
    "ai_utilities_included": "utilities_included",
    "ai_utilities_status": "utilities_status",
}

RAW_CAPTURED_FIELDS = (
    "source_url",
    "title",
    "address_raw",
    "address",
    "housing_type_raw",
    "bedrooms_raw",
    "utilities_raw",
    "date_available_raw",
    "location_area_raw",
    "distance_to_campus_raw",
    "preferred_gender_raw",
    "smoking_raw",
    "tenant_type_raw",
    "description",
    "amenities",
    "amenities_list",
    "landlord_name",
    "landlord_phone",
)

GEOCODE_FIELDS = (
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

DIFF_CLASSIFICATIONS = {
    "EXPECTED_ADDRESS_RECOVERY",
    "EXPECTED_GEOCODE_FOR_RECOVERED_ADDRESS",
    "EXPECTED_PRICE_PERIOD_CORRECTION",
    "EXPECTED_PRICE_PERIOD_RECOVERY",
    "EXPECTED_BATHROOM_RECOVERY",
    "EXPECTED_LEASE_DOWNGRADE",
    "EXPECTED_FURNISHING_REVIEW_CORRECTION",
    "EXPECTED_PROVENANCE_UPDATE",
    "EXPECTED_SEMANTIC_CORRECTION",
    "EXPECTED_UNKNOWN_DOWNGRADE",
    "EXPECTED_CATEGORY_RESTORATION",
    "EXPECTED_SOURCE_PRIORITY_CORRECTION",
    "UNEXPECTED_IDENTITY_CHANGE",
    "UNEXPECTED_ADDRESS_CHANGE",
    "UNEXPECTED_PRICE_CHANGE",
    "UNEXPECTED_PRICE_VALUE_CHANGE",
    "UNEXPECTED_GEOCODE_CHANGE",
    "UNEXPECTED_PROPERTY_LINK_CHANGE",
    "UNEXPECTED_OTHER_CHANGE",
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]

IDENTITY_FIELDS = {"listing_id", "listing_url"}
PRICE_FIELDS = {"price_text", "price_numeric", "price_period", "price_monthly"}
CATEGORY_FIELDS = {"housing_type", "housing_type_raw"}
SOURCE_PRIORITY_FIELDS = {
    "preferred_gender",
    "preferred_gender_rule",
    "preferred_gender_source",
    "utilities_included",
    "utilities_included_rule",
    "utilities_included_source",
    "utilities_status",
    "utilities_status_source",
}
SEMANTIC_FIELDS = {
    "is_sublet",
    "is_sublet_source",
    "furnished",
    "furnished_rule",
    "furnished_source",
    "bathrooms",
    "bathrooms_source",
    "bathroom_type",
    "bathroom_type_rule",
    "bathroom_type_source",
    "availability_category",
    "availability_category_source",
    "availability_category_evidence",
    "availability_category_conflict",
    "lease_term_months",
    "lease_term_months_rule",
    "lease_term_months_source",
    "lease_type",
    "lease_type_rule",
    "lease_type_source",
}


class FidelityRebuildError(RuntimeError):
    """Captured artifacts cannot produce a safe semantic rebuild."""


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=object, keep_default_na=False)


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, lineterminator="\n")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"nan", "none", "null", "nat"} else text


def _normalized(value: Any) -> str:
    text = _clean(value)
    if text.casefold() in {"true", "1.0", "1"}:
        return "true"
    if text.casefold() in {"false", "0.0", "0"}:
        return "false"
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer():
        return str(int(number))
    return format(number, ".12g")


def _unknown(value: Any) -> bool:
    return _clean(value).casefold() in {"", "unknown", "not_specified"}


def _amenities(value: Any, fallback: Any) -> list[str]:
    text = _clean(value)
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except json.JSONDecodeError:
            pass
    return [item.strip() for item in _clean(fallback).split(",") if item.strip()]


def _price_fields(title: Any, description: Any) -> tuple[Any, Any, Any, Any]:
    title_text = _clean(title)
    match = PRICE_TEXT_RE.search(title_text)
    price_text = match.group(0) if match else None
    numeric_match = PRICE_RE.search(price_text or "")
    price_numeric = (
        float(numeric_match.group(1).replace(",", "")) if numeric_match else None
    )
    price_period = UWOListingScraper._infer_price_period(
        title_text, _clean(description), price_numeric
    )
    price_monthly = UWOListingScraper._normalize_monthly_price(
        price_numeric, price_period
    )
    return price_text, price_numeric, price_period, price_monthly


def rebuild_stage1(details: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Recompute deterministic columns from captured raw/text fields only."""

    rebuilt_rows: list[dict[str, Any]] = []
    changed = Counter()
    record_fields = tuple(ListingRecord.__dataclass_fields__)
    for source in details.to_dict(orient="records"):
        row = dict(source)
        amenities = _amenities(row.get("amenities_list"), row.get("amenities"))
        description = _clean(row.get("description"))
        title = _clean(row.get("title"))
        price_text, price_numeric, price_period, price_monthly = _price_fields(
            title, description
        )
        utilities_status = UWOListingScraper._parse_utilities_status(
            amenities, _clean(row.get("utilities_raw")) or None, description or None
        )
        is_sublet = UWOListingScraper._detect_sublet(
            title,
            description,
            _clean(row.get("lease_term_raw")) or None,
            _clean(row.get("housing_type_raw")) or None,
        )
        lease_months = UWOListingScraper._parse_lease_term_months_rule(
            _clean(row.get("lease_term_raw")) or None
        )
        availability = UWOListingScraper._parse_availability_evidence(
            title,
            description,
            _clean(row.get("date_available_raw")) or None,
        )
        availability_category = availability.category
        availability_source = availability.source
        availability_evidence = availability.evidence
        if availability_category is None and not availability.conflict:
            existing_category = _clean(row.get("availability_category"))
            if existing_category in {"summer_available", "non_summer"}:
                availability_category = existing_category
                availability_source = (
                    _clean(row.get("availability_category_source")) or None
                )
                availability_evidence = (
                    _clean(row.get("availability_category_evidence")) or None
                )
        updates: dict[str, Any] = {
            "address": (
                _clean(row.get("address"))
                or UWOListingScraper._recover_address_from_description(
                    description or None
                )
                or None
            ),
            "price_text": price_text,
            "price_numeric": price_numeric,
            "price_period": price_period,
            "price_monthly": price_monthly,
            "housing_type": UWOListingScraper._normalize_housing_type(
                _clean(row.get("housing_type_raw")) or None
            ),
            "bedrooms": UWOListingScraper._to_int(
                _clean(row.get("bedrooms_raw")) or None
            ),
            "utilities_status": utilities_status,
            "utilities_status_source": "rule" if utilities_status else None,
            "utilities_included_rule": UWOListingScraper._utilities_included_from_status(
                utilities_status
            ),
            "date_available": UWOListingScraper._clean_text(
                _clean(row.get("date_available_raw"))
            )
            or None,
            "availability_text": UWOListingScraper._clean_text(
                _clean(row.get("date_available_raw"))
            )
            or None,
            "available_now_rule": UWOListingScraper._parse_available_now(
                _clean(row.get("date_available_raw")) or None, description or None
            ),
            "lease_term_months_rule": lease_months,
            "lease_type_rule": UWOListingScraper._parse_lease_type_rule(
                is_sublet, lease_months, description or None
            ),
            "location_area": UWOListingScraper._clean_text(
                _clean(row.get("location_area_raw"))
            )
            or None,
            "distance_to_campus_km": UWOListingScraper._parse_distance_km(
                _clean(row.get("distance_to_campus_raw")) or None
            ),
            "preferred_gender_rule": UWOListingScraper._normalize_gender(
                _clean(row.get("preferred_gender_raw")) or None, description or None
            ),
            "smoking_allowed_rule": UWOListingScraper._normalize_smoking(
                _clean(row.get("smoking_raw")) or None, description or None
            ),
            "tenant_type_rule": UWOListingScraper._normalize_tenant_type(
                _clean(row.get("tenant_type_raw")) or None, description or None
            ),
            "air_conditioning_rule": UWOListingScraper._parse_amenity_bool(
                amenities, {"a/c", "air conditioning", "central air", "air-conditioning"}
            ),
            "laundry_rule": UWOListingScraper._parse_amenity_bool(
                amenities,
                {
                    "laundry",
                    "in-suite laundry",
                    "ensuite laundry",
                    "in suite laundry",
                    "washer",
                    "washer/dryer",
                },
            ),
            "dishwasher_rule": UWOListingScraper._parse_amenity_bool(
                amenities, {"dishwasher"}
            ),
            "bathrooms_rule": UWOListingScraper._parse_bathrooms_rule(
                description or None
            ),
            "bathroom_type_rule": UWOListingScraper._parse_bathroom_type_rule(
                amenities
            ),
            "furnished_rule": UWOListingScraper._parse_furnished_rule(
                amenities, description or None
            ),
            "is_sublet": is_sublet,
            "is_sublet_source": "deterministic_rule" if is_sublet is True else None,
            "availability_category": availability_category,
            "availability_category_source": availability_source,
            "availability_category_evidence": availability_evidence,
            "availability_category_conflict": availability.conflict or None,
            "transit_routes": UWOListingScraper._extract_transit_routes(
                description or None
            ),
        }
        parking, spaces = UWOListingScraper._parse_parking_fields(
            amenities, description or None
        )
        updates["parking_available_rule"] = parking
        updates["parking_spaces_rule"] = spaces
        for field, value in updates.items():
            if _normalized(row.get(field)) != _normalized(value):
                changed[field] += 1
            row[field] = value
        rebuilt_rows.append({field: row.get(field) for field in record_fields})
    return pd.DataFrame(rebuilt_rows, columns=record_fields), dict(sorted(changed.items()))


def rebuild_stage2_cached(
    website_ready: pd.DataFrame, cached_reviewed: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Apply current rules and evidence gates to cached AI output, with no calls."""

    cached_by_id = {
        _clean(row.get("listing_id")): row
        for row in cached_reviewed.to_dict(orient="records")
    }
    enriched: list[dict[str, Any]] = []
    reviewed: list[dict[str, Any]] = []
    retained = Counter()
    rejected = Counter()
    promoted_count = 0
    manual_fields = 0
    for source in website_ready.to_dict(orient="records"):
        listing_id = _clean(source.get("listing_id"))
        cached = cached_by_id.get(listing_id)
        if cached is None:
            raise FidelityRebuildError(f"Cached Stage 2 row is missing: {listing_id}")
        base = pd.Series(source)
        promoted = promote_rule_fields(base)
        promoted_count += sum(not key.endswith("_source") for key in promoted)
        ai_row = {
            key: value
            for key, value in cached.items()
            if key.startswith("ai_") and key not in {"ai_error", "ai_skipped", "ai_skip_reason"}
        }
        merged = merge_ai_row(base, promoted, ai_row)
        merged["ai_error"] = ""
        merged["ai_skipped"] = True
        merged["ai_skip_reason"] = "cached_ai_only_no_network"
        for ai_field, target in AI_FIELD_TARGETS.items():
            if safe_val(ai_row.get(ai_field)) is None:
                continue
            if _clean(merged.get(f"{target}_source")).casefold() == "ai":
                retained[target] += 1
            else:
                rejected[target] += 1
        flags, score = validate_row(merged)
        merged["review_flags"] = json.dumps(flags, separators=(",", ":"))
        merged["review_score"] = score
        merged["needs_manual_review"] = score >= 1
        enriched.append(dict(merged))

        final = dict(merged)
        applied_manual = False
        for source_field, source_value in cached.items():
            if not source_field.endswith("_source"):
                continue
            if not _clean(source_value).casefold().startswith("manual"):
                continue
            target = source_field.removesuffix("_source")
            final[target] = cached.get(target)
            final[source_field] = source_value
            manual_fields += 1
            applied_manual = True
        if applied_manual:
            final["manual_reviewed"] = True
            final["manual_review_note"] = cached.get("manual_review_note")
            flags, score = validate_row(final)
            final["review_flags"] = json.dumps(flags, separators=(",", ":"))
            final["review_score"] = score
            final["needs_manual_review"] = score >= 1
        reviewed.append(final)

    enriched_frame = pd.DataFrame(enriched)
    reviewed_frame = pd.DataFrame(reviewed)
    review_count = int(reviewed_frame["needs_manual_review"].eq(True).sum())
    return enriched_frame, reviewed_frame, {
        "new_ai_call_count": 0,
        "deterministic_promotions": promoted_count,
        "cached_ai_values_retained": dict(sorted(retained.items())),
        "cached_ai_values_rejected": dict(sorted(rejected.items())),
        "cached_ai_retained_total": sum(retained.values()),
        "cached_ai_rejected_total": sum(rejected.values()),
        "manual_fields_preserved": manual_fields,
        "review_count": review_count,
        "ai_error_count": 0,
    }


def reuse_geocodes(
    reviewed: pd.DataFrame, source_geocoded: pd.DataFrame
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Attach cached geocodes only when URL and normalized address still match."""

    old_by_id = {
        _clean(row.get("listing_id")): row
        for row in source_geocoded.to_dict(orient="records")
    }
    rows: list[dict[str, Any]] = []
    metrics = Counter()
    for source in reviewed.to_dict(orient="records"):
        listing_id = _clean(source.get("listing_id"))
        cached = old_by_id.get(listing_id)
        if cached is None:
            raise FidelityRebuildError(f"Cached Stage 3 row is missing: {listing_id}")
        url_matches = _clean(source.get("listing_url")) == _clean(cached.get("listing_url"))
        query = normalize_address(source.get("address"))
        address_matches = _clean(query).casefold() == _clean(
            cached.get("geocode_query")
        ).casefold()
        if not url_matches or not address_matches:
            metrics["rejected_incompatible_geocodes"] += 1
            reason = "source URL" if not url_matches else "normalized address"
            raise FidelityRebuildError(
                f"Cannot reuse geocode for {listing_id}: incompatible {reason}"
            )
        row = dict(source)
        for field in GEOCODE_FIELDS:
            row[field] = cached.get(field, "")
        rows.append(row)
        if _clean(row.get("geocode_status")).casefold() == "missing_address":
            metrics["missing_geocodes"] += 1
        else:
            metrics["reused_geocodes"] += 1
    metrics["rejected_incompatible_geocodes"] += 0
    metrics["new_geocoding_calls"] = 0
    return pd.DataFrame(rows), dict(metrics)


def geocode_recovered_addresses(
    reviewed: pd.DataFrame,
    source_geocoded: pd.DataFrame,
    *,
    cache_csv: Path,
    max_new_geocodes: int,
) -> tuple[pd.DataFrame, dict[str, int], set[str]]:
    """Reuse compatible rows and geocode only deterministic address recoveries."""
    if max_new_geocodes < 0:
        raise FidelityRebuildError("max_new_geocodes cannot be negative")
    old_by_id = {
        _clean(row.get("listing_id")): row
        for row in source_geocoded.to_dict(orient="records")
    }
    reusable: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    recovered_ids: set[str] = set()
    metrics = Counter()
    for source in reviewed.to_dict(orient="records"):
        listing_id = _clean(source.get("listing_id"))
        cached = old_by_id.get(listing_id)
        if cached is None:
            raise FidelityRebuildError(f"Cached Stage 3 row is missing: {listing_id}")
        if _clean(source.get("listing_url")) != _clean(cached.get("listing_url")):
            raise FidelityRebuildError(
                f"Cannot reuse geocode for {listing_id}: incompatible source URL"
            )
        query = normalize_address(source.get("address"))
        old_query = _clean(cached.get("geocode_query"))
        if _clean(query).casefold() == old_query.casefold():
            row = dict(source)
            for field in GEOCODE_FIELDS:
                row[field] = cached.get(field, "")
            reusable.append(row)
            if _clean(row.get("geocode_status")).casefold() == "missing_address":
                metrics["missing_geocodes"] += 1
            else:
                metrics["reused_geocodes"] += 1
            continue
        old_missing = (
            not old_query
            and _clean(cached.get("geocode_status")).casefold() == "missing_address"
        )
        if not old_missing or not query:
            raise FidelityRebuildError(
                f"Cannot reuse geocode for {listing_id}: incompatible normalized address"
            )
        recovered.append(dict(source))
        recovered_ids.add(listing_id)

    if len(recovered) > max_new_geocodes:
        raise FidelityRebuildError(
            f"Recovered {len(recovered)} addresses, exceeding the external geocode cap "
            f"of {max_new_geocodes}"
        )
    if recovered:
        scratch_input = cache_csv.parent / ".recovered-addresses-stage2.csv"
        scratch_output = cache_csv.parent / ".recovered-addresses-stage3.csv"
        _write_csv(pd.DataFrame(recovered), scratch_input)
        load_dotenv(PROJECT_ROOT / ".env")
        api_key = os.getenv("GEOAPIFY_API_KEY")
        stats: dict[str, int] = {}
        try:
            recovered_frame = apply_geocoding(
                input_csv=scratch_input,
                output_csv=scratch_output,
                cache_csv=cache_csv,
                api_key=api_key,
                sleep_seconds=0,
                stats=stats,
            )
        finally:
            scratch_input.unlink(missing_ok=True)
            scratch_output.unlink(missing_ok=True)
        if stats.get("new_api_call_count", 0) > max_new_geocodes:
            raise FidelityRebuildError("Stage 3 exceeded the external geocode cap")
        if stats.get("failed_geocode_count", 0):
            raise FidelityRebuildError("Recovered-address geocoding did not succeed")
        reusable.extend(recovered_frame.to_dict(orient="records"))
        metrics.update(stats)
    else:
        metrics["new_api_call_count"] += 0
        metrics["cache_hit_count"] += 0
        metrics["failed_geocode_count"] += 0

    order = {
        _clean(row.get("listing_id")): index
        for index, row in enumerate(reviewed.to_dict(orient="records"))
    }
    reusable.sort(key=lambda row: order[_clean(row.get("listing_id"))])
    metrics["recovered_address_count"] = len(recovered_ids)
    metrics["new_geocoding_calls"] = metrics.get("new_api_call_count", 0)
    metrics["cache_hit_count"] += metrics.get("reused_geocodes", 0)
    return pd.DataFrame(reusable), dict(metrics), recovered_ids


def _property_signature(row: dict[str, Any]) -> dict[str, Any]:
    candidate = prepare_canonical_listing(row).property
    return {
        "normalized_address": candidate.normalized_address,
        "unit_identifier": candidate.unit_identifier,
        "match_key": candidate.match_key,
    }


def _classification(
    field: str,
    old: Any,
    new: Any,
    *,
    address_recovered: bool = False,
    price_numeric_stable: bool = False,
    old_price_period: Any = None,
    new_price_period: Any = None,
) -> tuple[str, str, str]:
    if field in IDENTITY_FIELDS:
        return "UNEXPECTED_IDENTITY_CHANGE", "stable source identity changed", "identity"
    if field == "address":
        if address_recovered and _unknown(old) and not _unknown(new):
            return (
                "EXPECTED_ADDRESS_RECOVERY",
                "explicit labelled civic address recovered from captured text",
                "captured_description",
            )
        return "UNEXPECTED_ADDRESS_CHANGE", "captured address changed", "website"
    if field in GEOCODE_FIELDS or field in {"map_ready", "geocode_quality_issue"}:
        if address_recovered:
            return (
                "EXPECTED_GEOCODE_FOR_RECOVERED_ADDRESS",
                "normal Stage 3 geocode attached to recovered civic address",
                "geocoder_cache_or_bounded_provider",
            )
        return "UNEXPECTED_GEOCODE_CHANGE", "cached geocode output changed", "geocoder_cache"
    if field in PRICE_FIELDS:
        if field == "price_numeric":
            return "UNEXPECTED_PRICE_VALUE_CHANGE", "numeric price value changed", "website_title"
        if field == "price_monthly":
            if price_numeric_stable and _normalized(old_price_period) != _normalized(
                new_price_period
            ):
                classification = (
                    "EXPECTED_PRICE_PERIOD_RECOVERY"
                    if _unknown(old_price_period) and not _unknown(new_price_period)
                    else "EXPECTED_PRICE_PERIOD_CORRECTION"
                )
                return (
                    classification,
                    "monthly normalization followed the corrected explicit period",
                    "website_title",
                )
            return (
                "UNEXPECTED_PRICE_VALUE_CHANGE",
                "monthly price changed without a justified period correction",
                "website_title",
            )
        if field == "price_period":
            if _unknown(old) and not _unknown(new):
                return (
                    "EXPECTED_PRICE_PERIOD_RECOVERY",
                    "explicit captured period evidence recovered",
                    "website_title",
                )
            return (
                "EXPECTED_PRICE_PERIOD_CORRECTION",
                "unsupported cached period corrected by deterministic evidence",
                "website_title",
            )
        if field == "price_text" and _clean(old) in _clean(new):
            return (
                "EXPECTED_SOURCE_PRIORITY_CORRECTION",
                "preserved explicit source price-period wording",
                "website_title",
            )
        return "UNEXPECTED_PRICE_CHANGE", "price value changed", "website_title"
    if field in CATEGORY_FIELDS:
        return (
            "EXPECTED_CATEGORY_RESTORATION",
            "current source-category normalization preserves Western vocabulary",
            "website_structured_field",
        )
    if field in SOURCE_PRIORITY_FIELDS:
        return (
            "EXPECTED_SOURCE_PRIORITY_CORRECTION",
            "structured/deterministic evidence now outranks cached inference",
            "website_structured_field",
        )
    if _unknown(new) and not _unknown(old):
        if field in {"lease_type", "lease_type_rule", "lease_type_source"}:
            return (
                "EXPECTED_LEASE_DOWNGRADE",
                "unsupported lease certainty downgraded",
                "captured_text_or_unknown",
            )
        return (
            "EXPECTED_UNKNOWN_DOWNGRADE",
            "unsupported certainty was removed by current evidence gates",
            "captured_text_or_unknown",
        )
    if field in SEMANTIC_FIELDS or field.endswith("_ai_evidence_blocked"):
        if (
            field
            in {
                "bathrooms",
                "bathrooms_source",
                "bathroom_type",
                "bathroom_type_rule",
                "bathroom_type_source",
            }
            and _unknown(old)
            and not _unknown(new)
        ):
            return (
                "EXPECTED_BATHROOM_RECOVERY",
                "conservative whole-unit bathroom evidence recovered",
                "captured_description",
            )
        return (
            "EXPECTED_SEMANTIC_CORRECTION",
            "current deterministic or evidence-gated semantics applied",
            "deterministic_or_cached_evidence",
        )
    if field in RAW_CAPTURED_FIELDS:
        return (
            "EXPECTED_SOURCE_PRIORITY_CORRECTION",
            "captured raw field restored to the canonical audit boundary",
            "website_captured",
        )
    if field in {
        "review_flags",
        "review_score",
        "needs_manual_review",
        "ai_skipped",
        "ai_skip_reason",
        "manual_reviewed",
        "manual_review_note",
    } or field.endswith("_source") or field.endswith("_rule"):
        if field in {"review_flags", "review_score", "needs_manual_review"}:
            return (
                "EXPECTED_FURNISHING_REVIEW_CORRECTION",
                "review metadata recomputed after negation-aware furnishing validation",
                "pipeline_provenance",
            )
        return (
            "EXPECTED_PROVENANCE_UPDATE",
            "provenance/review metadata recomputed with current rules",
            "pipeline_provenance",
        )
    if field.startswith("ai_"):
        return (
            "UNEXPECTED_OTHER_CHANGE",
            "cached AI evidence/output changed unexpectedly",
            "cached_ai",
        )
    return "UNEXPECTED_OTHER_CHANGE", "field changed outside expected semantic scope", "unknown"


def compare_canonical(
    old: pd.DataFrame, new: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Create field-level and listing-level diffs, including unchanged listings."""

    old_by_id = {_clean(row["listing_id"]): row for row in old.to_dict(orient="records")}
    new_by_id = {_clean(row["listing_id"]): row for row in new.to_dict(orient="records")}
    if set(old_by_id) != set(new_by_id):
        raise FidelityRebuildError("Candidate listing identity set differs from source canonical")
    fields = sorted(set(old.columns) | set(new.columns))
    diffs: list[dict[str, Any]] = []
    listings: list[dict[str, Any]] = []
    for listing_id in sorted(old_by_id, key=lambda value: (len(value), value)):
        old_row, new_row = old_by_id[listing_id], new_by_id[listing_id]
        changed_fields: list[str] = []
        for field in fields:
            if field == "listing_id" or _normalized(old_row.get(field)) == _normalized(
                new_row.get(field)
            ):
                continue
            classification, reason, evidence = _classification(
                field,
                old_row.get(field),
                new_row.get(field),
                address_recovered=(
                    _unknown(old_row.get("address"))
                    and not _unknown(new_row.get("address"))
                ),
                price_numeric_stable=(
                    _normalized(old_row.get("price_numeric"))
                    == _normalized(new_row.get("price_numeric"))
                ),
                old_price_period=old_row.get("price_period"),
                new_price_period=new_row.get("price_period"),
            )
            if classification not in DIFF_CLASSIFICATIONS:
                raise AssertionError(classification)
            changed_fields.append(field)
            diffs.append(
                {
                    "listing_id": listing_id,
                    "field": field,
                    "old_value": _clean(old_row.get(field)),
                    "new_value": _clean(new_row.get(field)),
                    "classification": classification,
                    "reason_rule": reason,
                    "source_evidence_class": evidence,
                    "confidence_review_state": json.dumps(
                        {
                            "review_score": _clean(new_row.get("review_score")),
                            "needs_manual_review": _clean(
                                new_row.get("needs_manual_review")
                            ),
                        },
                        separators=(",", ":"),
                    ),
                }
            )
        listings.append(
            {
                "listing_id": listing_id,
                "status": "changed" if changed_fields else "unchanged",
                "changed_field_count": len(changed_fields),
                "changed_fields": json.dumps(changed_fields, separators=(",", ":")),
            }
        )

        old_property = _property_signature(old_row)
        new_property = _property_signature(new_row)
        if old_property != new_property:
            address_recovered = _unknown(old_row.get("address")) and not _unknown(
                new_row.get("address")
            )
            diffs.append(
                {
                    "listing_id": listing_id,
                    "field": "__property_match_key",
                    "old_value": json.dumps(old_property, sort_keys=True),
                    "new_value": json.dumps(new_property, sort_keys=True),
                    "classification": (
                        "EXPECTED_ADDRESS_RECOVERY"
                        if address_recovered
                        else "UNEXPECTED_PROPERTY_LINK_CHANGE"
                    ),
                    "reason_rule": (
                        "Stage 4 property candidate gained deterministic identity"
                        if address_recovered
                        else "Stage 4 property candidate changed"
                    ),
                    "source_evidence_class": "stage4_normalization",
                    "confidence_review_state": "{}",
                }
            )
    diff_frame = pd.DataFrame(diffs)
    listing_frame = pd.DataFrame(listings)
    counts = Counter(diff_frame["classification"]) if not diff_frame.empty else Counter()
    unexpected = sum(
        count for name, count in counts.items() if name.startswith("UNEXPECTED_")
    )
    return diff_frame, listing_frame, {
        "changed_listings": int(listing_frame["status"].eq("changed").sum()),
        "unchanged_listings": int(listing_frame["status"].eq("unchanged").sum()),
        "changed_fields": len(diff_frame),
        "classification_counts": dict(sorted(counts.items())),
        "unexpected_change_count": unexpected,
    }


def stability_report(
    old_details: pd.DataFrame,
    old_canonical: pd.DataFrame,
    new_canonical: pd.DataFrame,
    *,
    recovered_address_ids: set[str] | None = None,
) -> dict[str, Any]:
    recovered_address_ids = recovered_address_ids or set()
    old_by_id = {_clean(row["listing_id"]): row for row in old_canonical.to_dict(orient="records")}
    new_by_id = {_clean(row["listing_id"]): row for row in new_canonical.to_dict(orient="records")}
    details_by_id = {_clean(row["listing_id"]): row for row in old_details.to_dict(orient="records")}
    report: dict[str, Any] = {}
    groups = {
        "source_url": ("listing_url",),
        "address": ("address",),
        "geocode": GEOCODE_FIELDS,
    }
    for name, fields in groups.items():
        mismatches = []
        for listing_id, old_row in old_by_id.items():
            if any(
                _normalized(old_row.get(field)) != _normalized(new_by_id[listing_id].get(field))
                for field in fields
            ):
                mismatches.append(listing_id)
        allowed = mismatches if name in {"address", "geocode"} else []
        disallowed = [item for item in mismatches if item not in recovered_address_ids]
        report[name] = {
            "equal": len(old_by_id) - len(mismatches),
            "mismatched": len(mismatches),
            "listing_ids": mismatches,
            "allowed_recovered_address_ids": [
                item for item in allowed if item in recovered_address_ids
            ],
            "disallowed_mismatched": len(disallowed),
        }
    property_mismatches = [
        listing_id
        for listing_id, old_row in old_by_id.items()
        if _property_signature(old_row) != _property_signature(new_by_id[listing_id])
    ]
    report["property_candidate"] = {
        "equal": len(old_by_id) - len(property_mismatches),
        "mismatched": len(property_mismatches),
        "listing_ids": property_mismatches,
        "allowed_recovered_address_ids": [
            item for item in property_mismatches if item in recovered_address_ids
        ],
        "disallowed_mismatched": len(
            [item for item in property_mismatches if item not in recovered_address_ids]
        ),
    }
    raw_mismatches: list[dict[str, str]] = []
    for listing_id, details in details_by_id.items():
        candidate = new_by_id[listing_id]
        for field in RAW_CAPTURED_FIELDS:
            candidate_field = "listing_url" if field == "source_url" else field
            if _normalized(details.get(field)) != _normalized(candidate.get(candidate_field)):
                if listing_id in recovered_address_ids and field == "address":
                    continue
                raw_mismatches.append({"listing_id": listing_id, "field": field})
    report["raw_captured"] = {
        "equal": len(details_by_id) * len(RAW_CAPTURED_FIELDS) - len(raw_mismatches),
        "mismatched": len(raw_mismatches),
        "items": raw_mismatches,
        "disallowed_mismatched": len(raw_mismatches),
    }
    report["passed"] = all(
        group["disallowed_mismatched"] == 0 for group in report.values()
    )
    return report


def _counts(frame: pd.DataFrame, field: str) -> dict[str, int]:
    values = (_clean(value) or "<unknown>" for value in frame.get(field, []))
    return dict(sorted(Counter(values).items()))


def semantic_counts(old: pd.DataFrame, new: pd.DataFrame) -> dict[str, Any]:
    fields = (
        "preferred_gender",
        "housing_type",
        "utilities_status",
        "utilities_included",
        "furnished",
        "is_sublet",
        "availability_category",
        "lease_type",
        "lease_term_months",
        "price_period",
        "price_monthly",
    )
    return {
        field: {"old": _counts(old, field), "new": _counts(new, field)}
        for field in fields
    }


def build_candidate(
    source_run: Path,
    baseline_run: Path,
    candidate_dir: Path,
    validation_dir: Path,
    *,
    cache_csv: Path,
    max_new_geocodes: int = 0,
) -> dict[str, Any]:
    source = RunContext.resume(source_run.resolve())
    baseline = RunContext.resume(baseline_run.resolve())
    source_paths = source.paths
    baseline_paths = baseline.paths
    required = (
        source_paths.stage0_listing_links,
        source_paths.stage1_details,
        source_paths.stage2_reviewed,
        source_paths.stage3_geocoded,
        source_paths.stage3_canonical,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if not baseline_paths.stage3_canonical.is_file():
        missing.append(str(baseline_paths.stage3_canonical))
    if missing:
        raise FidelityRebuildError("Source run is incomplete: " + ", ".join(missing))
    if candidate_dir.exists() and any(candidate_dir.iterdir()):
        raise FidelityRebuildError(f"Candidate directory is not empty: {candidate_dir}")

    source_manifest_sha = sha256_file(source_paths.manifest)
    configuration = {
        "rebuild": {
            "mode": "captured_source_semantic_rebuild_v1",
            "source_run_id": source.manifest["run_id"],
            "baseline_run_id": baseline.manifest["run_id"],
            "source_manifest_sha256": source_manifest_sha,
            "source_canonical_sha256": sha256_file(source_paths.stage3_canonical),
            "baseline_canonical_sha256": sha256_file(
                baseline_paths.stage3_canonical
            ),
            "external_scrape_calls": 0,
            "external_ai_calls": 0,
            "maximum_external_geocoding_calls": max_new_geocodes,
        },
        "stage0": dict(source.manifest.get("configuration", {}).get("stage0", {})),
        "stage1": {"limit": None, "captured_source_only": True},
        "stage2": {"limit": None, "cached_ai_only": True, "network_calls": False},
        "manual_fixes": {"preserve_explicit_manual_provenance": True},
        "stage3": {
            "limit": None,
            "cache_only_for_unchanged_addresses": True,
            "maximum_external_geocoding_calls": max_new_geocodes,
            "cache_path": str(cache_csv.resolve()),
            "source_run_id": source.manifest["run_id"],
        },
        "stage3_qc": dict(
            source.manifest.get("configuration", {}).get("stage3_qc", {})
        ),
    }
    context = RunContext.create_at(
        candidate_dir,
        run_id=candidate_dir.name,
        command=["python", "-m", "scripts.rebuild_listing_fidelity"],
        configuration=configuration,
        git_commit=None,
        git_dirty=None,
    )
    # Capture the actual repository metadata after creation so the candidate
    # records the implementation that produced it.
    from pipeline.run_context import get_git_metadata

    context.manifest["git_commit"], context.manifest["git_dirty"] = get_git_metadata()
    context.manifest["source_run"] = {
        "run_id": source.manifest["run_id"],
        "path": str(source_paths.root),
        "manifest_sha256": source_manifest_sha,
        "stage1_details_sha256": sha256_file(source_paths.stage1_details),
        "stage2_reviewed_sha256": sha256_file(source_paths.stage2_reviewed),
        "stage3_geocoded_sha256": sha256_file(source_paths.stage3_geocoded),
        "stage3_canonical_sha256": sha256_file(source_paths.stage3_canonical),
    }
    context.save()

    stage0_rows = _read_csv(source_paths.stage0_listing_links)
    context.start_stage(
        "stage0", output_paths=[context.paths.stage0_listing_links]
    )
    shutil.copyfile(source_paths.stage0_listing_links, context.paths.stage0_listing_links)
    stage0_metrics = dict(source.manifest["stages"]["stage0"].get("metrics", {}))
    stage0_metrics["captured_source_reused"] = True
    context.finish_stage(
        "stage0",
        output_rows=len(stage0_rows),
        metrics=stage0_metrics,
    )

    source_details = _read_csv(source_paths.stage1_details)
    context.start_stage(
        "stage1",
        input_paths=[source_paths.stage1_details],
        output_paths=[
            context.paths.stage1_details,
            context.paths.stage1_merged,
            context.paths.stage1_website_ready,
        ],
    )
    details, stage1_changes = rebuild_stage1(source_details)
    if len(details) != len(stage0_rows):
        raise FidelityRebuildError("Stage 0/Stage 1 row counts differ")
    _write_csv(details, context.paths.stage1_details)
    merged = merge_with_original_rows(stage0_rows, details)
    _write_csv(merged, context.paths.stage1_merged)
    website_ready = build_website_ready(details)
    _write_csv(website_ready, context.paths.stage1_website_ready)
    context.finish_stage(
        "stage1",
        input_rows=len(source_details),
        output_rows=len(details),
        metrics={
            "captured_source_only": True,
            "deterministic_field_change_counts": stage1_changes,
            "external_request_count": 0,
        },
    )

    cached_reviewed = _read_csv(source_paths.stage2_reviewed)
    context.start_stage(
        "stage2",
        input_paths=[context.paths.stage1_website_ready, source_paths.stage2_reviewed],
        output_paths=[context.paths.stage2_enriched, context.paths.stage2_review_queue],
    )
    enriched, reviewed, stage2_metrics = rebuild_stage2_cached(
        website_ready, cached_reviewed
    )
    _write_csv(enriched, context.paths.stage2_enriched)
    review_queue = enriched[enriched["needs_manual_review"].eq(True)].copy()
    _write_csv(review_queue, context.paths.stage2_review_queue)
    context.finish_stage(
        "stage2",
        input_rows=len(website_ready),
        output_rows=len(enriched),
        warnings=(
            [f"{len(review_queue)} row(s) require manual review."]
            if len(review_queue)
            else []
        ),
        metrics=stage2_metrics,
    )

    context.start_stage(
        "manual_fixes",
        input_paths=[context.paths.stage2_enriched],
        output_paths=[context.paths.stage2_reviewed],
    )
    _write_csv(reviewed, context.paths.stage2_reviewed)
    reviewed_queue = reviewed[reviewed["needs_manual_review"].eq(True)].copy()
    _write_csv(reviewed_queue, context.paths.stage2_review_queue)
    manual_count = int(reviewed.get("manual_reviewed", pd.Series(dtype=bool)).eq(True).sum())
    context.finish_stage(
        "manual_fixes",
        input_rows=len(enriched),
        output_rows=len(reviewed),
        warnings=(
            [f"{len(reviewed_queue)} row(s) still require manual review."]
            if len(reviewed_queue)
            else []
        ),
        metrics={
            "corrected_row_count": manual_count,
            "manual_review_fully_resolved": len(reviewed_queue) == 0,
            "review_count": len(reviewed_queue),
        },
    )

    source_geocoded = _read_csv(source_paths.stage3_geocoded)
    context.start_stage(
        "stage3",
        input_paths=[context.paths.stage2_reviewed, source_paths.stage3_geocoded],
        output_paths=[context.paths.stage3_geocoded],
    )
    try:
        geocoded, geocode_metrics, recovered_address_ids = geocode_recovered_addresses(
            reviewed,
            source_geocoded,
            cache_csv=cache_csv.resolve(),
            max_new_geocodes=max_new_geocodes,
        )
    except Exception as error:
        context.fail_stage("stage3", error)
        raise
    missing_geocode_fields = set(REQUIRED_GEOCODE_FIELDS) - set(geocoded.columns)
    if missing_geocode_fields:
        raise FidelityRebuildError(
            "Reused Stage 3 artifact lacks fields: " + ", ".join(sorted(missing_geocode_fields))
        )
    _write_csv(geocoded, context.paths.stage3_geocoded)
    source_stage3_metrics = source.manifest["stages"]["stage3"].get("metrics", {})
    context.finish_stage(
        "stage3",
        input_rows=len(reviewed),
        output_rows=len(geocoded),
        warnings=(
            [f"{geocode_metrics.get('missing_geocodes', 0)} row(s) have no address."]
            if geocode_metrics.get("missing_geocodes", 0)
            else []
        ),
        metrics={
            **geocode_metrics,
            "cache_hit_count": geocode_metrics.get("cache_hit_count", 0),
            "new_api_call_count": geocode_metrics.get("new_api_call_count", 0),
            "missing_address_count": geocode_metrics.get("missing_geocodes", 0),
            "failed_geocode_count": 0,
            "low_confidence_count": int(
                pd.to_numeric(geocoded["geocode_confidence"], errors="coerce")
                .lt(0.8)
                .sum()
            ),
        },
    )

    context.start_stage(
        "stage3_qc",
        input_paths=[context.paths.stage3_geocoded],
        output_paths=[context.paths.stage3_canonical, context.paths.stage3_geocode_review],
    )
    context.finish_stage(
        "stage3_qc",
        input_rows=len(geocoded),
        output_rows=len(geocoded),
    )
    rebuild_result = rebuild_canonical(context.paths.root)
    context = RunContext.resume(context.paths.root)
    status, completion_warnings = finalize_run(context)

    old_canonical = _read_csv(baseline_paths.stage3_canonical)
    new_canonical = _read_csv(context.paths.stage3_canonical)
    diffs, listing_summary, diff_summary = compare_canonical(old_canonical, new_canonical)
    stability = stability_report(
        source_details,
        old_canonical,
        new_canonical,
        recovered_address_ids=recovered_address_ids,
    )
    if not stability["passed"]:
        raise FidelityRebuildError("Candidate failed identity/address/property/geocode/raw stability gates")
    if diff_summary["unexpected_change_count"]:
        raise FidelityRebuildError(
            f"Candidate has {diff_summary['unexpected_change_count']} unexpected field changes"
        )
    validation_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(diffs, validation_dir / "field-diff.csv")
    _write_csv(listing_summary, validation_dir / "listing-summary.csv")
    summary = {
        "source_run_id": source.manifest["run_id"],
        "baseline_run_id": baseline.manifest["run_id"],
        "baseline_canonical_sha256": sha256_file(
            baseline_paths.stage3_canonical
        ),
        "candidate_run_id": context.manifest["run_id"],
        "candidate_status": status,
        "completion_warnings": completion_warnings,
        "source_artifacts": context.manifest["source_run"],
        "row_count": len(new_canonical),
        "stage1": {
            "external_request_count": 0,
            "deterministic_field_change_counts": stage1_changes,
        },
        "stage2": stage2_metrics,
        "stage3": geocode_metrics,
        "canonical_rebuild": rebuild_result,
        "diff": diff_summary,
        "stability": stability,
        "semantic_counts": semantic_counts(old_canonical, new_canonical),
        "candidate_canonical_sha256": sha256_file(context.paths.stage3_canonical),
    }
    (validation_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--baseline-run", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--validation-dir", type=Path, required=True)
    parser.add_argument(
        "--cache-csv",
        type=Path,
        default=Path("data/processed/geocode_cache.csv"),
    )
    parser.add_argument(
        "--max-new-geocodes",
        type=int,
        default=0,
        help="Bound external Stage 3 calls for deterministically recovered addresses.",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = build_candidate(
        args.source_run.resolve(),
        args.baseline_run.resolve(),
        args.candidate_dir.resolve(),
        args.validation_dir.resolve(),
        cache_csv=args.cache_csv.resolve(),
        max_new_geocodes=args.max_new_geocodes,
    )
    print(
        json.dumps(
            {
                "candidate_run_id": summary["candidate_run_id"],
                "row_count": summary["row_count"],
                "status": summary["candidate_status"],
                "canonical_sha256": summary["candidate_canonical_sha256"],
                "diff": summary["diff"],
                "stability_passed": summary["stability"]["passed"],
                "external_calls": {
                    "scrape": 0,
                    "ai": 0,
                    "geocoding": summary["stage3"].get("new_api_call_count", 0),
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
