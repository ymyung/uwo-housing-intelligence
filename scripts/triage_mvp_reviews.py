"""Read-only, evidence-only MVP review triage for an approved local run.

The command reads captured Stage 1/2/3 CSVs, the exported City shadow audit,
and (when configured) current PostgreSQL product projections.  It never writes
to the run or database and has no scraper, AI, or geocoder integration.
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
from urllib.parse import urlparse

from pipeline.run_approval import sha256_file
from pipeline.uwo_listing_enricher import (
    SUBLET_RE,
    UWOListingScraper,
)


DISPOSITIONS = {
    "RESOLVED_DETERMINISTIC",
    "LEGITIMATE_UNKNOWN",
    "SOURCE_AMBIGUOUS",
    "SOURCE_CONFLICT",
    "GEOCODE_CONFIDENCE_REVIEW",
    "PIPELINE_DEFECT",
    "MVP_BLOCKING",
    "POST_MVP_REVIEW",
}
SEVERITIES = {"P0", "P1", "P2", "P3"}
GEOCODE_EVIDENCE = {
    "SUPPORTED_BY_CITY",
    "SUPPORTED_WITH_MINOR_OFFSET",
    "SIGNIFICANT_CITY_DISAGREEMENT",
    "CITY_UNRESOLVED",
    "SOURCE_ADDRESS_INCOMPLETE",
    "POSSIBLE_GEOCODER_ERROR",
    "POSSIBLE_PROPERTY_IDENTITY_ISSUE",
}
LOCATION_VISIBILITY_POLICY_VERSION = "location-visibility-v1"
LOOPBACK_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}
UNKNOWN = {"", "none", "null", "nan", "unknown", "not_specified"}
ADDRESS_STOP_WORDS = {
    "london", "on", "ontario", "canada", "street", "st", "avenue", "ave",
    "road", "rd", "drive", "dr", "court", "crt", "crescent", "cres", "lane",
    "place", "boulevard", "blvd", "north", "south", "east", "west", "unit",
}
FURNITURE_ITEM_RE = re.compile(
    r"\b(?:bed|desk|dresser|table|chair|sofa|couch|wardrobe|mattress)\b", re.I
)
EXPLICIT_FURNISHED_RE = re.compile(r"\b(?:fully\s+)?furnished\b|\bfurniture\s+incl", re.I)
EXPLICIT_UNFURNISHED_RE = re.compile(
    r"\b(?:unfurnished|not\s+furnished)\b|\bfurnishings?\s+(?:are\s+)?not\s+included\b",
    re.I,
)
PRIVATE_BATH_RE = re.compile(r"\b(?:private|own|en-?suite)\s+(?:bath|bathroom)\b", re.I)
SHARED_BATH_RE = re.compile(r"\bshared?\s+(?:bath|bathroom)\b|\bshare\s+(?:a|the)\s+bathroom\b", re.I)
BATH_COUNT_RE = re.compile(r"\b(?:\d+(?:\.5)?|one|two|three|four|five|six)\s*-?\s*(?:full\s+)?(?:bath|bathroom)s?\b", re.I)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = sorted({key for row in materialized for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in materialized:
            writer.writerow({key: _serializable(row.get(key)) for key in columns})


def _serializable(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return value


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


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.casefold() in UNKNOWN else text


def _bool(value: Any) -> bool | None:
    text = (_clean(value) or "").casefold()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _float(value: Any) -> float | None:
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def classify_location_visibility(
    row: dict[str, Any],
    geocode_review: dict[str, Any] | None,
    *,
    city_distance_meters: float | None,
    city_significant: bool,
) -> tuple[str, tuple[str, ...]]:
    """Classify captured evidence without resolving or changing coordinates."""

    if _float(row.get("latitude")) is None or _float(row.get("longitude")) is None:
        return "missing_location", ("missing_location",)

    reasons: set[str] = set()
    evidence = _clean((geocode_review or {}).get("city_evidence"))
    if evidence == "SIGNIFICANT_CITY_DISAGREEMENT":
        reasons.add("city_reference_disagreement")
    if evidence == "POSSIBLE_GEOCODER_ERROR":
        reasons.add("geocoder_result_conflict")
    if _clean((geocode_review or {}).get("geocode_result_type")) in {
        "city",
        "district",
    }:
        reasons.add("broad_geocode_result")
    if city_significant:
        reasons.add("city_reference_offset")
    if reasons:
        return "exclude_from_map_demo", tuple(sorted(reasons))

    if not _bool(row.get("map_ready")):
        reasons.add("canonical_map_not_ready")
    if city_distance_meters is not None and city_distance_meters > 50:
        reasons.add("city_reference_minor_offset")
    if reasons:
        return "displayable_with_known_limitation", tuple(sorted(reasons))
    return "clearly_safe", ()


def _flags(row: dict[str, Any]) -> list[str]:
    value = row.get("review_flags")
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _stable_key(value: str, namespace: str) -> str:
    return hashlib.sha256(f"{namespace}:{value}".encode()).hexdigest()


def _address_tokens(value: Any) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", (_clean(value) or "").casefold())
        if len(token) > 1 and token not in ADDRESS_STOP_WORDS
    }


def _address_result_conflicts(row: dict[str, Any]) -> bool:
    source = _address_tokens(row.get("address"))
    result = _address_tokens(row.get("geocode_formatted"))
    if not source or not result:
        return False
    source_numbers = {token for token in source if token.isdigit()}
    result_numbers = {token for token in result if token.isdigit()}
    if source_numbers and result_numbers and not source_numbers.intersection(result_numbers):
        return True
    source_words = {token for token in source if not token.isdigit()}
    return bool(source_words and len(source_words.intersection(result)) / len(source_words) < 0.5)


def city_distance_band(distance: float) -> str:
    if distance <= 25:
        return "0-25 m"
    if distance <= 50:
        return "25-50 m"
    if distance <= 100:
        return "50-100 m"
    if distance <= 200:
        return "100-200 m"
    if distance <= 400:
        return "200-400 m"
    return ">400 m"


def classify_geocode(
    row: dict[str, Any], city: dict[str, Any] | None, *, city_p95_meters: float
) -> tuple[str, str, str]:
    """Return City evidence class, review disposition, and MVP severity."""
    if not _clean(row.get("address")) or _clean(row.get("geocode_status")) != "ok":
        return "SOURCE_ADDRESS_INCOMPLETE", "LEGITIMATE_UNKNOWN", "P2"

    city_method = _clean((city or {}).get("address_match_method"))
    distance = _float((city or {}).get("distance_meters"))
    if city_method in {"EXACT_UNIT_MATCH", "EXACT_CIVIC_MATCH", "NORMALIZED_MATCH"} and distance is not None:
        if distance <= 50:
            return "SUPPORTED_BY_CITY", "GEOCODE_CONFIDENCE_REVIEW", "P3"
        if distance <= city_p95_meters:
            return "SUPPORTED_WITH_MINOR_OFFSET", "GEOCODE_CONFIDENCE_REVIEW", "P2"
        return "SIGNIFICANT_CITY_DISAGREEMENT", "GEOCODE_CONFIDENCE_REVIEW", "P1"

    if _clean(row.get("geocode_result_type")) in {"city", "district"}:
        return "CITY_UNRESOLVED", "GEOCODE_CONFIDENCE_REVIEW", "P1"
    if _address_result_conflicts(row):
        return "POSSIBLE_GEOCODER_ERROR", "GEOCODE_CONFIDENCE_REVIEW", "P1"
    return "CITY_UNRESOLVED", "GEOCODE_CONFIDENCE_REVIEW", "P2"


def classify_price(row: dict[str, Any]) -> tuple[str, str, str, str | None]:
    amount = _float(row.get("price_numeric"))
    recomputed = UWOListingScraper._infer_price_period(
        _clean(row.get("title")), _clean(row.get("description")), amount
    )
    current = _clean(row.get("price_period"))
    if current is None and recomputed is not None:
        return "PERIOD_RECOVERABLE_EXPLICIT", "PIPELINE_DEFECT", "P1", recomputed
    if current is None:
        return "PERIOD_TRULY_UNSPECIFIED", "LEGITIMATE_UNKNOWN", "P2", None
    if current != recomputed:
        return "PIPELINE_DEFECT", "MVP_BLOCKING", "P0", recomputed
    return "PERIOD_CONFIRMED", "RESOLVED_DETERMINISTIC", "P3", recomputed


def _sample(rows: list[dict[str, Any]], size: int, namespace: str) -> list[dict[str, Any]]:
    """Deterministic category-first sample with stable-hash fill."""
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for tag in row.get("sample_tags", []):
            by_tag.setdefault(tag, []).append(row)
    for tag in sorted(by_tag):
        candidate = min(
            by_tag[tag], key=lambda item: _stable_key(str(item["listing_id"]), namespace + tag)
        )
        identity = str(candidate["listing_id"])
        if identity not in seen and len(chosen) < size:
            chosen.append(candidate)
            seen.add(identity)
    remaining = sorted(
        (row for row in rows if str(row["listing_id"]) not in seen),
        key=lambda item: _stable_key(str(item["listing_id"]), namespace),
    )
    return (chosen + remaining)[:size]


def _database_rows(database_url: str) -> list[dict[str, Any]]:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.hostname not in LOOPBACK_DATABASE_HOSTS:
        raise ValueError("MVP review triage only permits a loopback PostgreSQL URL")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - dependency is required by Stage 4
        raise RuntimeError("psycopg is required for database-backed triage") from exc

    query = """
        select
            l.source_listing_id as listing_id,
            l.id as database_listing_id,
            l.property_id,
            p.normalized_address,
            p.address_complete,
            score.ranking_status,
            score.overall_score,
            score.explanation as ranking_explanation,
            coalesce(profile.exact_profile_count, 0) as exact_profile_count,
            coalesce(surface.walk_surface_ready, false) as walk_surface_ready
        from public.housing_listings l
        left join public.housing_properties p on p.id = l.property_id
        left join public.housing_listing_scores score
          on score.listing_id = l.id
         and score.ranking_version = 'ranking-v1'
         and score.is_current
        left join lateral (
            select count(*) as exact_profile_count
            from public.housing_accessibility_profiles ap
            where ap.origin_property_id = l.property_id
              and not ap.is_stale
              and ap.result_type in ('exact_route', 'cached_exact_property', 'cached_exact_origin')
        ) profile on true
        left join lateral (
            select bool_or(surface.status = 'ready') as walk_surface_ready
            from public.housing_walk_time_surfaces surface
            where surface.property_id = l.property_id
        ) surface on true
        where l.source = 'uwo_offcampus' and l.status = 'active'
        order by l.source_listing_id
    """
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute("set transaction read only")
        return [dict(row) for row in connection.execute(query).fetchall()]


def _ranking_reasons(row: dict[str, Any]) -> list[str]:
    explanation = row.get("ranking_explanation") or {}
    if isinstance(explanation, str):
        try:
            explanation = json.loads(explanation)
        except json.JSONDecodeError:
            return []
    reasons = explanation.get("eligibility_reasons", []) if isinstance(explanation, dict) else []
    return [str(value) for value in reasons] if isinstance(reasons, list) else []


def _demo_shortlist(
    rows: list[dict[str, Any]], db_by_listing: dict[str, dict[str, Any]], unsafe: set[str]
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    prices = sorted(
        value for row in rows if (value := _float(row.get("price_monthly"))) is not None
    )
    low = prices[len(prices) // 4] if prices else 0
    high = prices[(len(prices) * 3) // 4] if prices else 0
    for row in rows:
        listing_id = str(row.get("listing_id"))
        db = db_by_listing.get(listing_id, {})
        if (
            listing_id in unsafe
            or db.get("ranking_status") != "ranked"
            or int(db.get("exact_profile_count") or 0) == 0
        ):
            continue
        tags = {"ranking", "routing"}
        price = _float(row.get("price_monthly"))
        if price is not None:
            tags.add("price:low" if price <= low else "price:high" if price >= high else "price:middle")
        if _clean(row.get("housing_type")):
            tags.add(f"housing:{row['housing_type']}")
        gender = _clean(row.get("preferred_gender"))
        if gender and gender != "not_specified":
            tags.add("gender")
        if _bool(row.get("is_sublet")) is True:
            tags.add("explicit_sublet")
        if _clean(row.get("availability_category")) == "summer_available" and _bool(row.get("is_sublet")) is not True:
            tags.add("summer_non_sublet")
        if _clean(row.get("utilities_status")):
            tags.add(f"utilities:{row['utilities_status']}")
        if db.get("walk_surface_ready"):
            tags.add("walking_surface")
        candidates.append(
            {
                "listing_id": listing_id,
                "property_id": db.get("property_id"),
                "address": row.get("address"),
                "price_monthly": price,
                "housing_type": row.get("housing_type"),
                "ranking_score": db.get("overall_score"),
                "exact_profile_count": db.get("exact_profile_count"),
                "walk_surface_ready": bool(db.get("walk_surface_ready")),
                "sample_tags": sorted(tags),
            }
        )

    wanted = {
        "price:low", "price:middle", "price:high", "gender", "explicit_sublet",
        "summer_non_sublet", "walking_surface", "utilities:all_included",
        "utilities:not_included", "ranking", "routing",
    }
    chosen: list[dict[str, Any]] = []
    remaining = list(candidates)
    while remaining and len(chosen) < 12:
        candidate = max(
            remaining,
            key=lambda item: (
                len(set(item["sample_tags"]).intersection(wanted)),
                float(item.get("ranking_score") or 0),
                _stable_key(str(item["listing_id"]), "demo"),
            ),
        )
        chosen.append(candidate)
        wanted.difference_update(candidate["sample_tags"])
        remaining.remove(candidate)
        if not wanted and len(chosen) >= 8:
            break
    return chosen


def triage(
    *, run_dir: Path, city_audit_path: Path, city_summary_path: Path,
    database_url: str, output_dir: Path
) -> dict[str, Any]:
    canonical_path = run_dir / "stage3" / "canonical.csv"
    details_path = run_dir / "stage1" / "details.csv"
    geocode_review_path = run_dir / "stage3" / "geocode_review.csv"
    for required in (canonical_path, details_path, geocode_review_path, city_audit_path, city_summary_path):
        if not required.is_file():
            raise FileNotFoundError(required)

    canonical = _read_csv(canonical_path)
    details_by_id = {str(row["listing_id"]): row for row in _read_csv(details_path)}
    geocode_review = _read_csv(geocode_review_path)
    city_by_property = {str(row["property_id"]): row for row in _read_csv(city_audit_path)}
    city_summary = json.loads(city_summary_path.read_text(encoding="utf-8-sig"))
    city_p95 = float(city_summary["distance_meters"]["p95"])
    db_rows = _database_rows(database_url)
    db_by_listing = {str(row["listing_id"]): row for row in db_rows}
    listing_ids_by_property: dict[str, list[str]] = {}
    for row in db_rows:
        listing_ids_by_property.setdefault(str(row.get("property_id")), []).append(str(row["listing_id"]))

    review_items: list[dict[str, Any]] = []
    geocode_rows: list[dict[str, Any]] = []
    risky_listing_ids: set[str] = set()
    distance_bands: Counter[str] = Counter()
    for row in geocode_review:
        listing_id = str(row["listing_id"])
        property_id = str(db_by_listing.get(listing_id, {}).get("property_id") or "")
        city = city_by_property.get(property_id)
        evidence, disposition, severity = classify_geocode(row, city, city_p95_meters=city_p95)
        distance = _float((city or {}).get("distance_meters"))
        if distance is not None:
            distance_bands[city_distance_band(distance)] += 1
        if severity == "P1":
            risky_listing_ids.add(listing_id)
        item = {
            "issue_type": "geocode",
            "listing_id": listing_id,
            "property_id": property_id,
            "listing_ids": listing_ids_by_property.get(property_id, [listing_id]),
            "address": row.get("address"),
            "geocode_status": row.get("geocode_status"),
            "geocode_result_type": row.get("geocode_result_type"),
            "geocode_formatted": row.get("geocode_formatted"),
            "city_evidence": evidence,
            "city_address_match_method": (city or {}).get("address_match_method"),
            "city_building_match_method": (city or {}).get("building_match_method"),
            "city_parcel_match_method": (city or {}).get("parcel_match_method"),
            "distance_meters": distance,
            "disposition": disposition,
            "severity": severity,
            "reason": row.get("geocode_quality_issue") or evidence,
        }
        geocode_rows.append(item)
        review_items.append(item)

    price_rows: list[dict[str, Any]] = []
    for row in canonical:
        classification, disposition, severity, candidate = classify_price(row)
        if classification == "PERIOD_CONFIRMED":
            continue
        item = {
            "issue_type": "price",
            "listing_id": row["listing_id"],
            "property_id": db_by_listing.get(str(row["listing_id"]), {}).get("property_id"),
            "listing_ids": [str(row["listing_id"])],
            "price_text": row.get("price_text"),
            "price_numeric": row.get("price_numeric"),
            "current_price_period": row.get("price_period"),
            "candidate_price_period": candidate,
            "evidence_classification": classification,
            "disposition": disposition,
            "severity": severity,
            "reason": "period tied to identical advertised amount" if candidate else "captured source does not establish advertised price period",
        }
        price_rows.append(item)
        review_items.append(item)

    missing_address: list[dict[str, Any]] = []
    for row in canonical:
        if _clean(row.get("address")):
            continue
        detail = details_by_id[str(row["listing_id"])]
        recovered = UWOListingScraper._recover_address_from_description(_clean(detail.get("description")))
        item = {
            "issue_type": "missing_address",
            "listing_id": row["listing_id"],
            "property_id": db_by_listing.get(str(row["listing_id"]), {}).get("property_id"),
            "listing_ids": [str(row["listing_id"])],
            "address_raw": detail.get("address_raw"),
            "candidate_address": recovered,
            "evidence_classification": "PIPELINE_DEFECT" if recovered else "SOURCE_ADDRESS_INCOMPLETE",
            "disposition": "MVP_BLOCKING" if recovered else "LEGITIMATE_UNKNOWN",
            "severity": "P0" if recovered else "P2",
            "reason": "explicit civic address captured in description" if recovered else "captured source has no usable civic address",
        }
        missing_address.append(item)
        review_items.append(item)

    bathroom_population: list[dict[str, Any]] = []
    furnishing_population: list[dict[str, Any]] = []
    for row in canonical:
        listing_id = str(row["listing_id"])
        flags = _flags(row)
        description = _clean(row.get("description")) or ""
        bathroom_flags = [flag for flag in flags if "bathroom" in flag]
        if bathroom_flags:
            candidate = UWOListingScraper._parse_bathrooms_rule(description)
            tags = []
            if BATH_COUNT_RE.search(description): tags.append("numeric")
            if PRIVATE_BATH_RE.search(description): tags.append("private")
            if SHARED_BATH_RE.search(description): tags.append("shared")
            if candidate and candidate > 1: tags.append("multiple")
            if any("evidence_blocked" in flag for flag in bathroom_flags): tags.append("ai_evidence_blocked")
            item = {
                "issue_type": "bathrooms",
                "listing_id": listing_id,
                "current_bathrooms": row.get("bathrooms"),
                "candidate_bathrooms": candidate,
                "review_flags": bathroom_flags,
                "sample_tags": tags or ["ambiguous"],
                "evidence_classification": "PIPELINE_DEFECT" if candidate is not None and _clean(row.get("bathrooms")) is None else "SOURCE_AMBIGUOUS",
                "disposition": "RESOLVED_DETERMINISTIC" if candidate is not None and _clean(row.get("bathrooms")) is None else "LEGITIMATE_UNKNOWN",
                "severity": "P1" if candidate is not None and _clean(row.get("bathrooms")) is None else "P2",
                "reason": "explicit whole-unit bathroom total" if candidate is not None else "no unambiguous whole-unit bathroom total",
            }
            bathroom_population.append(item)
            review_items.append(item)

        furnishing_flags = [flag for flag in flags if "furnished" in flag]
        if _clean(row.get("furnished")) is None or furnishing_flags:
            parsed = UWOListingScraper._parse_furnished_rule([], description)
            has_items = bool(FURNITURE_ITEM_RE.search(description))
            tags = ["explicit" if parsed is not None else "partial_items" if has_items else "unstated"]
            if furnishing_flags: tags.append("review_flag")
            positive_after_negation = EXPLICIT_FURNISHED_RE.search(
                EXPLICIT_UNFURNISHED_RE.sub("", description)
            )
            conflict = bool(
                "furnished_false_but_description_mentions_furnished" in furnishing_flags
                and positive_after_negation
            )
            false_positive_flag = bool(
                "furnished_false_but_description_mentions_furnished" in furnishing_flags
                and not positive_after_negation
            )
            item = {
                "issue_type": "furnished",
                "listing_id": listing_id,
                "current_furnished": row.get("furnished"),
                "deterministic_value": parsed,
                "review_flags": furnishing_flags,
                "sample_tags": tags,
                "evidence_classification": "SOURCE_CONFLICT" if conflict else "PIPELINE_DEFECT" if false_positive_flag else "LEGITIMATE_UNKNOWN",
                "disposition": "POST_MVP_REVIEW" if conflict else "RESOLVED_DETERMINISTIC" if false_positive_flag else "LEGITIMATE_UNKNOWN",
                "severity": "P2" if conflict or not false_positive_flag else "P3",
                "reason": "conflicting rental/common-area furnishing language" if conflict else "negated wording caused a false-positive review flag" if false_positive_flag else "rental furnishing is not explicitly stated",
            }
            furnishing_population.append(item)
            review_items.append(item)

    lease_defects: list[dict[str, Any]] = []
    utilities_conflicts: list[str] = []
    gender_review: set[str] = set()
    gender_contradictions: set[str] = set()
    lease_review: set[str] = set()
    for row in canonical:
        listing_id = str(row["listing_id"])
        flags = _flags(row)
        description = _clean(row.get("description")) or ""
        raw_months = UWOListingScraper._parse_lease_term_months_rule(_clean(row.get("lease_term_raw")))
        deterministic_type = UWOListingScraper._parse_lease_type_rule(
            _bool(row.get("is_sublet")), raw_months, description
        )
        current_type = _clean(row.get("lease_type"))
        unsupported_ai_sublet = (
            current_type == "sublet"
            and _clean(row.get("lease_type_source")) == "ai"
            and not SUBLET_RE.search(description)
        )
        if current_type not in {None, "unknown"} and (
            unsupported_ai_sublet or (deterministic_type and current_type != deterministic_type)
        ):
            item = {
                "issue_type": "lease",
                "listing_id": listing_id,
                "current_lease_type": current_type,
                "candidate_lease_type": deterministic_type,
                "evidence_classification": "PIPELINE_DEFECT",
                "disposition": "MVP_BLOCKING",
                "severity": "P0",
                "reason": "lease type is unsupported by explicit captured evidence",
            }
            lease_defects.append(item)
            review_items.append(item)
        if any("gender" in flag for flag in flags):
            gender_review.add(listing_id)
        if any("gender" in flag and "evidence_blocked" not in flag for flag in flags):
            gender_contradictions.add(listing_id)
        if any("lease" in flag for flag in flags):
            lease_review.add(listing_id)
        if "utilities_included_contradicts_description" in flags:
            utilities_conflicts.append(listing_id)

    canonical_by_id = {str(row["listing_id"]): row for row in canonical}
    city_significant_by_property: dict[str, dict[str, Any]] = {}
    for property_id, city in city_by_property.items():
        distance = _float(city.get("distance_meters"))
        if (
            _clean(city.get("address_match_method"))
            in {"EXACT_UNIT_MATCH", "EXACT_CIVIC_MATCH", "NORMALIZED_MATCH"}
            and distance is not None
            and distance > city_p95
        ):
            city_significant_by_property[property_id] = city

    reviewed_property_ids = {str(row["property_id"]) for row in geocode_rows}
    shadow_significant_outside_review = 0
    for property_id, city in city_significant_by_property.items():
        if property_id in reviewed_property_ids:
            continue
        listing_ids = listing_ids_by_property.get(property_id, [])
        representative = canonical_by_id.get(listing_ids[0], {}) if listing_ids else {}
        review_items.append(
            {
                "issue_type": "city_shadow_location",
                "listing_id": listing_ids[0] if listing_ids else None,
                "listing_ids": listing_ids,
                "property_id": property_id,
                "address": representative.get("address"),
                "city_evidence": "SIGNIFICANT_CITY_DISAGREEMENT",
                "city_address_match_method": city.get("address_match_method"),
                "city_building_match_method": city.get("building_match_method"),
                "city_parcel_match_method": city.get("parcel_match_method"),
                "distance_meters": _float(city.get("distance_meters")),
                "disposition": "GEOCODE_CONFIDENCE_REVIEW",
                "severity": "P1",
                "reason": "map-ready coordinate exceeds the observed City shadow-evidence p95",
            }
        )
        shadow_significant_outside_review += 1

    db_listing_count = len(db_rows)
    map_counts: Counter[str] = Counter()
    map_category_by_listing: dict[str, str] = {}
    map_reasons_by_listing: dict[str, tuple[str, ...]] = {}
    geo_by_listing = {str(row["listing_id"]): row for row in geocode_rows}
    for row in canonical:
        listing_id = str(row["listing_id"])
        geo = geo_by_listing.get(listing_id)
        property_id = str(db_by_listing.get(listing_id, {}).get("property_id") or "")
        city_distance = _float(city_by_property.get(property_id, {}).get("distance_meters"))
        category, reason_codes = classify_location_visibility(
            row,
            geo,
            city_distance_meters=city_distance,
            city_significant=property_id in city_significant_by_property,
        )
        if category == "exclude_from_map_demo":
            risky_listing_ids.add(listing_id)
        map_counts[category] += 1
        map_category_by_listing[listing_id] = category
        map_reasons_by_listing[listing_id] = reason_codes

    category_priority = {
        "clearly_safe": 0,
        "displayable_with_known_limitation": 1,
        "exclude_from_map_demo": 2,
        "missing_location": 3,
    }
    property_categories: dict[str, str] = {}
    property_reasons: dict[str, set[str]] = {}
    for row in db_rows:
        listing_id = str(row["listing_id"])
        property_id = str(row.get("property_id"))
        category = map_category_by_listing[listing_id]
        current = property_categories.get(property_id)
        if current is None or category_priority[category] > category_priority[current]:
            property_categories[property_id] = category
        property_reasons.setdefault(property_id, set()).update(
            map_reasons_by_listing[listing_id]
        )
    property_map_counts = Counter(property_categories.values())
    canonical_fingerprint = sha256_file(canonical_path)
    public_status = {
        "clearly_safe": ("available", True, True),
        "displayable_with_known_limitation": ("limited", True, False),
        "exclude_from_map_demo": ("unavailable", False, False),
        "missing_location": ("unavailable", False, False),
    }
    location_visibility = []
    for property_id, category in sorted(
        property_categories.items(), key=lambda item: int(item[0])
    ):
        status, map_visible, route_available = public_status[category]
        location_visibility.append(
            {
                "property_id": property_id,
                "policy_version": LOCATION_VISIBILITY_POLICY_VERSION,
                "location_status": status,
                "map_visible": map_visible,
                "route_available": route_available,
                "reason_codes": sorted(property_reasons[property_id]),
                "source_run_id": run_dir.name,
                "source_fingerprint": canonical_fingerprint,
            }
        )
    public_location_counts = Counter(
        row["location_status"] for row in location_visibility
    )

    db_by_listing = {str(row["listing_id"]): row for row in db_rows}
    ranking_missing = sum(
        "missing_monthly_price" in _ranking_reasons(row) for row in db_rows
    )
    price_only = sum(_ranking_reasons(row) == ["missing_monthly_price"] for row in db_rows)
    recoverable_ids = {
        str(row["listing_id"]) for row in price_rows
        if row["evidence_classification"] == "PERIOD_RECOVERABLE_EXPLICIT"
    }
    price_only_recoverable = sum(
        listing_id in recoverable_ids and _ranking_reasons(db_by_listing.get(listing_id, {})) == ["missing_monthly_price"]
        for listing_id in recoverable_ids
    )

    ranking_reason_combinations = Counter(
        ";".join(_ranking_reasons(row)) or "ranked"
        for row in db_rows
    )

    bathroom_sample = _sample(bathroom_population, 60, "mvp-bathroom-v1")
    furnishing_sample = _sample(furnishing_population, 40, "mvp-furnishing-v1")
    demo = _demo_shortlist(canonical, db_by_listing, risky_listing_ids)
    high_risk_by_property: dict[str, dict[str, Any]] = {}
    for row in geocode_rows:
        if row["city_evidence"] not in {"SIGNIFICANT_CITY_DISAGREEMENT", "POSSIBLE_GEOCODER_ERROR"}:
            continue
        high_risk_by_property[str(row["property_id"])] = row
    for property_id, city in city_significant_by_property.items():
        distance = _float(city.get("distance_meters"))
        if distance is None or distance <= 200 or property_id in high_risk_by_property:
            continue
        listing_ids = listing_ids_by_property.get(property_id, [])
        representative = canonical_by_id.get(listing_ids[0], {}) if listing_ids else {}
        high_risk_by_property[property_id] = {
            "issue_type": "geocode",
            "listing_id": listing_ids[0] if listing_ids else None,
            "listing_ids": listing_ids,
            "property_id": property_id,
            "address": representative.get("address"),
            "geocode_status": representative.get("geocode_status"),
            "geocode_result_type": representative.get("geocode_result_type"),
            "geocode_formatted": representative.get("geocode_formatted"),
            "city_evidence": "SIGNIFICANT_CITY_DISAGREEMENT",
            "city_address_match_method": city.get("address_match_method"),
            "city_building_match_method": city.get("building_match_method"),
            "city_parcel_match_method": city.get("parcel_match_method"),
            "distance_meters": distance,
            "disposition": "GEOCODE_CONFIDENCE_REVIEW",
            "severity": "P1",
            "reason": "map-ready coordinate exceeds the observed City shadow-evidence p95",
        }
    high_risk_candidates = list(high_risk_by_property.values()) + missing_address
    risk_order = {
        "PIPELINE_DEFECT": 4,
        "SOURCE_ADDRESS_INCOMPLETE": 3,
        "POSSIBLE_GEOCODER_ERROR": 2,
        "SIGNIFICANT_CITY_DISAGREEMENT": 1,
    }
    high_risk = sorted(
        high_risk_candidates,
        key=lambda row: (
            -risk_order.get(str(row.get("city_evidence") or row.get("evidence_classification")), 0),
            -(_float(row.get("distance_meters")) or 0),
            str(row.get("property_id")),
        ),
    )[:20]

    disposition_counts = Counter(str(row.get("disposition")) for row in review_items)
    severity_counts = Counter(str(row.get("severity")) for row in review_items)
    price_unresolved = [row for row in canonical if _clean(row.get("price_period")) is None]
    sublet_true = [row for row in canonical if _bool(row.get("is_sublet")) is True]
    sublet_false = [row for row in canonical if _bool(row.get("is_sublet")) is False]
    sublet_unknown = [row for row in canonical if _bool(row.get("is_sublet")) is None]
    summer_non_sublet = [
        row for row in canonical
        if _clean(row.get("availability_category")) == "summer_available"
        and _bool(row.get("is_sublet")) is not True
    ]

    core = {
        "housing_type": sum(_clean(row.get("housing_type")) is not None for row in canonical),
        "gender_preference_or_restriction": sum(
            _clean(row.get("preferred_gender")) not in {None, "not_specified"} for row in canonical
        ),
        "lease_type": sum(_clean(row.get("lease_type")) not in {None, "unknown"} for row in canonical) - len(lease_defects),
        "availability": sum(
            any(_clean(row.get(field)) for field in ("date_available_raw", "availability_category", "available_from"))
            for row in canonical
        ),
        "sublet_evidence": len(sublet_true) + len(sublet_false),
        "furnishing": sum(_bool(row.get("furnished")) is not None for row in canonical),
        "utilities": sum(_clean(row.get("utilities_status")) not in {None, "unknown"} for row in canonical),
        "bathrooms": sum(_float(row.get("bathrooms")) is not None for row in canonical),
    }
    price_summary = Counter(str(row["evidence_classification"]) for row in price_rows)
    geocode_summary = Counter(str(row["city_evidence"]) for row in geocode_rows)
    legitimate_unknowns = {
        "source_address_incomplete": sum(row["evidence_classification"] == "SOURCE_ADDRESS_INCOMPLETE" for row in missing_address),
        "price_period": price_summary["PERIOD_TRULY_UNSPECIFIED"],
        "bathrooms": (
            sum(_float(row.get("bathrooms")) is None for row in canonical)
            - sum(row["disposition"] == "RESOLVED_DETERMINISTIC" for row in bathroom_population)
        ),
        "furnished": sum(_clean(row.get("furnished")) is None for row in canonical),
        "sublet": len(sublet_unknown),
        "gender": sum(_clean(row.get("preferred_gender")) is None for row in canonical),
    }

    summary = {
        "run_id": run_dir.name,
        "canonical_sha256": canonical_fingerprint,
        "canonical_listing_count": len(canonical),
        "database_active_listing_count": db_listing_count,
        "database_property_count": len({row.get("property_id") for row in db_rows}),
        "review_counts": {
            "geocode": len(geocode_rows),
            "missing_address": len(missing_address),
            "unresolved_price_period": len(price_unresolved),
            "bathroom_flags": len(bathroom_population),
            "unknown_furnished": sum(_clean(row.get("furnished")) is None for row in canonical),
            "unknown_sublet": len(sublet_unknown),
        },
        "disposition_counts": dict(sorted(disposition_counts.items())),
        "severity_counts": dict(sorted(severity_counts.items())),
        "missing_address": missing_address,
        "geocode_city_evidence_counts": dict(sorted(geocode_summary.items())),
        "city_significant_property_count_all_locations": len(city_significant_by_property),
        "city_significant_outside_geocode_review_count": shadow_significant_outside_review,
        "geocode_review_distance_bands": {
            band: distance_bands[band]
            for band in ("0-25 m", "25-50 m", "50-100 m", "100-200 m", "200-400 m", ">400 m")
        },
        "city_overall_distance_distribution": city_summary["distance_meters"],
        "map_assessment": {
            "listings": dict(sorted(map_counts.items())),
            "properties": dict(sorted(property_map_counts.items())),
        },
        "public_location_visibility": {
            "policy_version": LOCATION_VISIBILITY_POLICY_VERSION,
            "properties": dict(sorted(public_location_counts.items())),
        },
        "high_risk_map_shortlist": high_risk,
        "price": {
            "current_reliable_monthly": sum(_float(row.get("price_monthly")) is not None for row in canonical) - price_summary["PIPELINE_DEFECT"],
            "raw_price_unknown_period": len(price_unresolved),
            "lack_useful_price": sum(_float(row.get("price_numeric")) is None for row in canonical),
            "dispositions": dict(sorted(price_summary.items())),
        },
        "ranking": {
            "missing_monthly_price": ranking_missing,
            "otherwise_eligible_blocked_only_by_price": price_only,
            "recoverable_price_rows_blocked_only_by_price": price_only_recoverable,
            "eligibility_reason_combinations": dict(sorted(ranking_reason_combinations.items())),
        },
        "bathrooms": {
            "review_population": len(bathroom_population),
            "deterministically_recoverable": sum(row["disposition"] == "RESOLVED_DETERMINISTIC" for row in bathroom_population),
            "sample_size": len(bathroom_sample),
        },
        "furnishing": {
            "review_population": len(furnishing_population),
            "sample_size": len(furnishing_sample),
            "source_conflicts": sum(row["evidence_classification"] == "SOURCE_CONFLICT" for row in furnishing_population),
            "false_positive_review_flags": sum(row["evidence_classification"] == "PIPELINE_DEFECT" for row in furnishing_population),
        },
        "sublet": {
            "explicit_true": len(sublet_true),
            "explicit_false": len(sublet_false),
            "unknown": len(sublet_unknown),
            "summer_non_sublet": len(summer_non_sublet),
        },
        "regressions": {
            "gender_review_rows": len(gender_review),
            "gender_contradictions": len(gender_contradictions),
            "utilities_contradictions": len(utilities_conflicts),
            "lease_review_rows": len(lease_review),
            "lease_pipeline_defects": len(lease_defects),
        },
        "core_reliable": core,
        "legitimate_unknowns": legitimate_unknowns,
        "confirmed_pipeline_defects": {
            "missing_address": sum(row["severity"] == "P0" for row in missing_address),
            "unsupported_price_period": price_summary["PIPELINE_DEFECT"],
            "recoverable_price_period": price_summary["PERIOD_RECOVERABLE_EXPLICIT"],
            "bathroom_parser_gaps": sum(row["disposition"] == "RESOLVED_DETERMINISTIC" for row in bathroom_population),
            "lease_semantics": len(lease_defects),
        },
        "demo_shortlist": demo,
        "contract": {
            "dispositions": sorted(DISPOSITIONS),
            "severities": sorted(SEVERITIES),
            "city_evidence": sorted(GEOCODE_EVIDENCE),
            "city_distance_policy": "0-50m supported; 50m-p95 minor offset; above current p95 significant shadow disagreement",
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "review-items.csv", review_items)
    _write_csv(output_dir / "geocode-review.csv", geocode_rows)
    _write_csv(output_dir / "map-shortlist.csv", high_risk)
    _write_csv(output_dir / "price-review.csv", price_rows)
    _write_csv(output_dir / "bathroom-sample.csv", bathroom_sample)
    _write_csv(output_dir / "furnishing-sample.csv", furnishing_sample)
    _write_csv(output_dir / "demo-shortlist.csv", demo)
    _write_csv(output_dir / "location-visibility.csv", location_visibility)
    (output_dir / "summary.json").write_text(
        json.dumps(_json_value(summary), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--city-audit", type=Path,
        default=Path("data/london-reference-validation/city-address-match-audit.csv"),
    )
    parser.add_argument(
        "--city-summary", type=Path,
        default=Path("data/london-reference-validation/reference-data-summary.json"),
    )
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--output-dir", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    database_url = os.getenv(args.database_url_env)
    if not database_url:
        raise SystemExit(f"{args.database_url_env} must contain the local read-only triage database URL")
    output_dir = args.output_dir or Path("data/mvp-review-triage") / args.run_dir.name
    summary = triage(
        run_dir=args.run_dir,
        city_audit_path=args.city_audit,
        city_summary_path=args.city_summary,
        database_url=database_url,
        output_dir=output_dir,
    )
    print(json.dumps(_json_value({
        "run_id": summary["run_id"],
        "canonical_sha256": summary["canonical_sha256"],
        "review_counts": summary["review_counts"],
        "severity_counts": summary["severity_counts"],
        "output_dir": str(output_dir),
    }), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
