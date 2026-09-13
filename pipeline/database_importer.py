"""Stage 4: validated, history-preserving PostgreSQL persistence.

The module deliberately separates pure run/CSV planning from the PostgreSQL
adapter. ``--dry-run`` uses only the pure layer and never opens a database
connection. Connected imports lazily load ``pipeline.postgres_repository``.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from pipeline.run_approval import approval_validation_errors

try:
    from pipeline.run_context import (
        COMPLETED,
        COMPLETED_WITH_WARNINGS,
        RunContext,
        determine_run_completion,
    )
except ModuleNotFoundError:  # Direct execution as pipeline/database_importer.py.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline.run_context import (
        COMPLETED,
        COMPLETED_WITH_WARNINGS,
        RunContext,
        determine_run_completion,
    )


SOURCE = "uwo_offcampus"
GEOCODE_PROVIDER = "geoapify"
DEFAULT_MISSING_RUN_THRESHOLD = 2
VALID_LISTING_STATUSES = {"active", "possibly_removed", "removed", "relisted"}
VALID_RUN_STATUSES = {COMPLETED, COMPLETED_WITH_WARNINGS}
LISTING_ID_RE = re.compile(r"^[1-9]\d*$")
WESTERN_PATH_RE = re.compile(r"^/Listings/Details/([1-9]\d*)/?$", re.IGNORECASE)
SECRET_QUERY_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret)=([^&\s]+)"
)
DATABASE_URL_RE = re.compile(
    r"(?i)\b(postgresql(?:\+\w+)?://)([^\s/@:]+):([^\s/@]+)@"
)

MONTH_PERIODS = {"month", "monthly", "per month"}
WEEK_PERIODS = {"week", "weekly", "per week"}
DAY_PERIODS = {"day", "daily", "per day"}
RULE_UNSET_SENTINELS = {"", "not_specified", "unknown"}

MEANINGFUL_FIELDS = (
    "title",
    "description",
    "address",
    "normalized_address",
    "unit_identifier",
    "price_text",
    "price_numeric",
    "price_period",
    "price_monthly",
    "bedrooms",
    "housing_type",
    "availability_text",
    "available_now",
    "date_available",
    "available_from",
    "available_to",
    "availability_category",
    "lease_type",
    "lease_term_months",
    "is_sublet",
    "utilities_included",
    "utilities_status",
    "furnished",
    "parking_available",
    "parking_spaces",
    "laundry",
    "air_conditioning",
    "dishwasher",
    "bathrooms",
    "bathroom_type",
    "amenities",
    "tenant_type",
    "preferred_gender",
    "map_ready",
    "latitude",
    "longitude",
    "distance_to_western_km",
)

SUMMARY_FIELDS = (
    "new_listings",
    "updated_listings",
    "unchanged_listings",
    "relisted_listings",
    "possibly_removed_listings",
    "removed_listings",
    "observations_inserted",
    "properties_created",
    "properties_reused",
    "possible_property_duplicates",
    "review_items_created",
    "rows_rejected",
    "rows_with_warnings",
)


class ImportValidationError(ValueError):
    """Fatal manifest, identity, or schema error that prevents an import."""


@dataclass(frozen=True)
class RowIssue:
    review_type: str
    severity: str
    field_name: Optional[str]
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PropertyCandidate:
    normalized_address: Optional[str]
    display_address: Optional[str]
    unit_identifier: Optional[str]
    city: Optional[str]
    province: Optional[str]
    postal_code: Optional[str]
    country_code: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    geocode_confidence: Optional[float]
    geocode_status: Optional[str]
    address_complete: bool
    match_key: Optional[str]
    unit_ambiguous: bool = False


@dataclass(frozen=True)
class GeocodeCandidate:
    normalized_query: str
    status: str
    latitude: Optional[float]
    longitude: Optional[float]
    confidence: Optional[float]
    match_type: Optional[str]
    result_type: Optional[str]
    formatted_address: Optional[str]
    city: Optional[str]
    postal_code: Optional[str]
    country_code: Optional[str]
    error: Optional[str]


@dataclass(frozen=True)
class CanonicalListing:
    source_listing_id: str
    source_url: str
    raw_data: dict[str, Any]
    values: dict[str, Any]
    comparison_data: dict[str, Any]
    observation_hash: str
    provenance_data: dict[str, Any]
    confidence_data: dict[str, Any]
    review_flags: list[str]
    issues: tuple[RowIssue, ...]
    property: PropertyCandidate
    geocode: Optional[GeocodeCandidate]


@dataclass(frozen=True)
class ValidatedRun:
    run_dir: Path
    run_id: str
    source: str
    manifest: dict[str, Any]
    listings: tuple[CanonicalListing, ...]
    discovered_ids: frozenset[str]
    discovered_urls: dict[str, str]
    canonical_sha256: str
    manifest_sha256: str
    override_used: bool
    override_reason: Optional[str]
    observed_at: str
    lifecycle_eligible: bool
    lifecycle_ineligibility_reasons: tuple[str, ...]


@dataclass(frozen=True)
class StoredProperty:
    id: int
    normalized_address: Optional[str]
    unit_identifier: Optional[str]
    match_key: Optional[str]
    latitude: Optional[float] = None
    longitude: Optional[float] = None


@dataclass(frozen=True)
class StoredListing:
    id: int
    source_listing_id: str
    property_id: Optional[int]
    source_url: str
    status: str
    missing_run_count: int
    latest_observation_hash: Optional[str] = None
    latest_comparison_data: Optional[dict[str, Any]] = None
    observation_run_ids: frozenset[str] = frozenset()


@dataclass
class ExistingState:
    properties_by_id: dict[int, StoredProperty] = field(default_factory=dict)
    properties_by_match_key: dict[str, StoredProperty] = field(default_factory=dict)
    listings_by_source_id: dict[str, StoredListing] = field(default_factory=dict)
    review_keys: set[str] = field(default_factory=set)
    latest_lifecycle_run_at: Optional[str] = None
    latest_imported_run_at: Optional[str] = None
    imported_run_summary: Optional[dict[str, Any]] = None
    imported_run_lifecycle_applied: bool = False
    imported_run_lifecycle_reasons: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> "ExistingState":
        return cls()


@dataclass(frozen=True)
class PropertyDecision:
    ref: str
    candidate: PropertyCandidate
    existing_id: Optional[int]
    reused: bool
    enrich_identity: bool = False


@dataclass(frozen=True)
class ListingDecision:
    source_listing_id: str
    source_url: str
    existing_id: Optional[int]
    property_ref: Optional[str]
    classification: Optional[str]
    new_status: str
    missing_run_count: int
    seen_in_canonical: bool
    refresh_current_state: bool


@dataclass(frozen=True)
class ObservationDecision:
    source_listing_id: str
    classification: str
    changed_fields: tuple[str, ...]
    listing: CanonicalListing


@dataclass(frozen=True)
class ReviewDecision:
    source_listing_id: Optional[str]
    property_ref: Optional[str]
    review_type: str
    severity: str
    field_name: Optional[str]
    reason: str
    payload: dict[str, Any]
    dedupe_key: str


@dataclass(frozen=True)
class ImportPlan:
    run: ValidatedRun
    properties: tuple[PropertyDecision, ...]
    listings: tuple[ListingDecision, ...]
    observations: tuple[ObservationDecision, ...]
    geocodes: tuple[GeocodeCandidate, ...]
    reviews: tuple[ReviewDecision, ...]
    summary: dict[str, int]
    lifecycle_applied: bool
    lifecycle_ineligibility_reasons: tuple[str, ...] = ()
    already_imported: bool = False


def sanitize_error(value: Any) -> str:
    """Remove common credential forms from persisted or displayed errors."""
    text = str(value)
    text = SECRET_QUERY_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return DATABASE_URL_RE.sub(r"\1[REDACTED]@", text)


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.casefold() in {"nan", "none", "null", "nat"}:
        return None
    return text


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_source_url(value: Any) -> tuple[str, str]:
    text = _clean_text(value)
    if not text:
        raise ImportValidationError("listing_url is required")
    parts = urlsplit(text)
    host = (parts.hostname or "").casefold()
    if host not in {"offcampus.uwo.ca", "www.offcampus.uwo.ca"}:
        raise ImportValidationError(f"Unsupported Western listing host: {host or 'missing'}")
    match = WESTERN_PATH_RE.fullmatch(parts.path)
    if not match:
        raise ImportValidationError(f"Malformed Western listing URL path: {parts.path}")
    source_id = match.group(1)
    return source_id, f"https://offcampus.uwo.ca/Listings/Details/{source_id}"


def normalize_source_listing_id(value: Any) -> str:
    text = _clean_text(value)
    if not text or not LISTING_ID_RE.fullmatch(text):
        raise ImportValidationError(f"Invalid Western source listing ID: {text!r}")
    return text


def parse_bool(
    value: Any, field_name: str, issues: list[RowIssue]
) -> Optional[bool]:
    text = _clean_text(value)
    if text is None:
        return None
    normalized = text.casefold()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    issues.append(
        RowIssue(
            "import_validation",
            "warning",
            field_name,
            "invalid_boolean",
            {"raw_value": text},
        )
    )
    return None


def parse_float(
    value: Any, field_name: str, issues: list[RowIssue]
) -> Optional[float]:
    text = _clean_text(value)
    if text is None:
        return None
    try:
        parsed = float(text.replace(",", ""))
    except ValueError:
        parsed = math.nan
    if not math.isfinite(parsed):
        issues.append(
            RowIssue(
                "import_validation",
                "warning",
                field_name,
                "invalid_number",
                {"raw_value": text},
            )
        )
        return None
    return parsed


def parse_int(value: Any, field_name: str, issues: list[RowIssue]) -> Optional[int]:
    parsed = parse_float(value, field_name, issues)
    if parsed is None:
        return None
    if not parsed.is_integer():
        issues.append(
            RowIssue(
                "import_validation",
                "warning",
                field_name,
                "non_integral_number",
                {"raw_value": _clean_text(value)},
            )
        )
        return None
    return int(parsed)


def normalize_price_period(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if text is None:
        return None
    normalized = re.sub(r"[_\s]+", " ", text.casefold()).strip()
    if normalized == "month per bedroom":
        return "month_per_bedroom"
    if normalized in MONTH_PERIODS:
        return "month"
    if normalized in WEEK_PERIODS:
        return "week"
    if normalized in DAY_PERIODS:
        return "day"
    return normalized


def calculate_monthly_price(
    price_numeric: Optional[float], price_period: Optional[str]
) -> Optional[float]:
    if price_numeric is None or price_period is None:
        return None
    if price_period in {"month", "month_per_bedroom"}:
        return round(price_numeric, 2)
    if price_period == "week":
        return round(price_numeric * 52 / 12, 2)
    if price_period == "day":
        return round(price_numeric * 365 / 12, 2)
    return None


STREET_SUFFIXES = {
    "street": "st",
    "avenue": "ave",
    "road": "rd",
    "boulevard": "blvd",
    "drive": "dr",
    "lane": "ln",
    "court": "ct",
    "place": "pl",
    "terrace": "terr",
}
UNIT_RE = re.compile(
    r"(?:\s|,|-)+(unit|apt|apartment|suite|room|rm|#)\s*([a-z0-9-]+)\b",
    re.IGNORECASE,
)
LEADING_UNIT_RE = re.compile(
    r"^\s*(unit|apt|apartment|suite|room|rm|#)\s*([a-z0-9-]+)(?:\s|,|-)+",
    re.IGNORECASE,
)
CANADIAN_UNIT_STREET_RE = re.compile(
    r"^\s*([a-z0-9]+)-(\d+[a-z]?)\s+(?=[a-z])",
    re.IGNORECASE,
)
AMBIGUOUS_UNIT_RE = re.compile(r"\b(?:upper|lower|basement|rear)\b", re.IGNORECASE)


def normalize_property_address(
    address: Any,
    *,
    geocode_city: Any = None,
    geocode_country_code: Any = None,
    geocode_status: Any = None,
    geocode_confidence: Optional[float] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> PropertyCandidate:
    display = _clean_text(address)
    city = (_clean_text(geocode_city) or "").casefold() or None
    country = (_clean_text(geocode_country_code) or "").casefold() or None
    status = (_clean_text(geocode_status) or "").casefold() or None
    if not display:
        return PropertyCandidate(
            None, None, None, city, None, None, country, None, None, None,
            status, False, None, False
        )

    normalized = unicodedata.normalize("NFKC", display).casefold()
    leading_unit = LEADING_UNIT_RE.match(normalized)
    canadian_unit = CANADIAN_UNIT_STREET_RE.match(normalized)
    if leading_unit:
        normalized = LEADING_UNIT_RE.sub("", normalized, count=1)
    elif canadian_unit:
        normalized = CANADIAN_UNIT_STREET_RE.sub(
            f"{canadian_unit.group(2)} ", normalized, count=1
        )
    unit_matches = list(UNIT_RE.finditer(normalized))
    unit_identifier: Optional[str] = None
    unit_ambiguous = False
    if leading_unit or canadian_unit:
        match_kind = leading_unit.group(1) if leading_unit else "unit"
        match_value = leading_unit.group(2) if leading_unit else canadian_unit.group(1)
        kind = match_kind.casefold()
        if kind in {"apartment", "apt"}:
            kind = "apt"
        elif kind in {"room", "rm"}:
            kind = "room"
        elif kind == "#":
            kind = "unit"
        unit_identifier = f"{kind}:{match_value.casefold()}"
        if unit_matches:
            unit_ambiguous = True
    elif unit_matches:
        explicit_units = {f"{m.group(1)} {m.group(2)}" for m in unit_matches}
        unit_ambiguous = len(explicit_units) > 1
        if not unit_ambiguous:
            match = unit_matches[-1]
            kind = match.group(1)
            if kind in {"apartment", "apt"}:
                kind = "apt"
            elif kind in {"room", "rm"}:
                kind = "room"
            elif kind == "#":
                kind = "unit"
            unit_identifier = f"{kind}:{match.group(2)}"
            normalized = UNIT_RE.sub(" ", normalized)
    elif AMBIGUOUS_UNIT_RE.search(normalized):
        unit_ambiguous = True

    raw_has_london = bool(re.search(r"\blondon\b", normalized))
    raw_has_ontario = bool(re.search(r"\b(?:on|ontario)\b", normalized))
    raw_has_canada = bool(re.search(r"\bcanada\b", normalized))
    normalized = re.sub(
        r"(?:\s|,)+(?:london)(?:\s|,)+(?:on|ontario)(?:\s|,)+(?:canada)\s*$",
        " ",
        normalized,
    )
    normalized = re.sub(r"[.,;:()\[\]]", " ", normalized)
    normalized = re.sub(r"[-_/]+", " ", normalized)
    tokens = [STREET_SUFFIXES.get(token, token) for token in normalized.split()]
    normalized_address = " ".join(tokens) or None

    if city is None and raw_has_london:
        city = "london"
    province = "on" if raw_has_ontario or city == "london" else None
    if country is None and raw_has_canada:
        country = "ca"
    reliable_geocode_locality = bool(
        status == "ok"
        and city == "london"
        and country == "ca"
        and latitude is not None
        and longitude is not None
        and geocode_confidence is not None
        and geocode_confidence >= 0.8
    )
    locality_reliable = reliable_geocode_locality or (
        raw_has_london and raw_has_ontario and raw_has_canada
    )
    street_complete = bool(
        normalized_address
        and re.search(r"\b\d+[a-z]?\b", normalized_address)
        and len(normalized_address.split()) >= 2
    )
    address_complete = bool(street_complete and locality_reliable and not unit_ambiguous)
    match_key = None
    if address_complete:
        match_key = "|".join(
            (
                normalized_address,
                unit_identifier or "no-unit",
                city or "",
                province or "",
                country or "",
            )
        )

    return PropertyCandidate(
        normalized_address=normalized_address,
        display_address=display,
        unit_identifier=unit_identifier,
        city=city,
        province=province,
        postal_code=None,
        country_code=country,
        latitude=None,
        longitude=None,
        geocode_confidence=None,
        geocode_status=status,
        address_complete=address_complete,
        match_key=match_key,
        unit_ambiguous=unit_ambiguous,
    )


def _parse_json_list(
    value: Any,
    *,
    field_name: Optional[str] = None,
    issues: Optional[list[RowIssue]] = None,
) -> list[str]:
    text = _clean_text(value)
    if text is None:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        parsed = None
        if text.lstrip().startswith(("[", "{")):
            if issues is not None:
                issues.append(
                    RowIssue(
                        "import_validation",
                        "warning",
                        field_name,
                        "invalid_json_array",
                        {"raw_value": text},
                    )
                )
            return []
    if isinstance(parsed, list):
        values = parsed
    elif parsed is not None:
        if issues is not None:
            issues.append(
                RowIssue(
                    "import_validation",
                    "warning",
                    field_name,
                    "expected_json_array",
                    {"raw_value": text},
                )
            )
        return []
    else:
        separator = "|" if "|" in text else ","
        values = text.split(separator)
    normalized = {item.strip() for item in map(str, values) if item.strip()}
    return sorted(normalized, key=str.casefold)


def _parse_review_flags(row: dict[str, Any]) -> list[str]:
    flags = _parse_json_list(row.get("review_flags"))
    for key, value in row.items():
        if key.startswith("consensus_disagreement_") or key.endswith(
            "_ai_evidence_blocked"
        ):
            local_issues: list[RowIssue] = []
            if parse_bool(value, key, local_issues) is True:
                flags.append(key)
    return sorted(set(flags))


def _raw_data(row: dict[str, Any]) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    for key, value in row.items():
        text = _clean_text(value)
        normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
        if any(
            marker in normalized_key
            for marker in (
                "apikey",
                "password",
                "secret",
                "token",
                "databaseurl",
                "servicerole",
            )
        ):
            raw[key] = "[REDACTED]" if text else None
        elif key in {"geocode_error", "ai_error", "scrape_error", "otp_error"}:
            raw[key] = sanitize_error(text) if text else None
        else:
            raw[key] = None if value is None else str(value)
    return raw


def _date_text(value: Any, field_name: str, issues: list[RowIssue]) -> Optional[str]:
    text = _clean_text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        issues.append(
            RowIssue(
                "availability", "warning", field_name, "invalid_date", {"raw_value": text}
            )
        )
        return None


def _required_aware_datetime(value: Any, field_name: str) -> str:
    text = _clean_text(value)
    if text is None:
        raise ImportValidationError(f"Manifest lacks {field_name}")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ImportValidationError(f"Manifest has invalid {field_name}: {text!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ImportValidationError(f"Manifest {field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_or_rule(
    row: dict[str, Any], canonical_name: str, rule_name: str
) -> Any:
    canonical = row.get(canonical_name)
    source = (_clean_text(row.get(f"{canonical_name}_source")) or "").casefold()
    if source.startswith("manual") and _clean_text(canonical) is not None:
        return canonical
    rule = row.get(rule_name)
    cleaned_rule = _clean_text(rule)
    if (
        cleaned_rule is not None
        and cleaned_rule.casefold() not in RULE_UNSET_SENTINELS
    ):
        return rule
    return canonical


def _comparison_text(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if text is None:
        return None
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def prepare_canonical_listing(row: dict[str, Any]) -> CanonicalListing:
    issues: list[RowIssue] = []
    source_id = normalize_source_listing_id(row.get("listing_id"))
    url_id, source_url = canonical_source_url(
        row.get("listing_url") or row.get("source_url")
    )
    if source_id != url_id:
        raise ImportValidationError(
            f"listing_id {source_id} does not match listing URL ID {url_id}"
        )

    latitude = parse_float(row.get("latitude"), "latitude", issues)
    longitude = parse_float(row.get("longitude"), "longitude", issues)
    if latitude is not None and not -90 <= latitude <= 90:
        issues.append(RowIssue("geocode", "error", "latitude", "latitude_out_of_range"))
        latitude = None
    if longitude is not None and not -180 <= longitude <= 180:
        issues.append(RowIssue("geocode", "error", "longitude", "longitude_out_of_range"))
        longitude = None
    if (latitude is None) != (longitude is None):
        issues.append(RowIssue("geocode", "error", None, "incomplete_coordinate_pair"))
        latitude = longitude = None

    confidence = parse_float(
        row.get("geocode_confidence"), "geocode_confidence", issues
    )
    if confidence is not None and not 0 <= confidence <= 1:
        issues.append(
            RowIssue("geocode", "warning", "geocode_confidence", "confidence_out_of_range")
        )
        confidence = None

    geocode_status = (_clean_text(row.get("geocode_status")) or "").casefold() or None
    if geocode_status == "ok" and (latitude is None or longitude is None):
        issues.append(
            RowIssue("geocode", "error", "geocode_status", "ok_geocode_missing_coordinates")
        )
        geocode_status = "invalid"

    base_property = normalize_property_address(
        row.get("address"),
        geocode_city=row.get("geocode_city"),
        geocode_country_code=row.get("geocode_country_code"),
        geocode_status=geocode_status,
        geocode_confidence=confidence,
        latitude=latitude,
        longitude=longitude,
    )
    property_candidate = PropertyCandidate(
        normalized_address=base_property.normalized_address,
        display_address=base_property.display_address,
        unit_identifier=base_property.unit_identifier,
        city=base_property.city,
        province=base_property.province,
        postal_code=_clean_text(row.get("geocode_postcode")),
        country_code=base_property.country_code,
        latitude=latitude,
        longitude=longitude,
        geocode_confidence=confidence,
        geocode_status=base_property.geocode_status,
        address_complete=base_property.address_complete,
        match_key=base_property.match_key,
        unit_ambiguous=base_property.unit_ambiguous,
    )
    if property_candidate.unit_ambiguous:
        issues.append(
            RowIssue(
                "property_match", "warning", "address", "ambiguous_unit_identifier"
            )
        )
    if property_candidate.normalized_address is None:
        issues.append(RowIssue("property_match", "warning", "address", "missing_address"))

    price_numeric = parse_float(row.get("price_numeric"), "price_numeric", issues)
    price_period = normalize_price_period(row.get("price_period"))
    supplied_monthly = parse_float(row.get("price_monthly"), "price_monthly", issues)
    if price_numeric is not None and price_numeric < 0:
        issues.append(
            RowIssue(
                "price",
                "error",
                "price_numeric",
                "negative_price_not_allowed",
                {"raw_value": price_numeric},
            )
        )
        price_numeric = None
    if supplied_monthly is not None and supplied_monthly < 0:
        issues.append(
            RowIssue(
                "price",
                "error",
                "price_monthly",
                "negative_price_not_allowed",
                {"raw_value": supplied_monthly},
            )
        )
        supplied_monthly = None
    calculated_monthly = calculate_monthly_price(price_numeric, price_period)
    price_monthly = supplied_monthly
    if supplied_monthly is None and calculated_monthly is not None:
        price_monthly = calculated_monthly
    elif supplied_monthly is not None and calculated_monthly is not None:
        if abs(supplied_monthly - calculated_monthly) > 0.01:
            issues.append(
                RowIssue(
                    "price",
                    "warning",
                    "price_monthly",
                    "monthly_price_conflicts_with_deterministic_conversion",
                    {"supplied": supplied_monthly, "calculated": calculated_monthly},
                )
            )
            price_monthly = calculated_monthly
    if price_numeric is not None and price_period is None:
        issues.append(RowIssue("price", "warning", "price_period", "missing_price_period"))
    if price_numeric is not None and price_monthly is None:
        issues.append(
            RowIssue("price", "warning", "price_monthly", "monthly_price_unresolved")
        )
    if supplied_monthly is not None and price_period is None:
        issues.append(
            RowIssue(
                "price",
                "warning",
                "price_period",
                "normalized_price_has_unknown_original_period",
            )
        )
    if price_monthly is not None and (price_monthly < 100 or price_monthly > 10000):
        issues.append(
            RowIssue(
                "price",
                "warning",
                "price_monthly",
                "suspicious_monthly_price",
                {"price_monthly": price_monthly},
            )
        )

    map_ready = parse_bool(row.get("map_ready"), "map_ready", issues)
    if latitude is None or longitude is None:
        map_ready = False
        issues.append(RowIssue("geocode", "warning", None, "missing_coordinates"))
    if map_ready is True and geocode_status != "ok":
        issues.append(
            RowIssue(
                "geocode",
                "error",
                "map_ready",
                "map_ready_inconsistent_with_geocode_status",
            )
        )
        map_ready = False
    if geocode_status != "ok":
        issues.append(
            RowIssue(
                "geocode", "warning", "geocode_status", f"geocode_status_{geocode_status or 'missing'}"
            )
        )
    geocode_city = (_clean_text(row.get("geocode_city")) or "").casefold()
    geocode_country = (
        _clean_text(row.get("geocode_country_code")) or ""
    ).casefold()
    if geocode_city and geocode_city != "london":
        issues.append(RowIssue("geocode", "warning", "geocode_city", "city_mismatch"))
    if geocode_country != "ca":
        issues.append(
            RowIssue("geocode", "warning", "geocode_country_code", "country_mismatch")
        )
    if confidence is not None and confidence < 0.8:
        issues.append(
            RowIssue("geocode", "warning", "geocode_confidence", "low_confidence")
        )
    geocode_quality_text = _clean_text(row.get("geocode_quality_issue"))
    for issue_name in (
        [item.strip() for item in geocode_quality_text.split(";") if item.strip()]
        if geocode_quality_text
        else []
    ):
        issues.append(RowIssue("geocode", "warning", None, issue_name))

    review_flags = _parse_review_flags(row)
    needs_review_issues: list[RowIssue] = []
    needs_manual_review = parse_bool(
        row.get("needs_manual_review"), "needs_manual_review", needs_review_issues
    )
    issues.extend(needs_review_issues)
    if needs_manual_review:
        issues.append(RowIssue("ai", "warning", None, "needs_manual_review"))
    for flag in review_flags:
        issues.append(RowIssue("ai", "warning", None, flag))

    values: dict[str, Any] = {
        "title": _clean_text(row.get("title")),
        "description": _clean_text(row.get("description")),
        "address": _clean_text(row.get("address")),
        "price_text": _clean_text(row.get("price_text")),
        "price_numeric": price_numeric,
        "price_period": price_period,
        "price_monthly": price_monthly,
        "bedrooms": parse_int(row.get("bedrooms"), "bedrooms", issues),
        "housing_type": _clean_text(row.get("housing_type")),
        "utilities_included": parse_bool(
            _canonical_or_rule(
                row, "utilities_included", "utilities_included_rule"
            ),
            "utilities_included",
            issues,
        ),
        "utilities_status": _clean_text(row.get("utilities_status")),
        "lease_type": _clean_text(
            _canonical_or_rule(row, "lease_type", "lease_type_rule")
        ),
        "lease_term_months": parse_int(
            _canonical_or_rule(
                row, "lease_term_months", "lease_term_months_rule"
            ),
            "lease_term_months",
            issues,
        ),
        "is_sublet": parse_bool(row.get("is_sublet"), "is_sublet", issues),
        "furnished": parse_bool(
            _canonical_or_rule(row, "furnished", "furnished_rule"),
            "furnished",
            issues,
        ),
        "parking_available": parse_bool(
            _canonical_or_rule(
                row, "parking_available", "parking_available_rule"
            ),
            "parking_available",
            issues,
        ),
        "parking_spaces": parse_int(
            _canonical_or_rule(row, "parking_spaces", "parking_spaces_rule"),
            "parking_spaces",
            issues,
        ),
        "laundry": parse_bool(
            _canonical_or_rule(row, "laundry", "laundry_rule"),
            "laundry",
            issues,
        ),
        "air_conditioning": parse_bool(
            _canonical_or_rule(
                row, "air_conditioning", "air_conditioning_rule"
            ),
            "air_conditioning",
            issues,
        ),
        "dishwasher": parse_bool(
            _canonical_or_rule(row, "dishwasher", "dishwasher_rule"),
            "dishwasher",
            issues,
        ),
        "bathrooms": parse_float(row.get("bathrooms"), "bathrooms", issues),
        "bathroom_type": _clean_text(
            _canonical_or_rule(row, "bathroom_type", "bathroom_type_rule")
        ),
        "available_now": parse_bool(
            row.get("available_now") or row.get("available_now_rule"),
            "available_now",
            issues,
        ),
        "availability_text": _clean_text(row.get("availability_text")),
        "date_available": _date_text(row.get("date_available"), "date_available", issues),
        "tenant_type": _clean_text(
            _canonical_or_rule(row, "tenant_type", "tenant_type_rule")
        ),
        "preferred_gender": _clean_text(
            _canonical_or_rule(
                row, "preferred_gender", "preferred_gender_rule"
            )
        ),
        "amenities": _parse_json_list(
            row.get("amenities_list") or row.get("amenities"),
            field_name="amenities",
            issues=issues,
        ),
        "latitude": latitude,
        "longitude": longitude,
        "map_ready": bool(map_ready),
        "geocode_status": geocode_status,
        "geocode_confidence": confidence,
        "geocode_quality_issue": geocode_quality_text,
        "distance_to_western_km": parse_float(
            row.get("distance_to_western_km"), "distance_to_western_km", issues
        ),
        "available_from": _date_text(row.get("available_from"), "available_from", issues),
        "available_to": _date_text(row.get("available_to"), "available_to", issues),
        "availability_category": _clean_text(row.get("availability_category")),
    }
    values["normalized_address"] = property_candidate.normalized_address
    values["unit_identifier"] = property_candidate.unit_identifier
    for field_name in (
        "bedrooms",
        "lease_term_months",
        "parking_spaces",
        "bathrooms",
        "distance_to_western_km",
    ):
        if values[field_name] is not None and values[field_name] < 0:
            issues.append(
                RowIssue(
                    "import_validation",
                    "warning",
                    field_name,
                    "negative_value_not_allowed",
                    {"raw_value": values[field_name]},
                )
            )
            values[field_name] = None
    if values["lease_term_months"] == 0:
        issues.append(
            RowIssue(
                "import_validation",
                "warning",
                "lease_term_months",
                "zero_lease_term_is_unknown",
                {"raw_value": 0},
            )
        )
        values["lease_term_months"] = None

    raw = _raw_data(row)
    provenance = {
        key: value
        for key, value in raw.items()
        if key.endswith("_source")
        or key.endswith("_rule")
        or key in {"manual_reviewed", "manual_review_note"}
    }
    confidence_data = {
        key: value
        for key, value in raw.items()
        if "confidence" in key or key.startswith("ai_evidence_") or key == "review_score"
    }
    comparison_data = {key: values.get(key) for key in MEANINGFUL_FIELDS}
    comparison_data["address"] = property_candidate.normalized_address
    for field_name in (
        "title",
        "description",
        "price_text",
        "housing_type",
        "availability_text",
        "availability_category",
        "lease_type",
        "utilities_status",
        "bathroom_type",
        "tenant_type",
        "preferred_gender",
    ):
        comparison_data[field_name] = _comparison_text(values.get(field_name))
    observation_hash = sha256_json(comparison_data)

    query = _clean_text(row.get("geocode_query"))
    geocode = None
    if query:
        geocode = GeocodeCandidate(
            normalized_query=" ".join(query.casefold().split()),
            status=geocode_status or "missing",
            latitude=latitude,
            longitude=longitude,
            confidence=confidence,
            match_type=_clean_text(row.get("geocode_match_type")),
            result_type=_clean_text(row.get("geocode_result_type")),
            formatted_address=_clean_text(row.get("geocode_formatted")),
            city=_clean_text(row.get("geocode_city")),
            postal_code=_clean_text(row.get("geocode_postcode")),
            country_code=_clean_text(row.get("geocode_country_code")),
            error=sanitize_error(row.get("geocode_error"))
            if _clean_text(row.get("geocode_error"))
            else None,
        )

    return CanonicalListing(
        source_listing_id=source_id,
        source_url=source_url,
        raw_data=raw,
        values=values,
        comparison_data=comparison_data,
        observation_hash=observation_hash,
        provenance_data=provenance,
        confidence_data=confidence_data,
        review_flags=review_flags,
        issues=tuple(issues),
        property=property_candidate,
        geocode=geocode,
    )


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None:
            raise ImportValidationError(f"CSV has no header: {path}")
        duplicate_headers = {
            name for name in reader.fieldnames if reader.fieldnames.count(name) > 1
        }
        if duplicate_headers:
            raise ImportValidationError(
                f"CSV contains duplicate columns: {sorted(duplicate_headers)}"
            )
        return [dict(row) for row in reader]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _warning_messages(manifest: dict[str, Any], stage_name: str) -> list[str]:
    messages: list[str] = []
    for warning in manifest.get("warnings", []):
        if not isinstance(warning, dict) or warning.get("stage") != stage_name:
            continue
        message = _clean_text(warning.get("message"))
        if message:
            messages.append(message)
    return messages


def determine_lifecycle_eligibility(
    manifest: dict[str, Any], *, stage0_actual_rows: int
) -> tuple[bool, tuple[str, ...]]:
    """Return whether absence transitions are safe for this discovery run."""
    reasons: list[str] = []
    stage0 = manifest.get("stages", {}).get("stage0", {})
    stage1 = manifest.get("stages", {}).get("stage1", {})
    configuration = manifest.get("configuration", {})
    stage0_config = configuration.get("stage0", {}) or {}
    stage1_config = configuration.get("stage1", {}) or {}
    stage0_metrics = stage0.get("metrics", {}) or {}

    if manifest.get("status") not in VALID_RUN_STATUSES:
        reasons.append("pipeline run is not successfully completed")
    if stage0.get("status") not in VALID_RUN_STATUSES:
        reasons.append("Stage 0 discovery is not successfully completed")
    if stage0_actual_rows <= 0:
        reasons.append("Stage 0 discovered no listings")
    if stage1.get("status") not in VALID_RUN_STATUSES:
        reasons.append("Stage 1 is not successfully completed")
    if stage1_config.get("limit") is not None:
        reasons.append("Stage 1 used a row limit")
    max_pages = stage0_config.get("max_pages")
    if isinstance(max_pages, (int, float)) and max_pages <= 1:
        reasons.append("Stage 0 used the smoke-test page limit")
    maximum_page_limit_reached = stage0_metrics.get(
        "maximum_page_limit_reached"
    )
    if not isinstance(maximum_page_limit_reached, bool):
        reasons.append("Stage 0 lacks a trustworthy maximum-page completion metric")
    elif maximum_page_limit_reached:
        reasons.append("Stage 0 reached its maximum page limit")
    if stage0_metrics.get("discovered_listing_count") != stage0_actual_rows:
        reasons.append("Stage 0 discovery metric does not match its validated CSV")

    blocking_warning_fragments = (
        "returned zero",
        "below minimum",
        "fell substantially",
        "maximum page limit",
    )
    warning_text = " ".join(_warning_messages(manifest, "stage0")).casefold()
    for fragment in blocking_warning_fragments:
        if fragment in warning_text:
            reasons.append(f"Stage 0 warning indicates incomplete discovery: {fragment}")

    return not reasons, tuple(dict.fromkeys(reasons))


def load_and_validate_run(
    run_dir: Path,
    *,
    allow_noncanonical_run: bool = False,
    override_reason: Optional[str] = None,
) -> ValidatedRun:
    """Validate one explicitly selected run and parse its canonical CSV."""
    root = run_dir.resolve()
    context = RunContext.resume(root)
    manifest = context.manifest
    run_id = _clean_text(manifest.get("run_id"))
    if not run_id or run_id != root.name:
        raise ImportValidationError(
            "Manifest run_id must match the explicitly selected run directory name"
        )
    if manifest.get("status") not in VALID_RUN_STATUSES:
        raise ImportValidationError(
            f"Run status is not importable: {manifest.get('status')!r}"
        )

    derived_status, completion_issues = determine_run_completion(context)
    if derived_status not in VALID_RUN_STATUSES:
        raise ImportValidationError(
            "Run fails Stage 0-to-3 completion validation: "
            + "; ".join(completion_issues)
        )

    canonical_path = context.paths.stage3_canonical
    if not canonical_path.is_file():
        raise ImportValidationError(f"Canonical CSV is missing: {canonical_path}")
    stage0_path = context.paths.stage0_listing_links
    if not stage0_path.is_file():
        raise ImportValidationError(f"Stage 0 discovery CSV is missing: {stage0_path}")

    canonical_rows = _read_csv_rows(canonical_path)
    stages = manifest.get("stages", {})
    for stage_name in (
        "stage0",
        "stage1",
        "stage2",
        "manual_fixes",
        "stage3",
        "stage3_qc",
    ):
        stage = stages.get(stage_name, {})
        if stage.get("input_rows") is None and stage_name != "stage0":
            raise ImportValidationError(f"Manifest lacks {stage_name} input row count")
        if stage.get("output_rows") is None:
            raise ImportValidationError(f"Manifest lacks {stage_name} output row count")

    qc = stages["stage3_qc"]
    if len(canonical_rows) != qc.get("output_rows"):
        raise ImportValidationError(
            "Canonical CSV row count does not match Stage 3 QC output rows"
        )
    if len(canonical_rows) != stages["stage3"].get("output_rows"):
        raise ImportValidationError(
            "Canonical CSV row count does not match Stage 3 geocoding output rows"
        )

    discovered_rows = _read_csv_rows(stage0_path)
    discovered_urls: dict[str, str] = {}
    for row_number, row in enumerate(discovered_rows, start=2):
        raw_url = row.get("item_page_link") or row.get("listing_url") or row.get("url")
        try:
            source_id, canonical_url = canonical_source_url(raw_url)
        except ImportValidationError as error:
            raise ImportValidationError(
                f"Invalid Stage 0 identity at row {row_number}: {error}"
            ) from error
        discovered_urls[source_id] = canonical_url
    if len(discovered_urls) != stages["stage0"].get("output_rows"):
        raise ImportValidationError(
            "Stage 0 CSV identity count does not match manifest output rows"
        )

    listings: list[CanonicalListing] = []
    seen_ids: set[str] = set()
    for row_number, row in enumerate(canonical_rows, start=2):
        try:
            listing = prepare_canonical_listing(row)
        except ImportValidationError as error:
            raise ImportValidationError(
                f"Invalid canonical identity at row {row_number}: {error}"
            ) from error
        if listing.source_listing_id in seen_ids:
            raise ImportValidationError(
                f"Duplicate source listing ID in canonical CSV: {listing.source_listing_id}"
            )
        if listing.source_listing_id not in discovered_urls:
            raise ImportValidationError(
                f"Canonical listing {listing.source_listing_id} was not present in Stage 0"
            )
        seen_ids.add(listing.source_listing_id)
        listings.append(listing)

    lifecycle_eligible, lifecycle_reasons = determine_lifecycle_eligibility(
        manifest, stage0_actual_rows=len(discovered_urls)
    )
    _required_aware_datetime(manifest.get("created_at_utc"), "created_at_utc")
    observed_at = _required_aware_datetime(
        stages["stage3_qc"].get("completed_at_utc"),
        "stage3_qc.completed_at_utc",
    )
    approval_errors = approval_validation_errors(manifest, canonical_path)
    approval_valid = not approval_errors
    if not approval_valid and not allow_noncanonical_run:
        raise ImportValidationError(
            "Run is not approved or its current approval is invalid: "
            + "; ".join(approval_errors)
            + ". Use --allow-noncanonical-run only as an exceptional reviewed override"
        )
    override_used = bool(not approval_valid and allow_noncanonical_run)
    selected_override_reason: Optional[str] = None
    if override_used:
        selected_override_reason = _clean_text(override_reason)
        if not selected_override_reason:
            raise ImportValidationError(
                "override_reason is required when overriding invalid approval"
            )
        selected_override_reason = sanitize_error(selected_override_reason)[:500]

    return ValidatedRun(
        run_dir=root,
        run_id=run_id,
        source=SOURCE,
        manifest=manifest,
        listings=tuple(listings),
        discovered_ids=frozenset(discovered_urls),
        discovered_urls=discovered_urls,
        canonical_sha256=_file_sha256(canonical_path),
        manifest_sha256=sha256_json(manifest),
        override_used=override_used,
        override_reason=selected_override_reason,
        observed_at=observed_at,
        lifecycle_eligible=lifecycle_eligible,
        lifecycle_ineligibility_reasons=lifecycle_reasons,
    )


def changed_fields(
    previous: Optional[dict[str, Any]], current: dict[str, Any]
) -> tuple[str, ...]:
    if previous is None:
        return tuple(MEANINGFUL_FIELDS)
    return tuple(
        field_name
        for field_name in MEANINGFUL_FIELDS
        if previous.get(field_name) != current.get(field_name)
    )


def _review_key(
    run_id: str,
    source_listing_id: Optional[str],
    review_type: str,
    field_name: Optional[str],
    reason: str,
) -> str:
    return sha256_json(
        {
            "run_id": run_id,
            "source_listing_id": source_listing_id,
            "review_type": review_type,
            "field_name": field_name,
            "reason": reason,
        }
    )


def _review_decision(
    run: ValidatedRun,
    source_listing_id: Optional[str],
    property_ref: Optional[str],
    issue: RowIssue,
) -> ReviewDecision:
    return ReviewDecision(
        source_listing_id=source_listing_id,
        property_ref=property_ref,
        review_type=issue.review_type,
        severity=issue.severity,
        field_name=issue.field_name,
        reason=issue.reason,
        payload=issue.payload,
        dedupe_key=_review_key(
            run.run_id,
            source_listing_id,
            issue.review_type,
            issue.field_name,
            issue.reason,
        ),
    )


def _same_property_for_existing_listing(
    candidate: PropertyCandidate,
    listing: StoredListing,
    state: ExistingState,
) -> Optional[StoredProperty]:
    if listing.property_id is None:
        return None
    existing = state.properties_by_id.get(listing.property_id)
    if existing is None:
        return None
    if (
        existing.normalized_address == candidate.normalized_address
        and existing.unit_identifier == candidate.unit_identifier
    ):
        return existing
    return None


def _incomplete_linked_property_for_enrichment(
    candidate: PropertyCandidate,
    listing: StoredListing,
    state: ExistingState,
) -> Optional[StoredProperty]:
    """Return a linked addressless property that can safely gain identity.

    A recovered complete address may enrich the property's durable row only
    when the existing row has no deterministic identity and no other property
    already owns the candidate's exact match key. This deliberately does not
    merge partial, conflicting, or fuzzy property identities.
    """
    if listing.property_id is None or not candidate.address_complete:
        return None
    if not candidate.match_key or candidate.unit_ambiguous:
        return None
    existing = state.properties_by_id.get(listing.property_id)
    if existing is None:
        return None
    if existing.normalized_address is not None or existing.match_key is not None:
        return None
    competing = state.properties_by_match_key.get(candidate.match_key)
    if competing is not None and competing.id != existing.id:
        return None
    return existing


def build_import_plan(
    run: ValidatedRun,
    state: Optional[ExistingState] = None,
    *,
    missing_run_threshold: int = DEFAULT_MISSING_RUN_THRESHOLD,
    skip_lifecycle_updates: bool = False,
) -> ImportPlan:
    """Create a deterministic write plan against an existing-state snapshot."""
    if missing_run_threshold < 1:
        raise ImportValidationError("missing-run-threshold must be at least 1")
    state = state or ExistingState.empty()
    invalid_statuses = {
        listing.status
        for listing in state.listings_by_source_id.values()
        if listing.status not in VALID_LISTING_STATUSES
    }
    if invalid_statuses:
        raise ImportValidationError(
            f"Existing database state has invalid listing status: {sorted(invalid_statuses)}"
        )
    if state.imported_run_summary is not None:
        summary = {name: int(state.imported_run_summary.get(name, 0)) for name in SUMMARY_FIELDS}
        return ImportPlan(
            run=run,
            properties=(),
            listings=(),
            observations=(),
            geocodes=(),
            reviews=(),
            summary=summary,
            lifecycle_applied=state.imported_run_lifecycle_applied,
            lifecycle_ineligibility_reasons=state.imported_run_lifecycle_reasons,
            already_imported=True,
        )

    if state.latest_imported_run_at:
        try:
            current_time = datetime.fromisoformat(run.observed_at.replace("Z", "+00:00"))
            previous_time = datetime.fromisoformat(
                state.latest_imported_run_at.replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as error:
            raise ImportValidationError(
                "Imported pipeline-run timestamps cannot be ordered safely"
            ) from error
        if current_time <= previous_time:
            raise ImportValidationError(
                "Out-of-order imports are not supported: import completed runs "
                "chronologically so current listing state cannot be rewound"
            )

    summary = {name: 0 for name in SUMMARY_FIELDS}
    positive_lifecycle_updates = not skip_lifecycle_updates
    properties: list[PropertyDecision] = []
    property_by_ref: dict[str, PropertyDecision] = {}
    local_match_refs: dict[str, str] = {}
    incomplete_seen: dict[tuple[Optional[str], Optional[str]], str] = {}
    units_by_address: dict[str, set[Optional[str]]] = {}
    addresses_by_coordinate: dict[tuple[float, float], set[str]] = {}
    for stored_property in state.properties_by_id.values():
        if stored_property.normalized_address:
            units_by_address.setdefault(stored_property.normalized_address, set()).add(
                stored_property.unit_identifier
            )
            if stored_property.latitude is not None and stored_property.longitude is not None:
                coordinate = (
                    round(stored_property.latitude, 6),
                    round(stored_property.longitude, 6),
                )
                addresses_by_coordinate.setdefault(coordinate, set()).add(
                    stored_property.normalized_address
                )
    for stored_listing in state.listings_by_source_id.values():
        if stored_listing.property_id is None:
            continue
        stored_property = state.properties_by_id.get(stored_listing.property_id)
        if (
            stored_property is not None
            and stored_property.match_key is None
            and stored_property.normalized_address
        ):
            incomplete_seen.setdefault(
                (
                    stored_property.normalized_address,
                    stored_property.unit_identifier,
                ),
                stored_listing.source_listing_id,
            )
    listing_decisions: list[ListingDecision] = []
    observation_decisions: list[ObservationDecision] = []
    review_decisions: list[ReviewDecision] = []
    geocodes: dict[tuple[str, str], GeocodeCandidate] = {}
    property_ref_by_listing: dict[str, Optional[str]] = {}

    for canonical in run.listings:
        existing_listing = state.listings_by_source_id.get(canonical.source_listing_id)
        candidate = canonical.property
        property_decision: Optional[PropertyDecision] = None
        existing_property = _same_property_for_existing_listing(
            candidate, existing_listing, state
        ) if existing_listing else None
        enriching_existing_property = False
        if existing_listing is not None and existing_property is None:
            enrichable_property = _incomplete_linked_property_for_enrichment(
                candidate, existing_listing, state
            )
            if (
                enrichable_property is not None
                and candidate.match_key not in local_match_refs
                and f"existing:{enrichable_property.id}" not in property_by_ref
            ):
                existing_property = enrichable_property
                enriching_existing_property = True
        retained_incomplete_property = False
        if (
            existing_listing is not None
            and existing_property is None
            and existing_listing.property_id is not None
            and not candidate.address_complete
        ):
            existing_property = state.properties_by_id.get(existing_listing.property_id)
            retained_incomplete_property = existing_property is not None

        if existing_property is not None:
            ref = f"existing:{existing_property.id}"
            property_decision = property_by_ref.get(ref)
            if property_decision is None:
                property_decision = PropertyDecision(
                    ref,
                    candidate,
                    existing_property.id,
                    True,
                    enrich_identity=enriching_existing_property,
                )
                property_by_ref[ref] = property_decision
                properties.append(property_decision)
                if enriching_existing_property and candidate.match_key:
                    local_match_refs[candidate.match_key] = ref
            summary["properties_reused"] += 1
        elif candidate.match_key and candidate.match_key in state.properties_by_match_key:
            stored = state.properties_by_match_key[candidate.match_key]
            ref = f"existing:{stored.id}"
            property_decision = property_by_ref.get(ref)
            if property_decision is None:
                property_decision = PropertyDecision(ref, candidate, stored.id, True)
                property_by_ref[ref] = property_decision
                properties.append(property_decision)
            summary["properties_reused"] += 1
        elif candidate.match_key and candidate.match_key in local_match_refs:
            ref = local_match_refs[candidate.match_key]
            property_decision = property_by_ref[ref]
            summary["properties_reused"] += 1
        else:
            ref = f"new:{canonical.source_listing_id}"
            property_decision = PropertyDecision(ref, candidate, None, False)
            property_by_ref[ref] = property_decision
            properties.append(property_decision)
            summary["properties_created"] += 1
            if candidate.match_key:
                local_match_refs[candidate.match_key] = ref

        if enriching_existing_property:
            review_decisions.append(
                _review_decision(
                    run,
                    canonical.source_listing_id,
                    property_decision.ref,
                    RowIssue(
                        "property_match",
                        "info",
                        "address",
                        "existing_incomplete_property_enriched",
                        {"enriched_property_id": existing_listing.property_id},
                    ),
                )
            )
        elif existing_listing and existing_listing.property_id and existing_property is None:
            review_decisions.append(
                _review_decision(
                    run,
                    canonical.source_listing_id,
                    property_decision.ref,
                    RowIssue(
                        "property_match",
                        "warning",
                        "address",
                        "listing_property_address_changed",
                        {"previous_property_id": existing_listing.property_id},
                    ),
                )
            )
        elif retained_incomplete_property:
            review_decisions.append(
                _review_decision(
                    run,
                    canonical.source_listing_id,
                    property_decision.ref,
                    RowIssue(
                        "property_match",
                        "warning",
                        "address",
                        "incomplete_address_did_not_replace_existing_property",
                        {"retained_property_id": existing_listing.property_id},
                    ),
                )
            )

        property_ref_by_listing[canonical.source_listing_id] = property_decision.ref
        if candidate.normalized_address:
            known_units = units_by_address.setdefault(
                candidate.normalized_address, set()
            )
            if known_units and candidate.unit_identifier not in known_units and (
                candidate.unit_identifier is None or None in known_units
            ):
                summary["possible_property_duplicates"] += 1
                review_decisions.append(
                    _review_decision(
                        run,
                        canonical.source_listing_id,
                        property_decision.ref,
                        RowIssue(
                            "property_match",
                            "warning",
                            "address",
                            "unit_presence_differs_from_possible_property_match",
                            {
                                "unit_identifier": candidate.unit_identifier,
                                "known_unit_identifiers": sorted(
                                    item or "no-unit" for item in known_units
                                ),
                            },
                        ),
                    )
                )
            known_units.add(candidate.unit_identifier)
        if (
            candidate.normalized_address
            and candidate.latitude is not None
            and candidate.longitude is not None
        ):
            coordinate = (round(candidate.latitude, 6), round(candidate.longitude, 6))
            known_addresses = addresses_by_coordinate.setdefault(coordinate, set())
            if known_addresses and candidate.normalized_address not in known_addresses:
                summary["possible_property_duplicates"] += 1
                review_decisions.append(
                    _review_decision(
                        run,
                        canonical.source_listing_id,
                        property_decision.ref,
                        RowIssue(
                            "property_match",
                            "info",
                            "address",
                            "coordinates_match_different_normalized_address",
                            {"other_normalized_addresses": sorted(known_addresses)},
                        ),
                    )
                )
            known_addresses.add(candidate.normalized_address)
        if not candidate.match_key and candidate.normalized_address:
            incomplete_key = (candidate.normalized_address, candidate.unit_identifier)
            other = incomplete_seen.get(incomplete_key)
            if other and other != canonical.source_listing_id:
                summary["possible_property_duplicates"] += 1
                review_decisions.append(
                    _review_decision(
                        run,
                        canonical.source_listing_id,
                        property_decision.ref,
                        RowIssue(
                            "property_match",
                            "warning",
                            "address",
                            "incomplete_exact_address_not_automatically_merged",
                            {"other_source_listing_id": other},
                        ),
                    )
                )
            else:
                incomplete_seen[incomplete_key] = canonical.source_listing_id

        if existing_listing is None:
            classification = "new"
            new_status = "active"
            summary["new_listings"] += 1
        elif existing_listing.status == "removed" and positive_lifecycle_updates:
            classification = "relisted"
            new_status = "relisted"
            summary["relisted_listings"] += 1
        else:
            classification = (
                "unchanged"
                if existing_listing.latest_observation_hash == canonical.observation_hash
                else "updated"
            )
            if positive_lifecycle_updates:
                new_status = (
                    "active"
                    if existing_listing.status == "possibly_removed"
                    else existing_listing.status
                )
                if new_status not in {"active", "relisted"}:
                    new_status = "active"
            else:
                new_status = existing_listing.status
            summary[f"{classification}_listings"] += 1

        listing_decisions.append(
            ListingDecision(
                source_listing_id=canonical.source_listing_id,
                source_url=canonical.source_url,
                existing_id=existing_listing.id if existing_listing else None,
                property_ref=property_decision.ref,
                classification=classification,
                new_status=new_status,
                missing_run_count=(
                    0
                    if positive_lifecycle_updates or existing_listing is None
                    else existing_listing.missing_run_count
                ),
                seen_in_canonical=True,
                refresh_current_state=(
                    existing_listing is None or positive_lifecycle_updates
                ),
            )
        )
        if existing_listing is None or run.run_id not in existing_listing.observation_run_ids:
            changes = changed_fields(
                existing_listing.latest_comparison_data if existing_listing else None,
                canonical.comparison_data,
            )
            observation_decisions.append(
                ObservationDecision(
                    canonical.source_listing_id, classification, changes, canonical
                )
            )
            summary["observations_inserted"] += 1

        for issue in canonical.issues:
            review_decisions.append(
                _review_decision(
                    run, canonical.source_listing_id, property_decision.ref, issue
                )
            )
        if canonical.geocode:
            geocodes[(canonical.geocode.normalized_query, GEOCODE_PROVIDER)] = canonical.geocode

    # Strong exact duplicate-ad signal: same property, normalized description,
    # monthly price, and bedroom count. Identities remain separate.
    duplicate_groups: dict[tuple[Any, ...], set[str]] = {}
    for stored_listing in state.listings_by_source_id.values():
        if stored_listing.property_id is None or not stored_listing.latest_comparison_data:
            continue
        stored_property = state.properties_by_id.get(stored_listing.property_id)
        if stored_property is None or not stored_property.match_key:
            continue
        previous = stored_listing.latest_comparison_data
        description = _comparison_text(previous.get("description"))
        if not description:
            continue
        duplicate_key = (
            stored_property.match_key,
            description,
            previous.get("price_monthly"),
            previous.get("bedrooms"),
        )
        duplicate_groups.setdefault(duplicate_key, set()).add(
            stored_listing.source_listing_id
        )
    for canonical in run.listings:
        description = _comparison_text(canonical.values.get("description"))
        if not description or not canonical.property.match_key:
            continue
        duplicate_key = (
            canonical.property.match_key,
            description,
            canonical.values.get("price_monthly"),
            canonical.values.get("bedrooms"),
        )
        duplicate_groups.setdefault(duplicate_key, set()).add(
            canonical.source_listing_id
        )
    current_source_ids = {listing.source_listing_id for listing in run.listings}
    for source_ids in duplicate_groups.values():
        if len(source_ids) < 2:
            continue
        for source_id in sorted(source_ids & current_source_ids):
            others = sorted(item for item in source_ids if item != source_id)
            review_decisions.append(
                _review_decision(
                    run,
                    source_id,
                    property_ref_by_listing[source_id],
                    RowIssue(
                        "identity",
                        "warning",
                        None,
                        "possible_duplicate_advertisement",
                        {"other_source_listing_ids": others},
                    ),
                )
            )

    canonical_ids = {item.source_listing_id for item in run.listings}
    # Positive Stage 0 sightings reset missing state even if Stage 1 failed and
    # therefore no canonical observation was produced.
    for source_id in sorted(run.discovered_ids - canonical_ids):
        existing = state.listings_by_source_id.get(source_id)
        if existing is None:
            continue
        if existing.status == "removed" and positive_lifecycle_updates:
            new_status = "relisted"
            summary["relisted_listings"] += 1
        elif existing.status == "possibly_removed" and positive_lifecycle_updates:
            new_status = "active"
        else:
            new_status = existing.status
        listing_decisions.append(
            ListingDecision(
                source_id,
                run.discovered_urls[source_id],
                existing.id,
                None,
                None,
                new_status,
                0 if positive_lifecycle_updates else existing.missing_run_count,
                False,
                positive_lifecycle_updates,
            )
        )

    lifecycle_reasons = list(run.lifecycle_ineligibility_reasons)
    if skip_lifecycle_updates:
        lifecycle_reasons.append("lifecycle updates explicitly skipped")
    if state.latest_lifecycle_run_at:
        try:
            current_time = datetime.fromisoformat(run.observed_at.replace("Z", "+00:00"))
            previous_time = datetime.fromisoformat(
                state.latest_lifecycle_run_at.replace("Z", "+00:00")
            )
        except ValueError:
            lifecycle_reasons.append("run timestamps cannot be ordered safely")
        else:
            if current_time <= previous_time:
                lifecycle_reasons.append(
                    "run is not newer than the latest lifecycle-eligible import"
                )
    lifecycle_applied = not lifecycle_reasons
    if lifecycle_applied:
        for source_id, existing in sorted(state.listings_by_source_id.items()):
            if source_id in run.discovered_ids:
                continue
            missing_count = existing.missing_run_count + 1
            if missing_count >= missing_run_threshold:
                new_status = "removed"
                if existing.status != "removed":
                    summary["removed_listings"] += 1
            else:
                new_status = "possibly_removed"
                if existing.status != "possibly_removed":
                    summary["possibly_removed_listings"] += 1
            listing_decisions.append(
                ListingDecision(
                    source_id,
                    existing.source_url,
                    existing.id,
                    None,
                    None,
                    new_status,
                    missing_count,
                    False,
                    False,
                )
            )

    # A review's deterministic key is the idempotency boundary. Do not update or
    # reopen an existing human-managed review item.
    unique_reviews: dict[str, ReviewDecision] = {}
    for review in review_decisions:
        if review.dedupe_key not in state.review_keys:
            unique_reviews.setdefault(review.dedupe_key, review)
    summary["review_items_created"] = len(unique_reviews)
    summary["rows_with_warnings"] = len(
        {
            review.source_listing_id
            for review in unique_reviews.values()
            if review.source_listing_id is not None
        }
    )

    return ImportPlan(
        run=run,
        properties=tuple(properties),
        listings=tuple(listing_decisions),
        observations=tuple(observation_decisions),
        geocodes=tuple(geocodes.values()),
        reviews=tuple(unique_reviews.values()),
        summary=summary,
        lifecycle_applied=lifecycle_applied,
        lifecycle_ineligibility_reasons=tuple(dict.fromkeys(lifecycle_reasons)),
    )


def print_change_summary(plan: ImportPlan, *, dry_run: bool) -> None:
    mode = "offline dry run (empty database baseline)" if dry_run else "database import"
    if plan.run.override_used:
        print(
            "WARNING: exceptional noncanonical override used; this run is not "
            "equivalent to an approved run. "
            f"Reason: {plan.run.override_reason}",
            file=sys.stderr,
        )
    print(f"Stage 4 {mode}: run_id={plan.run.run_id}")
    if plan.already_imported:
        print("Run was already imported; no writes or lifecycle transitions were replayed.")
    for field_name in SUMMARY_FIELDS:
        print(f"  {field_name}: {plan.summary[field_name]}")
    print(f"  lifecycle_updates_applied: {plan.lifecycle_applied}")
    if plan.lifecycle_ineligibility_reasons:
        print("  lifecycle_ineligibility_reasons:")
        for reason in plan.lifecycle_ineligibility_reasons:
            print(f"    - {reason}")
    print(f"  noncanonical_override_used: {plan.run.override_used}")
    print(f"  noncanonical_override_reason: {plan.run.override_reason or 'none'}")


def import_configuration(
    *,
    missing_run_threshold: int,
    skip_lifecycle_updates: bool,
    override_used: bool = False,
    override_reason: Optional[str] = None,
) -> dict[str, Any]:
    configuration: dict[str, Any] = {
        "missing_run_threshold": missing_run_threshold,
        "skip_lifecycle_updates": skip_lifecycle_updates,
    }
    if override_used:
        configuration["noncanonical_override"] = {
            "used": True,
            "reason": override_reason,
        }
    return configuration


class InMemoryImportDatabase:
    """Deterministic transaction-like store for offline planner tests.

    It is not presented as a PostgreSQL substitute. PostgreSQL-specific SQL and
    constraints remain covered by static migration tests and optional real-DB
    integration tests.
    """

    def __init__(self) -> None:
        self.state = ExistingState.empty()
        self.imported_runs: dict[
            str,
            tuple[
                str,
                str,
                dict[str, int],
                bool,
                tuple[str, ...],
                dict[str, Any],
            ],
        ] = {}
        self._next_property_id = 1
        self._next_listing_id = 1

    def import_run(
        self,
        run: ValidatedRun,
        *,
        missing_run_threshold: int = DEFAULT_MISSING_RUN_THRESHOLD,
        skip_lifecycle_updates: bool = False,
        fail_after_plan: bool = False,
    ) -> ImportPlan:
        selected_configuration = import_configuration(
            missing_run_threshold=missing_run_threshold,
            skip_lifecycle_updates=skip_lifecycle_updates,
            override_used=run.override_used,
            override_reason=run.override_reason,
        )
        imported = self.imported_runs.get(run.run_id)
        if imported:
            (
                canonical_hash,
                manifest_hash,
                summary,
                lifecycle_applied,
                lifecycle_reasons,
                stored_configuration,
            ) = imported
            if (
                canonical_hash != run.canonical_sha256
                or manifest_hash != run.manifest_sha256
            ):
                raise ImportValidationError(
                    "The selected run_id was already imported with different content"
                )
            if stored_configuration != selected_configuration:
                raise ImportValidationError(
                    "The selected run_id was already imported with different Stage 4 configuration"
                )
            repeat_state = copy.deepcopy(self.state)
            repeat_state.imported_run_summary = summary
            repeat_state.imported_run_lifecycle_applied = lifecycle_applied
            repeat_state.imported_run_lifecycle_reasons = lifecycle_reasons
            return build_import_plan(
                run,
                repeat_state,
                missing_run_threshold=missing_run_threshold,
                skip_lifecycle_updates=skip_lifecycle_updates,
            )

        before = copy.deepcopy(
            (
                self.state,
                self.imported_runs,
                self._next_property_id,
                self._next_listing_id,
            )
        )
        try:
            plan = build_import_plan(
                run,
                self.state,
                missing_run_threshold=missing_run_threshold,
                skip_lifecycle_updates=skip_lifecycle_updates,
            )
            if fail_after_plan:
                raise RuntimeError("injected import failure")
            property_ids: dict[str, int] = {}
            for decision in plan.properties:
                if decision.existing_id is not None:
                    property_id = decision.existing_id
                    if decision.enrich_identity:
                        stored_property = StoredProperty(
                            property_id,
                            decision.candidate.normalized_address,
                            decision.candidate.unit_identifier,
                            decision.candidate.match_key,
                            decision.candidate.latitude,
                            decision.candidate.longitude,
                        )
                        self.state.properties_by_id[property_id] = stored_property
                        if decision.candidate.match_key:
                            self.state.properties_by_match_key[
                                decision.candidate.match_key
                            ] = stored_property
                else:
                    property_id = self._next_property_id
                    self._next_property_id += 1
                    stored_property = StoredProperty(
                        property_id,
                        decision.candidate.normalized_address,
                        decision.candidate.unit_identifier,
                        decision.candidate.match_key,
                        decision.candidate.latitude,
                        decision.candidate.longitude,
                    )
                    self.state.properties_by_id[property_id] = stored_property
                    if decision.candidate.match_key:
                        self.state.properties_by_match_key[
                            decision.candidate.match_key
                        ] = stored_property
                property_ids[decision.ref] = property_id

            observations = {
                item.source_listing_id: item for item in plan.observations
            }
            for decision in plan.listings:
                previous = self.state.listings_by_source_id.get(
                    decision.source_listing_id
                )
                listing_id = previous.id if previous else self._next_listing_id
                if previous is None:
                    self._next_listing_id += 1
                observation = observations.get(decision.source_listing_id)
                observation_runs = set(
                    previous.observation_run_ids if previous else frozenset()
                )
                latest_hash = previous.latest_observation_hash if previous else None
                latest_data = previous.latest_comparison_data if previous else None
                if observation:
                    observation_runs.add(run.run_id)
                    latest_hash = observation.listing.observation_hash
                    latest_data = observation.listing.comparison_data
                if previous is not None and not decision.refresh_current_state:
                    property_id = previous.property_id
                    source_url = previous.source_url
                else:
                    property_id = (
                        property_ids.get(decision.property_ref)
                        if decision.property_ref
                        else (previous.property_id if previous else None)
                    )
                    source_url = decision.source_url
                self.state.listings_by_source_id[decision.source_listing_id] = (
                    StoredListing(
                        id=listing_id,
                        source_listing_id=decision.source_listing_id,
                        property_id=property_id,
                        source_url=source_url,
                        status=decision.new_status,
                        missing_run_count=decision.missing_run_count,
                        latest_observation_hash=latest_hash,
                        latest_comparison_data=latest_data,
                        observation_run_ids=frozenset(observation_runs),
                    )
                )
            self.state.review_keys.update(item.dedupe_key for item in plan.reviews)
            if plan.lifecycle_applied:
                self.state.latest_lifecycle_run_at = run.observed_at
            self.state.latest_imported_run_at = run.observed_at
            self.imported_runs[run.run_id] = (
                run.canonical_sha256,
                run.manifest_sha256,
                dict(plan.summary),
                plan.lifecycle_applied,
                plan.lifecycle_ineligibility_reasons,
                selected_configuration,
            )
            return plan
        except Exception:
            (
                self.state,
                self.imported_runs,
                self._next_property_id,
                self._next_listing_id,
            ) = before
            raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--database-url")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-noncanonical-run", action="store_true")
    parser.add_argument("--override-reason")
    parser.add_argument(
        "--missing-run-threshold", type=int, default=DEFAULT_MISSING_RUN_THRESHOLD
    )
    parser.add_argument("--skip-lifecycle-updates", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.missing_run_threshold < 1:
        raise SystemExit("--missing-run-threshold must be at least 1")
    if args.allow_noncanonical_run and not _clean_text(args.override_reason):
        raise SystemExit(
            "--override-reason is required with --allow-noncanonical-run"
        )
    if args.override_reason and not args.allow_noncanonical_run:
        raise SystemExit(
            "--override-reason requires --allow-noncanonical-run"
        )
    validated = load_and_validate_run(
        args.run_dir,
        allow_noncanonical_run=args.allow_noncanonical_run,
        override_reason=args.override_reason,
    )
    if args.dry_run:
        plan = build_import_plan(
            validated,
            ExistingState.empty(),
            missing_run_threshold=args.missing_run_threshold,
            skip_lifecycle_updates=args.skip_lifecycle_updates,
        )
        print_change_summary(plan, dry_run=True)
        return

    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL or --database-url is required for a connected import")
    from pipeline.postgres_repository import import_validated_run

    plan = import_validated_run(
        validated,
        database_url=database_url,
        missing_run_threshold=args.missing_run_threshold,
        skip_lifecycle_updates=args.skip_lifecycle_updates,
    )
    print_change_summary(plan, dry_run=False)


if __name__ == "__main__":
    main()
