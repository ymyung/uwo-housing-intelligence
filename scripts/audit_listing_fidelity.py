"""Read-only field-level fidelity audit for a completed Stage 0-to-Stage 3 run.

The audit uses captured local artifacts only. It never scrapes, calls AI,
geocodes, imports listings, or changes canonical data.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from pipeline.ai_enricher import ai_value_has_semantic_support, evidence_is_verbatim
from pipeline.database_importer import prepare_canonical_listing
from pipeline.uwo_listing_enricher import UWOListingScraper


AUDIT_FIELDS = (
    "address",
    "price",
    "bedrooms",
    "bathrooms",
    "housing_type",
    "lease_type",
    "availability",
    "sublet",
    "gender",
    "furnished",
    "utilities",
)
CLASSIFICATIONS = {
    "MATCH",
    "NORMALIZED_EQUIVALENT",
    "SOURCE_AMBIGUOUS",
    "PIPELINE_ERROR",
    "DB_IMPORT_ERROR",
    "API_ERROR",
    "FRONTEND_ERROR",
    "AI_OVERRIDE_ERROR",
    "NEEDS_HUMAN_REVIEW",
    "NOT_APPLICABLE",
}
SUMMER_CATEGORIES = {"summer", "summer_only", "summer_available"}
UNKNOWN_VALUES = {"", "none", "null", "nan", "unknown", "not_specified"}
BATHROOM_COUNT_RE = re.compile(r"\b(\d+(?:\.5)?)\s*(?:full\s+)?(?:bath|bathroom|washroom)s?\b", re.I)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.casefold() in {"none", "null", "nan"} else text


def _bool(value: Any) -> bool | None:
    text = (_clean(value) or "").casefold()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _number(value: Any) -> float | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _equivalent(left: Any, right: Any) -> bool:
    left_number, right_number = _number(left), _number(right)
    if left_number is not None and right_number is not None:
        return abs(left_number - right_number) <= 0.01
    return (_clean(left) or "").casefold() == (_clean(right) or "").casefold()


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _amenities(row: dict[str, Any]) -> list[str]:
    raw = _clean(row.get("amenities_list"))
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(value) for value in parsed]
        except json.JSONDecodeError:
            pass
    return [value.strip() for value in (_clean(row.get("amenities")) or "").split(",") if value.strip()]


def _stable_key(listing_id: str) -> str:
    return hashlib.sha256(f"mvp-fidelity-v1:{listing_id}".encode()).hexdigest()


def _row_tags(
    details: dict[str, Any],
    canonical: dict[str, Any],
    *,
    review_ids: set[str],
    duplicate_addresses: set[str],
    low_price: float,
    high_price: float,
) -> set[str]:
    tags = {
        f"housing:{_clean(details.get('housing_type_raw')) or 'unknown'}",
        f"bedrooms:{_clean(details.get('bedrooms')) or 'unknown'}",
        f"lease:{_clean(canonical.get('lease_type')) or 'unknown'}",
        f"gender:{_clean(details.get('preferred_gender_raw')) or 'unspecified'}",
        f"furnished:{_bool(details.get('furnished_rule'))}",
        f"utilities:{_clean(details.get('utilities_raw')) or 'unknown'}",
        "sublet:explicit" if UWOListingScraper._detect_sublet(
            details.get("title"),
            details.get("description"),
            details.get("lease_term_raw"),
            details.get("housing_type_raw"),
        ) else "sublet:unstated",
    }
    price = _number(canonical.get("price_monthly"))
    if price is not None and price <= low_price:
        tags.add("price:low")
    if price is not None and price >= high_price:
        tags.add("price:high")
    if UWOListingScraper._parse_availability_category(
        details.get("title"), details.get("description"), details.get("date_available")
    ):
        tags.add("availability:summer")
    if canonical.get("listing_id") in review_ids:
        tags.add("review:queue")
    if _bool(canonical.get("manual_reviewed")):
        tags.add("review:historical")
    address = (_clean(details.get("address")) or "").casefold()
    if address and address in duplicate_addresses:
        tags.add("property:multiple-listings")
    return tags


def select_cohort(
    details_rows: list[dict[str, Any]],
    canonical_rows: list[dict[str, Any]],
    review_rows: list[dict[str, Any]],
    *,
    cohort_size: int,
) -> list[dict[str, Any]]:
    """Greedy deterministic coverage, then stable-hash fill."""
    details_by_id = {row["listing_id"]: row for row in details_rows}
    review_ids = {row["listing_id"] for row in review_rows}
    address_counts = Counter(
        (_clean(row.get("address")) or "").casefold() for row in details_rows
    )
    duplicate_addresses = {value for value, count in address_counts.items() if value and count > 1}
    prices = sorted(
        value
        for row in canonical_rows
        if (value := _number(row.get("price_monthly"))) is not None
    )
    low_price = prices[max(0, len(prices) // 10 - 1)] if prices else 0
    high_price = prices[min(len(prices) - 1, len(prices) * 9 // 10)] if prices else 0
    candidates: list[tuple[dict[str, Any], set[str]]] = []
    for canonical in canonical_rows:
        details = details_by_id.get(canonical["listing_id"])
        if details is None:
            continue
        candidates.append(
            (
                canonical,
                _row_tags(
                    details,
                    canonical,
                    review_ids=review_ids,
                    duplicate_addresses=duplicate_addresses,
                    low_price=low_price,
                    high_price=high_price,
                ),
            )
        )
    targets = set().union(*(tags for _, tags in candidates))
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    uncovered = set(targets)
    while uncovered and len(selected) < min(cohort_size, len(candidates)):
        row, tags = max(
            (item for item in candidates if item[0]["listing_id"] not in selected_ids),
            key=lambda item: (len(item[1] & uncovered), -int(_stable_key(item[0]["listing_id"]), 16)),
        )
        selected.append({"listing_id": row["listing_id"], "selection_reasons": sorted(tags)})
        selected_ids.add(row["listing_id"])
        uncovered -= tags
    for row, tags in sorted(candidates, key=lambda item: _stable_key(item[0]["listing_id"])):
        if len(selected) >= min(cohort_size, len(candidates)):
            break
        if row["listing_id"] not in selected_ids:
            selected.append({"listing_id": row["listing_id"], "selection_reasons": sorted(tags)})
            selected_ids.add(row["listing_id"])
    return selected


def _result(
    listing_id: str,
    field: str,
    classification: str,
    stage: str,
    reason: str,
    *,
    source_value: Any = None,
    product_value: Any = None,
) -> dict[str, Any]:
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"Unsupported classification: {classification}")
    return {
        "listing_id": listing_id,
        "field": field,
        "classification": classification,
        "stage": stage,
        "reason": reason,
        "source_value": _json_value(source_value),
        "product_value": _json_value(product_value),
    }


def _audit_listing(
    details: dict[str, Any], canonical: dict[str, Any]
) -> list[dict[str, Any]]:
    listing_id = canonical["listing_id"]
    results: list[dict[str, Any]] = []

    source_url = _clean(details.get("source_url"))
    product_url = _clean(canonical.get("listing_url"))
    expected_suffix = f"/Listings/Details/{listing_id}"
    identity_matches = (
        details.get("listing_id") == listing_id
        and source_url == product_url
        and bool(source_url and source_url.endswith(expected_suffix))
    )
    results.append(
        _result(
            listing_id,
            "identity",
            "MATCH" if identity_matches else "PIPELINE_ERROR",
            "stage0_to_stage3",
            "Western listing ID and canonical URL are stable"
            if identity_matches
            else "Western listing ID or canonical URL changed",
            source_value={"listing_id": details.get("listing_id"), "url": source_url},
            product_value={"listing_id": listing_id, "url": product_url},
        )
    )

    source_address, product_address = _clean(details.get("address")), _clean(canonical.get("address"))
    if source_address == product_address:
        address_class = "MATCH"
    elif (source_address or "").casefold() == (product_address or "").casefold():
        address_class = "NORMALIZED_EQUIVALENT"
    else:
        address_class = "PIPELINE_ERROR"
    results.append(_result(listing_id, "address", address_class, "stage1_to_stage3", "captured address preserved" if address_class != "PIPELINE_ERROR" else "captured address changed", source_value=source_address, product_value=product_address))

    numeric = _number(details.get("price_numeric"))
    period = _clean(details.get("price_period"))
    expected_monthly = UWOListingScraper._normalize_monthly_price(numeric, period)
    product_monthly = _number(canonical.get("price_monthly"))
    if numeric is None:
        price_class, reason = "SOURCE_AMBIGUOUS", "source price is absent"
    elif period is None:
        price_class, reason = "SOURCE_AMBIGUOUS", "source price period is not explicit"
    elif expected_monthly is not None and product_monthly is not None and abs(expected_monthly - product_monthly) <= 0.01:
        price_class, reason = "NORMALIZED_EQUIVALENT", "monthly conversion matches source amount and period"
    else:
        price_class, reason = "PIPELINE_ERROR", "monthly conversion differs from deterministic source conversion"
    results.append(_result(listing_id, "price", price_class, "stage1", reason, source_value={"text": _clean(details.get("price_text")), "numeric": numeric, "period": period}, product_value=product_monthly))

    expected_bedrooms = UWOListingScraper._to_int(_clean(details.get("bedrooms_raw")))
    product_bedrooms = _number(canonical.get("bedrooms"))
    bedrooms_class = "NORMALIZED_EQUIVALENT" if expected_bedrooms == product_bedrooms else "PIPELINE_ERROR"
    results.append(_result(listing_id, "bedrooms", bedrooms_class, "stage1", "structured bedroom value parsed deterministically", source_value=_clean(details.get("bedrooms_raw")), product_value=product_bedrooms))

    evidence = _clean(canonical.get("ai_evidence_bathrooms"))
    count = _number(canonical.get("bathrooms"))
    bathroom_type = _clean(canonical.get("bathroom_type"))
    rule_type = _clean(details.get("bathroom_type_rule"))
    bathroom_source = (_clean(canonical.get("bathrooms_source")) or "").casefold()
    bathroom_type_source = (
        _clean(canonical.get("bathroom_type_source")) or ""
    ).casefold()
    uses_ai_bathroom = bathroom_source == "ai" or bathroom_type_source == "ai"
    source_series = pd.Series(details)
    if rule_type and bathroom_type != rule_type:
        bathroom_class, bathroom_reason = "AI_OVERRIDE_ERROR", "deterministic bathroom type was not preserved"
    elif rule_type == bathroom_type and rule_type is not None and count is None:
        bathroom_class, bathroom_reason = "NORMALIZED_EQUIVALENT", "structured amenity supplies bathroom type"
    elif count is None and bathroom_type in {None, "unknown"}:
        bathroom_class, bathroom_reason = "SOURCE_AMBIGUOUS", "no supported bathroom count or type"
    elif uses_ai_bathroom and evidence and not evidence_is_verbatim(source_series, evidence):
        bathroom_class, bathroom_reason = "PIPELINE_ERROR", "AI bathroom evidence is not verbatim source text"
    elif count is not None and evidence and uses_ai_bathroom:
        match = BATHROOM_COUNT_RE.search(evidence)
        if match and abs(float(match.group(1)) - count) <= 0.01:
            bathroom_class, bathroom_reason = "NORMALIZED_EQUIVALENT", "bathroom count has verbatim numeric evidence"
        else:
            bathroom_class, bathroom_reason = "NEEDS_HUMAN_REVIEW", "bathroom evidence requires contextual interpretation"
    elif rule_type == bathroom_type:
        bathroom_class, bathroom_reason = "NORMALIZED_EQUIVALENT", "structured amenity supplies bathroom type"
    else:
        bathroom_class, bathroom_reason = "NEEDS_HUMAN_REVIEW", "bathroom value lacks deterministic count evidence"
    results.append(_result(listing_id, "bathrooms", bathroom_class, "stage1" if rule_type else "stage2", bathroom_reason, source_value={"rule_type": rule_type, "evidence": evidence}, product_value={"count": count, "type": bathroom_type}))

    source_housing = _clean(details.get("housing_type_raw"))
    expected_housing = UWOListingScraper._normalize_housing_type(source_housing)
    product_housing = _clean(canonical.get("housing_type"))
    housing_class = "NORMALIZED_EQUIVALENT" if expected_housing == product_housing else "PIPELINE_ERROR"
    results.append(_result(listing_id, "housing_type", housing_class, "stage1", "source category normalized without losing sharing semantics" if housing_class != "PIPELINE_ERROR" else "normalized category loses or changes source meaning", source_value=source_housing, product_value=product_housing))

    explicit_sublet = UWOListingScraper._detect_sublet(details.get("title"), details.get("description"), details.get("lease_term_raw"), details.get("housing_type_raw"))
    expected_months = UWOListingScraper._parse_lease_term_months_rule(details.get("lease_term_raw"))
    expected_lease = UWOListingScraper._parse_lease_type_rule(explicit_sublet, expected_months, details.get("description"))
    product_lease = _clean(canonical.get("lease_type"))
    if expected_lease:
        lease_class = "NORMALIZED_EQUIVALENT" if expected_lease == product_lease else "PIPELINE_ERROR"
        lease_reason = "lease category is a documented deterministic derivation" if lease_class != "PIPELINE_ERROR" else "lease category differs from deterministic duration/sublet derivation"
    elif product_lease in {None, "unknown"}:
        lease_class, lease_reason = "SOURCE_AMBIGUOUS", "source lease duration does not support a category"
    else:
        lease_class, lease_reason = "NEEDS_HUMAN_REVIEW", "lease category is inferred beyond deterministic source terminology"
    results.append(_result(listing_id, "lease_type", lease_class, "stage1" if expected_lease else "stage2", lease_reason, source_value=_clean(details.get("lease_term_raw")), product_value=product_lease))

    expected_summer = UWOListingScraper._parse_availability_category(details.get("title"), details.get("description"), details.get("date_available"))
    product_category = _clean(canonical.get("availability_category"))
    availability_text_matches = _equivalent(details.get("availability_text"), canonical.get("availability_text"))
    if not availability_text_matches:
        availability_class, availability_reason = "PIPELINE_ERROR", "captured availability text changed"
    elif expected_summer and product_category not in SUMMER_CATEGORIES:
        availability_class, availability_reason = "PIPELINE_ERROR", "explicit summer availability was not categorized"
    else:
        availability_class, availability_reason = "MATCH", "availability text and conservative category are preserved"
    results.append(_result(listing_id, "availability", availability_class, "stage1", availability_reason, source_value=_clean(details.get("availability_text")), product_value={"text": _clean(canonical.get("availability_text")), "category": product_category}))

    product_sublet = _bool(canonical.get("is_sublet"))
    if explicit_sublet is True:
        sublet_class = "NORMALIZED_EQUIVALENT" if product_sublet is True else "PIPELINE_ERROR"
        sublet_reason = "explicit source sublet evidence preserved" if sublet_class != "PIPELINE_ERROR" else "explicit source sublet evidence was lost"
    elif product_sublet is True:
        sublet_class, sublet_reason = "PIPELINE_ERROR", "listing was marked sublet without explicit source evidence"
    elif product_sublet is False and str(canonical.get("is_sublet_source") or "").startswith("manual"):
        sublet_class, sublet_reason = "NORMALIZED_EQUIVALENT", "manual review explicitly resolved non-sublet semantics"
    elif product_sublet is False:
        sublet_class, sublet_reason = "PIPELINE_ERROR", "absence of sublet evidence became false certainty"
    else:
        sublet_class, sublet_reason = "SOURCE_AMBIGUOUS", "source does not explicitly state sublet status"
    results.append(_result(listing_id, "sublet", sublet_class, "stage1" if explicit_sublet else "stage2", sublet_reason, source_value=explicit_sublet, product_value=product_sublet))

    expected_gender = UWOListingScraper._normalize_gender(details.get("preferred_gender_raw"), details.get("description"))
    product_gender = _clean(canonical.get("preferred_gender"))
    if expected_gender == product_gender:
        gender_class = "NORMALIZED_EQUIVALENT" if expected_gender != "not_specified" else "SOURCE_AMBIGUOUS"
        gender_reason = "structured preference and explicit restriction semantics are preserved"
    else:
        gender_class, gender_reason = "AI_OVERRIDE_ERROR", "AI/product value conflicts with structured preferred-gender semantics"
    results.append(_result(listing_id, "gender", gender_class, "stage2", gender_reason, source_value=_clean(details.get("preferred_gender_raw")), product_value=product_gender))

    expected_furnished = UWOListingScraper._parse_furnished_rule(_amenities(details), details.get("description"))
    product_furnished = _bool(canonical.get("furnished"))
    furnished_evidence = _clean(canonical.get("ai_evidence_furnished"))
    if expected_furnished is not None:
        furnished_class = "NORMALIZED_EQUIVALENT" if expected_furnished == product_furnished else "AI_OVERRIDE_ERROR"
        furnished_reason = "deterministic furnishing evidence preserved" if furnished_class != "AI_OVERRIDE_ERROR" else "deterministic furnishing evidence was overridden"
    elif product_furnished is None:
        furnished_class, furnished_reason = "SOURCE_AMBIGUOUS", "source has no definitive furnishing evidence"
    elif furnished_evidence and evidence_is_verbatim(source_series, furnished_evidence) and ai_value_has_semantic_support("furnished", product_furnished, furnished_evidence):
        furnished_class, furnished_reason = "NORMALIZED_EQUIVALENT", "AI value has verbatim, semantically explicit source evidence"
    else:
        furnished_class, furnished_reason = "PIPELINE_ERROR", "definitive furnishing value lacks explicit verbatim evidence"
    results.append(_result(listing_id, "furnished", furnished_class, "stage1" if expected_furnished is not None else "stage2", furnished_reason, source_value=expected_furnished, product_value=product_furnished))

    expected_status = UWOListingScraper._parse_utilities_status(_amenities(details), details.get("utilities_raw"), details.get("description"))
    expected_included = UWOListingScraper._utilities_included_from_status(expected_status)
    product_status = _clean(canonical.get("utilities_status"))
    product_included = _bool(canonical.get("utilities_included"))
    if expected_status is None:
        utilities_class = "SOURCE_AMBIGUOUS" if product_included is None else "PIPELINE_ERROR"
        utilities_reason = "source utilities are unspecified" if utilities_class == "SOURCE_AMBIGUOUS" else "missing utility data became a definitive boolean"
    elif expected_status == product_status and expected_included == product_included:
        utilities_class, utilities_reason = "NORMALIZED_EQUIVALENT", "structured utilities field preserved"
    else:
        utilities_class, utilities_reason = "AI_OVERRIDE_ERROR", "structured utilities field conflicts with enriched product value"
    results.append(_result(listing_id, "utilities", utilities_class, "stage2", utilities_reason, source_value={"raw": _clean(details.get("utilities_raw")), "status": expected_status, "all_included": expected_included}, product_value={"status": product_status, "all_included": product_included}))
    return results


def _database_checks(
    database_url: str, cohort_ids: list[str], canonical_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - optional integration dependency
        raise RuntimeError("Database audit requires requirements-database.txt") from exc
    fields = (
        "address", "price_monthly", "bedrooms", "bathrooms", "bathroom_type",
        "housing_type", "lease_type", "availability_text", "availability_category",
        "is_sublet", "preferred_gender", "furnished", "utilities_included", "utilities_status",
    )
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute("set transaction read only")
        rows = connection.execute(
            f"select listing_id, {', '.join(fields)} from public.active_housing_listings where listing_id = any(%s)",
            (cohort_ids,),
        ).fetchall()
    database_by_id = {str(row["listing_id"]): dict(row) for row in rows}
    checks: list[dict[str, Any]] = []
    for listing_id in cohort_ids:
        canonical = canonical_by_id[listing_id]
        database = database_by_id.get(listing_id)
        if database is None:
            checks.append(_result(listing_id, "identity", "DB_IMPORT_ERROR", "stage4", "canonical listing is missing from active database view"))
            continue
        for field in fields:
            canonical_field = field
            left = canonical.get(canonical_field)
            right = database.get(field)
            classification = "MATCH" if _equivalent(left, right) else "DB_IMPORT_ERROR"
            checks.append(_result(listing_id, field, classification, "stage4", "database value matches canonical artifact" if classification == "MATCH" else "database value differs from canonical artifact", source_value=left, product_value=right))
    return checks


def audit_run(
    run_dir: Path,
    *,
    cohort_size: int = 40,
    database_url: str | None = None,
) -> dict[str, Any]:
    details_rows = _read_csv(run_dir / "stage1" / "details.csv")
    reviewed_rows = _read_csv(run_dir / "stage2" / "reviewed.csv")
    review_rows = _read_csv(run_dir / "stage2" / "review_queue.csv")
    canonical_rows = _read_csv(run_dir / "stage3" / "canonical.csv")
    details_by_id = {row["listing_id"]: row for row in details_rows}
    reviewed_by_id = {row["listing_id"]: row for row in reviewed_rows}
    canonical_by_id = {row["listing_id"]: row for row in canonical_rows}
    cohort = select_cohort(details_rows, canonical_rows, review_rows, cohort_size=cohort_size)
    results: list[dict[str, Any]] = []
    for selected in cohort:
        listing_id = selected["listing_id"]
        details = details_by_id[listing_id]
        canonical = canonical_by_id[listing_id]
        results.extend(_audit_listing(details, canonical))
        imported = prepare_canonical_listing(canonical)
        if imported.source_listing_id != listing_id:
            results.append(_result(listing_id, "identity", "DB_IMPORT_ERROR", "stage4", "prepared database identity differs"))
        elif not imported.property.normalized_address:
            results.append(
                _result(
                    listing_id,
                    "property_identity",
                    "NEEDS_HUMAN_REVIEW",
                    "stage4",
                    "listing has no normalized property address",
                )
            )
        else:
            results.append(
                _result(
                    listing_id,
                    "property_identity",
                    "NORMALIZED_EQUIVALENT",
                    "stage4",
                    "listing-to-property candidate retains normalized address and unit semantics",
                    source_value=canonical.get("address"),
                    product_value={
                        "normalized_address": imported.property.normalized_address,
                        "unit_identifier": imported.property.unit_identifier,
                        "match_key": imported.property.match_key,
                    },
                )
            )
        if reviewed_by_id.get(listing_id) is None:
            results.append(_result(listing_id, "identity", "PIPELINE_ERROR", "stage2", "listing is missing from reviewed artifact"))
    database_checks = _database_checks(database_url, [row["listing_id"] for row in cohort], canonical_by_id) if database_url else []
    metrics: dict[str, dict[str, int]] = {}
    for field in AUDIT_FIELDS:
        field_results = [row for row in results if row["field"] == field]
        counts = Counter(row["classification"] for row in field_results)
        metrics[field] = {
            "audited": len(field_results),
            "exact_or_normalized": counts["MATCH"] + counts["NORMALIZED_EQUIVALENT"],
            "source_ambiguous": counts["SOURCE_AMBIGUOUS"],
            "confirmed_errors": sum(
                counts[value]
                for value in (
                    "PIPELINE_ERROR", "DB_IMPORT_ERROR", "API_ERROR",
                    "FRONTEND_ERROR", "AI_OVERRIDE_ERROR",
                )
            ),
            "manual_review": counts["NEEDS_HUMAN_REVIEW"],
            "classifications": dict(sorted(counts.items())),
        }
    error_stages = Counter(
        row["stage"]
        for row in results + database_checks
        if row["classification"] in {
            "PIPELINE_ERROR", "DB_IMPORT_ERROR", "API_ERROR",
            "FRONTEND_ERROR", "AI_OVERRIDE_ERROR",
        }
    )
    return {
        "audit_version": "mvp_listing_fidelity_v1",
        "run_id": run_dir.name,
        "cohort_size": len(cohort),
        "selection_method": "deterministic greedy edge/category coverage with SHA-256 stable fill",
        "cohort": cohort,
        "metrics": metrics,
        "confirmed_error_stage_counts": dict(sorted(error_stages.items())),
        "results": results,
        "database_checks": database_checks,
    }


def _write_outputs(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "audit.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "audit_version", "run_id", "cohort_size", "selection_method",
                    "metrics", "confirmed_error_stage_counts",
                )
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    with (output_dir / "cohort.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("listing_id", "selection_reasons"))
        writer.writeheader()
        for row in report["cohort"]:
            writer.writerow({"listing_id": row["listing_id"], "selection_reasons": json.dumps(row["selection_reasons"])})
    result_fields = ("listing_id", "field", "classification", "stage", "reason", "source_value", "product_value")
    with (output_dir / "field-results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=result_fields)
        writer.writeheader()
        for row in report["results"] + report["database_checks"]:
            writer.writerow({key: json.dumps(row[key], ensure_ascii=False) if isinstance(row[key], (dict, list)) else row[key] for key in result_fields})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cohort-size", type=int, default=40)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--database-url-env",
        help="Optional environment variable containing a local read-only audit target URL.",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cohort_size < 1:
        raise ValueError("--cohort-size must be positive")
    database_url = os.getenv(args.database_url_env, "").strip() if args.database_url_env else None
    if args.database_url_env and not database_url:
        raise ValueError(f"{args.database_url_env} is not configured")
    output_dir = args.output_dir or Path("data/mvp-fidelity-validation") / args.run_dir.name
    report = audit_run(args.run_dir.resolve(), cohort_size=args.cohort_size, database_url=database_url)
    _write_outputs(report, output_dir.resolve())
    print(json.dumps({"run_id": report["run_id"], "cohort_size": report["cohort_size"], "metrics": report["metrics"], "confirmed_error_stage_counts": report["confirmed_error_stage_counts"]}, indent=2))
    print(f"Audit artifacts: {output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
