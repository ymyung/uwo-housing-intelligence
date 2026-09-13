"""Deterministic, evidence-preserving review and decision application workflow."""

from __future__ import annotations

import copy
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Optional, Protocol

from pipeline.canonical_rebuild import rebuild_canonical
from pipeline.database_importer import calculate_monthly_price, normalize_price_period
from pipeline.geocoder import normalize_address
from pipeline.operator_summary import derive_current_metrics
from pipeline.run_approval import evaluate_run_approval, sha256_file
from pipeline.run_context import (
    RunContext,
    atomic_write_json,
    determine_run_completion,
    manifest_lock,
    utc_text,
    validate_run_id,
)


DECISION_STATUSES = {
    "auto_resolved",
    "human_review_required",
    "accepted_as_unknown",
    "excluded",
    "already_resolved",
}
SAFE_APPLICATION_STATUSES = {
    "auto_resolved",
    "accepted_as_unknown",
    "already_resolved",
}
IMMUTABLE_FIELDS = {"listing_id", "listing_url", "source_url", "item_page_link"}
GEOCODE_FIELDS = {
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
    "geocode_provider",
    "geocode_manual_override",
    "geocode_manual_override_reason",
    "geocode_review_disposition",
}
REVIEW_OUTPUTS = (
    "review-decisions.jsonl",
    "remaining-human-review.csv",
    "auto-resolved.csv",
    "accepted-unknown.csv",
    "review-auto-summary.json",
)
REQUIRED_DECISION_FIELDS = (
    "decision_id",
    "run_id",
    "listing_id",
    "field",
    "original_value",
    "selected_value",
    "decision_status",
    "reason_code",
    "evidence",
    "evidence_source",
    "confidence",
    "reviewer_type",
    "created_at_utc",
    "input_fingerprint",
)
FLAG_FIELD_MAP = {
    "furnished_false_but_description_mentions_furnished": "furnished",
    "is_sublet_true_but_ai_confidence_low": "is_sublet",
    "utilities_included_contradicts_description": "utilities_included",
    "bathroom_type_low_ai_confidence": "bathroom_type",
}
RULE_SENTINELS = {"", "unknown", "not_specified", "none", "null", "nan", "nat"}
LONDON_BOUNDS = (42.75, 43.25, -81.55, -80.85)


class ReviewWorkflowError(RuntimeError):
    """Review inputs or decisions are unsafe, stale, or incomplete."""


class ReviewProposer(Protocol):
    """Optional proposal interface; proposals never become automatic decisions."""

    def propose(
        self, row: dict[str, str], *, field: str, reason_code: str
    ) -> Optional[dict[str, Any]]:
        """Return proposed_value/supporting_text/conflicting_text/reasoning_summary."""


@dataclass(frozen=True)
class ReviewConfig:
    geocode_confidence_threshold: float = 0.8
    london_min_latitude: float = LONDON_BOUNDS[0]
    london_max_latitude: float = LONDON_BOUNDS[1]
    london_min_longitude: float = LONDON_BOUNDS[2]
    london_max_longitude: float = LONDON_BOUNDS[3]

    def __post_init__(self) -> None:
        if not 0 <= self.geocode_confidence_threshold <= 1:
            raise ValueError("geocode_confidence_threshold must be between 0 and 1")
        if self.london_min_latitude >= self.london_max_latitude:
            raise ValueError("latitude bounds are invalid")
        if self.london_min_longitude >= self.london_max_longitude:
            raise ValueError("longitude bounds are invalid")


@dataclass
class ReviewIssue:
    listing_id: str
    field: str
    reason_code: str
    categories: set[str]
    review_flag: Optional[str] = None
    evidence: dict[str, Any] | None = None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.casefold() in RULE_SENTINELS else text


def _truth(value: Any) -> bool:
    return _clean(value).casefold() in {"true", "1", "yes", "y"}


def _number(value: Any) -> Optional[float]:
    text = _clean(value).replace(",", "")
    if not text:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def listing_fingerprint(row: dict[str, Any]) -> str:
    """Fingerprint all current listing evidence used to make a decision."""

    return hashlib.sha256(_canonical_json(row).encode("utf-8")).hexdigest()


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise ReviewWorkflowError(f"Required review artifact is missing: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames:
                raise ReviewWorkflowError(f"CSV has no header: {path}")
            duplicates = {
                field for field in reader.fieldnames if reader.fieldnames.count(field) > 1
            }
            if duplicates:
                raise ReviewWorkflowError(
                    f"CSV has duplicate columns: {sorted(duplicates)}"
                )
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReviewWorkflowError(f"Cannot read review artifact {path}: {exc}") from exc
    return list(reader.fieldnames), rows


def _index(rows: Iterable[dict[str, str]], *, artifact: str) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row_number, row in enumerate(rows, start=2):
        listing_id = _clean(row.get("listing_id"))
        if not listing_id:
            raise ReviewWorkflowError(
                f"{artifact} has a missing listing_id at row {row_number}"
            )
        if listing_id in result:
            raise ReviewWorkflowError(
                f"{artifact} has duplicate listing_id {listing_id!r}"
            )
        result[listing_id] = row
    return result


def _csv_bytes(fieldnames: list[str], rows: list[dict[str, Any]]) -> bytes:
    from io import StringIO

    output = StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=fieldnames, extrasaction="raise", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _atomic_bytes(path: Path, content: bytes) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
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


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    _atomic_bytes(path, _csv_bytes(fieldnames, rows))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ReviewWorkflowError(
                        f"JSONL record {line_number} is not an object"
                    )
                records.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewWorkflowError(f"Review decisions JSONL is invalid: {exc}") from exc
    return records


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    content = "".join(_canonical_json(record) + "\n" for record in records)
    _atomic_bytes(path, content.encode("utf-8"))


def _parse_flags(value: Any) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = text.split("|") if "|" in text else text.split(",")
    if not isinstance(parsed, list):
        parsed = [parsed]
    return sorted({_clean(item) for item in parsed if _clean(item)})


def _field_for_flag(flag: str) -> str:
    if flag in FLAG_FIELD_MAP:
        return FLAG_FIELD_MAP[flag]
    if flag.startswith("unusual_price_"):
        return "price_monthly"
    if flag.startswith("unusual_lease_term_months_"):
        return "lease_term_months"
    if flag.startswith("consensus_disagreement_"):
        return flag.removeprefix("consensus_disagreement_")
    if flag.endswith("_ai_evidence_blocked"):
        return flag.removesuffix("_ai_evidence_blocked")
    return "__listing__"


def _add_issue(
    issues: dict[tuple[str, str], ReviewIssue],
    *,
    listing_id: str,
    field: str,
    reason_code: str,
    category: str,
    review_flag: Optional[str] = None,
    evidence: Optional[dict[str, Any]] = None,
) -> None:
    key = (field, reason_code)
    issue = issues.get(key)
    if issue is None:
        issue = ReviewIssue(
            listing_id,
            field,
            reason_code,
            set(),
            review_flag=review_flag,
            evidence=evidence or {},
        )
        issues[key] = issue
    issue.categories.add(category)


def _discover_issues(row: dict[str, str]) -> list[ReviewIssue]:
    listing_id = _clean(row.get("listing_id"))
    issues: dict[tuple[str, str], ReviewIssue] = {}
    flags = _parse_flags(row.get("review_flags"))
    for flag in flags:
        _add_issue(
            issues,
            listing_id=listing_id,
            field=_field_for_flag(flag),
            reason_code=flag,
            category="ai_review",
            review_flag=flag,
        )
    if _truth(row.get("needs_manual_review")):
        if not flags:
            _add_issue(
                issues,
                listing_id=listing_id,
                field="__listing__",
                reason_code="needs_manual_review",
                category="ai_review",
            )
        if not _truth(row.get("manual_reviewed")):
            for issue in issues.values():
                issue.categories.add("unresolved_manual_review")

    for field, value in row.items():
        if not field.endswith("_rule"):
            continue
        canonical_field = field.removesuffix("_rule")
        current = _clean(row.get(canonical_field))
        rule = _clean(value)
        if rule and current and current.casefold() != rule.casefold():
            _add_issue(
                issues,
                listing_id=listing_id,
                field=canonical_field,
                reason_code="conflicting_deterministic_source",
                category="source_conflict",
                evidence={"rule_field": field, "rule_value": rule},
            )

    if not _clean(row.get("price_monthly")):
        _add_issue(
            issues,
            listing_id=listing_id,
            field="price_monthly",
            reason_code="missing_monthly_price",
            category="missing_monthly_price",
        )

    quality = _clean(row.get("geocode_quality_issue"))
    if not _truth(row.get("map_ready")) or quality:
        _add_issue(
            issues,
            listing_id=listing_id,
            field="map_ready",
            reason_code="geocode_review_required",
            category="geocoding_review",
            evidence={"geocode_quality_issue": quality},
        )
        issues[("map_ready", "geocode_review_required")].categories.add(
            "map_readiness"
        )
    return sorted(issues.values(), key=lambda issue: (issue.field, issue.reason_code))


def _explicit_text_value(field: str, text: str) -> tuple[Any, list[str], list[str]]:
    normalized = " ".join(text.casefold().split())
    supporting: list[str] = []
    conflicting: list[str] = []

    def matches(patterns: Iterable[str]) -> list[str]:
        found: list[str] = []
        for pattern in patterns:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                found.append(match.group(0))
        return found

    if field == "is_sublet":
        negative = matches((r"\bnot\s+(?:a\s+)?sublet\b", r"\bno\s+subletting\b"))
        hypothetical = (
            r"\b(?:may|can|could)\s+(?:be\s+)?(?:sublet|sublease|subletting)\b",
            r"\boption to (?:sublet|sublease)\b",
            r"\bsubletting (?:is )?(?:allowed|permitted)\b",
        )
        current_listing_text = normalized
        for pattern in hypothetical:
            current_listing_text = re.sub(pattern, "", current_listing_text)
        positive = []
        for pattern in (r"\bsublet\b", r"\bsublease\b", r"\bsubletting\b"):
            match = re.search(pattern, current_listing_text, re.IGNORECASE)
            if match:
                positive.append(match.group(0))
        positive = [item for item in positive if not negative]
        supporting, conflicting = positive or negative, negative if positive else []
        if positive and not negative:
            return True, supporting, []
        if negative and not positive:
            return False, negative, []
    elif field == "furnished":
        positive = matches((r"\bfully furnished\b", r"\bfurnished (?:room|unit|home|house|apartment)\b"))
        negative = matches((r"\bunfurnished\b", r"\bnot furnished\b"))
        if positive and not negative:
            return True, positive, []
        if negative and not positive:
            return False, negative, []
        supporting, conflicting = positive, negative
    elif field == "utilities_included":
        positive = matches((r"\butilities (?:are )?included\b", r"\ball[- ]inclusive\b"))
        negative = matches((r"\bplus utilities\b", r"\butilities (?:are )?(?:extra|not included)\b"))
        if positive and not negative:
            return True, positive, []
        if negative and not positive:
            return False, negative, []
        supporting, conflicting = positive, negative
    elif field == "bathroom_type":
        private = matches((r"\bprivate bathroom\b", r"\bensuite bathroom\b", r"\ben-suite\b"))
        shared = matches((r"\bshared bathroom\b", r"\bbathroom shared\b"))
        if private and not shared:
            return "private", private, []
        if shared and not private:
            return "shared", shared, []
        supporting, conflicting = private, shared
    elif field == "preferred_gender":
        female = matches((r"\bfemale(?:s)? only\b", r"\bwomen only\b"))
        male = matches((r"\bmale(?:s)? only\b", r"\bmen only\b"))
        female_preferred = matches(
            (
                r"\bfemale(?:s)? (?:preferred|preference)\b",
                r"\bwomen (?:preferred|preference)\b",
                r"\bprefer(?:red|ence)? (?:female(?:s)?|women)\b",
            )
        )
        male_preferred = matches(
            (
                r"\bmale(?:s)? (?:preferred|preference)\b",
                r"\bmen (?:preferred|preference)\b",
                r"\bprefer(?:red|ence)? (?:male(?:s)?|men)\b",
            )
        )
        any_gender = matches((r"\bany gender\b", r"\bno gender preference\b", r"\bco-?ed\b"))
        present = [
            ("female_only", female),
            ("male_only", male),
            ("female_preferred", female_preferred),
            ("male_preferred", male_preferred),
            ("any", any_gender),
        ]
        selected = [(value, evidence) for value, evidence in present if evidence]
        if len(selected) == 1:
            return selected[0][0], selected[0][1], []
        if selected:
            supporting = selected[0][1]
            conflicting = [item for _, values in selected[1:] for item in values]
    elif field == "lease_term_months":
        values = {
            int(match.group(1))
            for match in re.finditer(r"\b(\d{1,2})[- ]month lease\b", normalized)
        }
        if len(values) == 1:
            value = next(iter(values))
            return value, [f"{value}-month lease"], []
        if len(values) > 1:
            conflicting = [f"{value}-month lease" for value in sorted(values)]
    return None, supporting, conflicting


def _infer_price_period(row: dict[str, str]) -> Optional[str]:
    text = f"{_clean(row.get('price_text'))} {_clean(row.get('description'))}".casefold()
    matches: set[str] = set()
    if re.search(r"(?:per|/)\s*week\b", text):
        matches.add("week")
    if re.search(r"(?:per|/)\s*day\b", text):
        matches.add("day")
    if "per bdrm" in text or "per bedroom" in text:
        matches.add("month_per_bedroom")
    if re.search(r"(?:per|/)\s*month\b|\bmonthly\b", text):
        matches.add("month")
    if len(matches) > 1:
        return "ambiguous"
    return next(iter(matches), None)


def _load_cache_candidates(
    path: Optional[Path], query: str
) -> list[dict[str, str]]:
    if path is None or not path.is_file() or not query:
        return []
    _, rows = _read_csv(path)
    normalized = " ".join(query.casefold().split())
    return [
        row
        for row in rows
        if " ".join(_clean(row.get("geocode_query")).casefold().split()) == normalized
    ]


def _geocode_resolution(
    row: dict[str, str], *, cache_path: Optional[Path], config: ReviewConfig
) -> tuple[bool, dict[str, Any], str]:
    address = _clean(row.get("address"))
    expected_query = normalize_address(address)
    query = _clean(row.get("geocode_query"))
    if not address or not expected_query or expected_query.casefold() != query.casefold():
        return False, {}, "geocode_address_query_mismatch"
    candidates = _load_cache_candidates(cache_path, query)
    if len(candidates) != 1:
        return False, {"candidate_count": len(candidates)}, (
            "geocode_multiple_cached_candidates" if len(candidates) > 1 else "geocode_cache_evidence_missing"
        )
    cached = candidates[0]
    provider = _clean(cached.get("provider") or "geoapify").casefold()
    if provider != "geoapify":
        return False, {"provider": provider}, "geocode_provider_mismatch"
    fields = ("latitude", "longitude", "geocode_confidence", "geocode_status")
    if any(_clean(cached.get(field)) != _clean(row.get(field)) for field in fields):
        return False, {}, "geocode_cache_result_mismatch"
    latitude = _number(row.get("latitude"))
    longitude = _number(row.get("longitude"))
    confidence = _number(row.get("geocode_confidence"))
    in_bounds = bool(
        latitude is not None
        and longitude is not None
        and config.london_min_latitude <= latitude <= config.london_max_latitude
        and config.london_min_longitude <= longitude <= config.london_max_longitude
    )
    if not in_bounds:
        return False, {"latitude": latitude, "longitude": longitude}, "geocode_outside_london_bounds"
    if confidence is None or confidence < config.geocode_confidence_threshold:
        return False, {"confidence": confidence}, "geocode_confidence_below_threshold"
    if _clean(row.get("geocode_status")).casefold() != "ok":
        return False, {}, "geocode_status_not_ok"
    if _clean(row.get("geocode_result_type")).casefold() != "building":
        return False, {}, "geocode_property_precision_required"
    if _clean(row.get("geocode_match_type")).casefold() != "full_match":
        return False, {}, "geocode_full_match_required"
    return True, {
        "normalized_query": query,
        "provider": provider,
        "confidence": confidence,
        "cache_sha256": sha256_file(cache_path),
    }, "cached_geocode_verified"


def _decision_id(
    run_id: str,
    listing_id: str,
    field: str,
    issue_code: str,
    reason_code: str,
    fingerprint: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "run_id": run_id,
                "listing_id": listing_id,
                "field": field,
                "issue_code": issue_code,
                "reason_code": reason_code,
                "input_fingerprint": fingerprint,
            }
        ).encode("utf-8")
    ).hexdigest()


def _make_decision(
    run_id: str,
    row: dict[str, str],
    issue: ReviewIssue,
    *,
    created_at: str,
    cache_path: Optional[Path],
    config: ReviewConfig,
    proposer: Optional[ReviewProposer],
) -> dict[str, Any]:
    field = issue.field
    original = None if field == "__listing__" else row.get(field)
    selected: Any = original
    status = "human_review_required"
    reason = issue.reason_code
    evidence_source = "current_canonical"
    confidence = 0.0
    evidence: dict[str, Any] = dict(issue.evidence or {})
    apply_updates: dict[str, Any] = {}
    proposed_value: Any = None
    supporting_text: list[str] = []
    conflicting_text: list[str] = []
    reasoning_summary = "No deterministic rule resolved this issue."

    source = _clean(row.get(f"{field}_source")).casefold()
    manual = source.startswith("manual") or (
        field == "__listing__"
        and _truth(row.get("manual_reviewed"))
        and bool(_clean(row.get("manual_review_note")))
    )
    rule_value = _clean(row.get(f"{field}_rule"))
    current = _clean(original)
    structured_source = "website" in source or "structured" in source
    if structured_source and current:
        status = "already_resolved"
        reason = "structured_website_value_preserved"
        evidence_source = "website_structured_field"
        confidence = 1.0
        reasoning_summary = "The current value comes directly from a structured website field."
    elif issue.reason_code == "conflicting_deterministic_source" and manual:
        reason = "manual_and_deterministic_sources_conflict"
        evidence_source = "source_conflict"
        reasoning_summary = (
            "A manual correction conflicts with deterministic evidence and requires review."
        )
    elif issue.reason_code == "conflicting_deterministic_source":
        selected = row.get(f"{field}_rule")
        apply_updates[field] = selected
        apply_updates[f"{field}_source"] = "deterministic_rule"
        status = "auto_resolved"
        reason = "deterministic_rule_overrode_lower_priority_value"
        evidence_source = "deterministic_parser"
        confidence = 1.0
        evidence.update({"rule_field": f"{field}_rule", "rule_value": selected})
        reasoning_summary = "A deterministic parser result outranks inference."
    elif rule_value and rule_value.casefold() not in RULE_SENTINELS:
        selected = row.get(f"{field}_rule")
        if current.casefold() == rule_value.casefold():
            status = "already_resolved"
            reason = "current_value_matches_deterministic_rule"
        else:
            status = "auto_resolved"
            reason = "deterministic_rule_selected"
            apply_updates[field] = selected
            apply_updates[f"{field}_source"] = "deterministic_rule"
        evidence_source = "deterministic_parser"
        confidence = 1.0
        evidence.update({"rule_field": f"{field}_rule", "rule_value": selected})
        reasoning_summary = "The deterministic parser produced one unambiguous value."
    elif field == "price_monthly":
        numeric = _number(row.get("price_numeric"))
        raw_numeric = _clean(row.get("price_numeric"))
        period = normalize_price_period(row.get("price_period"))
        inferred_period = period or _infer_price_period(row)
        price_disposition = _clean(row.get("price_review_disposition")).casefold()
        if price_disposition in {
            "period_ambiguous",
            "genuinely_missing",
            "non_monthly_convertible",
            "accepted_unknown",
        }:
            status = "accepted_as_unknown"
            selected = None
            reason = f"human_{price_disposition}"
            evidence_source = "human_review"
            confidence = 1.0
            evidence["price_classification"] = price_disposition
            reasoning_summary = "A reviewer explicitly accepted the missing monthly price."
        elif raw_numeric and numeric is None:
            reason = "invalid_price"
            evidence["price_classification"] = "invalid_price"
        elif numeric is not None and numeric <= 0:
            reason = "invalid_price"
            evidence["price_classification"] = "invalid_price"
        elif numeric is not None and inferred_period == "ambiguous":
            reason = "conflicting_price_period_evidence"
            evidence_source = "listing_description"
            evidence["price_classification"] = "human_review_required"
            reasoning_summary = (
                "The listing contains conflicting rent periods and requires review."
            )
        elif numeric is not None and inferred_period in {
            "month",
            "month_per_bedroom",
            "week",
            "day",
        }:
            selected = calculate_monthly_price(numeric, inferred_period)
            status = "auto_resolved"
            reason = "parser_failure"
            evidence_source = "website_price_and_deterministic_parser"
            confidence = 1.0
            apply_updates["price_monthly"] = selected
            if not period:
                apply_updates["price_period"] = inferred_period
            evidence.update(
                {
                    "price_numeric": numeric,
                    "price_period": inferred_period,
                    "price_text": _clean(row.get("price_text")),
                    "price_classification": "parser_failure",
                }
            )
            reasoning_summary = "The existing conversion rule produced one monthly value."
        elif numeric is not None and not inferred_period:
            status = "accepted_as_unknown"
            reason = "period_ambiguous"
            evidence_source = "website_price"
            confidence = 1.0
            selected = None
            evidence["price_text"] = _clean(row.get("price_text"))
            evidence["price_classification"] = "period_ambiguous"
            reasoning_summary = (
                "The amount is preserved, but no rent period can be inferred safely."
            )
        elif numeric is not None:
            status = "accepted_as_unknown"
            reason = "non_monthly_convertible"
            evidence_source = "website_price"
            confidence = 1.0
            selected = None
            evidence["price_classification"] = "non_monthly_convertible"
            reasoning_summary = "The explicit period has no approved monthly conversion."
        else:
            status = "accepted_as_unknown"
            reason = "genuinely_missing"
            evidence_source = "website_price"
            confidence = 1.0
            selected = None
            evidence["price_classification"] = "genuinely_missing"
            reasoning_summary = "No price amount exists and none can be inferred safely."
    elif field == "map_ready":
        geocode_disposition = _clean(row.get("geocode_review_disposition")).casefold()
        if geocode_disposition in {"coordinates_unknown", "address_unknown"}:
            selected = None
            status = "accepted_as_unknown"
            reason = f"human_{geocode_disposition}"
            evidence_source = "human_review"
            confidence = 1.0
            evidence["geocode_review_disposition"] = geocode_disposition
            reasoning_summary = "A reviewer explicitly accepted missing geocode evidence."
        else:
            verified, geo_evidence, geo_reason = _geocode_resolution(
                row, cache_path=cache_path, config=config
            )
            evidence.update(geo_evidence)
            reason = geo_reason
            evidence_source = "geoapify_cache"
            if verified:
                selected = True
                status = "already_resolved" if _truth(original) else "auto_resolved"
                confidence = 1.0
                reasoning_summary = "The current address and exact cached property geocode agree."
            else:
                reasoning_summary = "Geocode evidence does not meet deterministic acceptance rules."
    else:
        description = _clean(row.get("description"))
        text_value, supporting_text, conflicting_text = _explicit_text_value(
            field, description
        )
        if text_value is not None and not conflicting_text and manual:
            if _clean(original).casefold() == _clean(text_value).casefold():
                status = "already_resolved"
                reason = "manual_correction_confirmed_by_explicit_text"
                confidence = 1.0
                reasoning_summary = (
                    "The recorded manual value agrees with explicit listing text."
                )
            else:
                proposed_value = text_value
                reason = "manual_and_explicit_text_conflict"
                reasoning_summary = (
                    "Explicit listing text conflicts with the recorded manual value."
                )
            evidence_source = "manual_correction_and_listing_description"
            evidence["manual_review_note"] = _clean(row.get("manual_review_note"))
            evidence["supporting_text"] = supporting_text
        elif text_value is not None and not conflicting_text:
            selected = text_value
            status = "already_resolved" if _clean(original).casefold() == _clean(text_value).casefold() else "auto_resolved"
            reason = "explicit_listing_text_evidence"
            evidence_source = "listing_description"
            confidence = 1.0
            apply_updates[field] = text_value
            apply_updates[f"{field}_source"] = "explicit_text"
            evidence["supporting_text"] = supporting_text
            reasoning_summary = "An explicit unambiguous phrase determines the value."
        elif supporting_text or conflicting_text:
            reason = "conflicting_explicit_text"
            evidence_source = "listing_description"
            evidence.update(
                {"supporting_text": supporting_text, "conflicting_text": conflicting_text}
            )
        elif manual:
            status = "already_resolved"
            reason = "current_manual_correction_preserved"
            evidence_source = "manual_correction"
            confidence = 1.0
            evidence["manual_review_note"] = _clean(row.get("manual_review_note"))
            reasoning_summary = "A current field-specific manual correction is preserved."
        elif not current or current.casefold() == "unknown":
            ai_value = row.get(f"ai_{field}")
            if _clean(ai_value):
                proposed_value = ai_value
                reason = "ai_only_proposal_requires_human"
                evidence_source = "ai_inference"
                reasoning_summary = "Only an AI-inferred value is available."
            else:
                status = "accepted_as_unknown"
                selected = None
                reason = "legitimately_unknown"
                evidence_source = "missing_evidence"
                confidence = 1.0
                reasoning_summary = "No reliable evidence determines this optional value."

    reviewer_type = "deterministic_rule"
    if status in {"human_review_required", "accepted_as_unknown"} and proposer is not None:
        proposal = proposer.propose(row, field=field, reason_code=reason)
        if proposal:
            status = "human_review_required"
            proposed_value = proposal.get("proposed_value")
            supporting_text = list(proposal.get("supporting_text") or [])
            conflicting_text = list(proposal.get("conflicting_text") or [])
            reasoning_summary = _clean(proposal.get("reasoning_summary"))
            reviewer_type = "codex_assisted"
            evidence_source = "codex_assisted_proposal"

    fingerprint = listing_fingerprint(row)
    decision = {
        "decision_id": _decision_id(
            run_id,
            issue.listing_id,
            field,
            issue.reason_code,
            reason,
            fingerprint,
        ),
        "run_id": run_id,
        "listing_id": issue.listing_id,
        "field": field,
        "original_value": original,
        "selected_value": selected,
        "decision_status": status,
        "reason_code": reason,
        "evidence": evidence,
        "evidence_source": evidence_source,
        "confidence": confidence,
        "reviewer_type": reviewer_type,
        "created_at_utc": created_at,
        "input_fingerprint": fingerprint,
        "review_categories": sorted(issue.categories),
        "issue_code": issue.reason_code,
        "review_flag": issue.review_flag,
        "apply_updates": apply_updates,
        "proposed_value": proposed_value,
        "supporting_text": supporting_text,
        "conflicting_text": conflicting_text,
        "reasoning_summary": reasoning_summary,
        "human_approved": False,
        "human_reviewer": None,
        "human_approved_at_utc": None,
    }
    missing = [field_name for field_name in REQUIRED_DECISION_FIELDS if field_name not in decision]
    if missing or status not in DECISION_STATUSES:
        raise AssertionError(f"Generated invalid review decision: {missing}")
    return decision


def _manifest_cache_path(run_dir: Path, manifest: dict[str, Any]) -> Optional[Path]:
    configuration = manifest.get("configuration", {})
    stage3 = configuration.get("stage3", {}) if isinstance(configuration, dict) else {}
    configured = stage3.get("cache_path") if isinstance(stage3, dict) else None
    if configured:
        candidate = Path(str(configured))
        if candidate.is_file():
            return candidate
    repository_candidate = run_dir.parents[2] / "processed" / "geocode_cache.csv"
    return repository_candidate if repository_candidate.is_file() else None


def _decision_rows(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    columns = list(REQUIRED_DECISION_FIELDS) + [
        "issue_code",
        "review_categories",
        "review_flag",
        "proposed_value",
        "supporting_text",
        "conflicting_text",
        "reasoning_summary",
    ]
    rows: list[dict[str, Any]] = []
    for decision in decisions:
        row = {}
        for column in columns:
            value = decision.get(column)
            row[column] = (
                _canonical_json(value)
                if isinstance(value, (dict, list, tuple))
                else value
            )
        rows.append(row)
    return rows


def _remaining_rows(
    decisions: list[dict[str, Any]], rows_by_id: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        if decision["decision_status"] != "human_review_required":
            continue
        listing_id = decision["listing_id"]
        row = rows_by_id[listing_id]
        item = grouped.setdefault(
            listing_id,
            {
                "listing_id": listing_id,
                "source_url": row.get("listing_url") or row.get("source_url"),
                "review_category": [],
                "field": [],
                "current_value": {},
                "proposed_value": {},
                "reason": [],
                "evidence": {},
                "original_description": row.get("description"),
                "address": row.get("address"),
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
            },
        )
        item["review_category"].extend(decision["review_categories"])
        item["field"].append(decision["field"])
        item["current_value"][decision["field"]] = decision["original_value"]
        item["proposed_value"][decision["field"]] = decision.get("proposed_value")
        item["reason"].append(decision["reason_code"])
        item["evidence"][decision["field"]] = decision["evidence"]
    output: list[dict[str, Any]] = []
    for listing_id in sorted(grouped):
        item = grouped[listing_id]
        item["review_category"] = "|".join(sorted(set(item["review_category"])))
        item["field"] = "|".join(sorted(set(item["field"])))
        item["reason"] = "|".join(sorted(set(item["reason"])))
        for field in ("current_value", "proposed_value", "evidence"):
            item[field] = _canonical_json(item[field])
        output.append(item)
    return output


def _application_registry(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    workflow = manifest.get("review_workflow", {})
    registry = workflow.get("applied_decisions", {}) if isinstance(workflow, dict) else {}
    return registry if isinstance(registry, dict) else {}


def run_automated_review(
    run_dir: Path,
    *,
    config: Optional[ReviewConfig] = None,
    cache_path: Optional[Path] = None,
    proposer: Optional[ReviewProposer] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Analyze current canonical rows and atomically write review-only outputs."""

    root = run_dir.resolve()
    context = RunContext.resume(root)
    manifest = context.manifest
    run_id = _clean(manifest.get("run_id"))
    if not run_id or run_id != root.name:
        raise ReviewWorkflowError("Manifest run_id does not match the selected directory")
    fields, rows = _read_csv(context.paths.stage3_canonical)
    del fields
    rows_by_id = _index(rows, artifact="stage3/canonical.csv")
    selected_config = config or ReviewConfig()
    selected_cache = cache_path or _manifest_cache_path(root, manifest)
    review_dir = root / "review"
    summary_path = review_dir / "review-auto-summary.json"
    previous_summary: dict[str, Any] = {}
    if summary_path.is_file():
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
            previous_summary = loaded if isinstance(loaded, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            previous_summary = {}
    canonical_sha = sha256_file(context.paths.stage3_canonical)
    if now is not None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ReviewWorkflowError("Review timestamp must be timezone-aware")
        created_at = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    elif previous_summary.get("canonical_sha256") == canonical_sha:
        created_at = str(previous_summary.get("generated_at_utc") or utc_text())
    else:
        created_at = utc_text()

    current: list[dict[str, Any]] = []
    for row in rows:
        for issue in _discover_issues(row):
            current.append(
                _make_decision(
                    run_id,
                    row,
                    issue,
                    created_at=created_at,
                    cache_path=selected_cache,
                    config=selected_config,
                    proposer=proposer,
                )
            )
    current.sort(key=lambda item: (item["listing_id"], item["field"], item["reason_code"]))

    decisions_path = review_dir / "review-decisions.jsonl"
    historical = _read_jsonl(decisions_path)
    records_by_id = {
        str(record.get("decision_id")): record
        for record in historical
        if _clean(record.get("decision_id"))
    }
    registry = _application_registry(manifest)
    for decision in current:
        applied = registry.get(decision["decision_id"])
        if isinstance(applied, dict) and applied.get("input_fingerprint") == decision["input_fingerprint"]:
            decision["application_status"] = "applied"
            decision["applied_at_utc"] = applied.get("applied_at_utc")
        else:
            decision["application_status"] = "pending"
            decision["applied_at_utc"] = None
        records_by_id[decision["decision_id"]] = decision
    all_records = sorted(
        records_by_id.values(),
        key=lambda item: (
            str(item.get("created_at_utc", "")),
            str(item.get("listing_id", "")),
            str(item.get("field", "")),
            str(item.get("decision_id", "")),
        ),
    )

    counts = {status: 0 for status in sorted(DECISION_STATUSES)}
    category_listing_ids: dict[str, set[str]] = {}
    pending_safe = 0
    for decision in current:
        counts[decision["decision_status"]] += 1
        for category in decision["review_categories"]:
            category_listing_ids.setdefault(category, set()).add(
                decision["listing_id"]
            )
        if (
            decision["decision_status"] in SAFE_APPLICATION_STATUSES
            and decision["application_status"] != "applied"
        ):
            pending_safe += 1

    evaluation = evaluate_run_approval(root)
    current_metrics = derive_current_metrics(root, manifest)
    decision_fingerprints_current = all(
        decision["input_fingerprint"] == listing_fingerprint(rows_by_id[decision["listing_id"]])
        for decision in current
    )
    blockers: list[str] = []
    if evaluation.blocking_conditions:
        blockers.append("canonical import eligibility checks have blocking conditions")
    if current_metrics.get("metric_discrepancies"):
        blockers.append("current pipeline metrics disagree")
    if counts["human_review_required"]:
        blockers.append("human review decisions remain")
    if pending_safe:
        blockers.append("safe review decisions have not been applied")
    if not decision_fingerprints_current:
        blockers.append("review decision fingerprints are stale")
    active_errors = manifest.get("errors", [])
    if not isinstance(active_errors, list) or active_errors:
        blockers.append("active fatal errors remain")

    summary = {
        "version": 1,
        "run_id": run_id,
        "generated_at_utc": created_at,
        "canonical_sha256": canonical_sha,
        "manifest_sha256": sha256_file(context.paths.manifest),
        "configuration": asdict(selected_config),
        "cache_evidence_available": bool(selected_cache and selected_cache.is_file()),
        "decision_counts": counts,
        "review_category_counts": {
            category: len(listing_ids)
            for category, listing_ids in sorted(category_listing_ids.items())
        },
        "current_decision_count": len(current),
        "historical_decision_count": len(all_records) - len(current),
        "pending_safe_application_count": pending_safe,
        "remaining_human_listing_count": len(
            {item["listing_id"] for item in current if item["decision_status"] == "human_review_required"}
        ),
        "accepted_unknown_count": counts["accepted_as_unknown"],
        "decision_fingerprints_current": decision_fingerprints_current,
        "metric_discrepancies": current_metrics.get("metric_discrepancies", []),
        "approval_blocking_conditions": list(evaluation.blocking_conditions),
        "accepted_unknowns_documented": all(
            decision.get("evidence_source") and decision.get("reasoning_summary")
            for decision in current
            if decision["decision_status"] == "accepted_as_unknown"
        ),
        "ready_for_approval": not blockers,
        "ready_for_approval_blockers": blockers,
        "external_services_used": False,
    }

    remaining = _remaining_rows(current, rows_by_id)
    decision_columns = list(REQUIRED_DECISION_FIELDS) + [
        "issue_code",
        "review_categories",
        "review_flag",
        "proposed_value",
        "supporting_text",
        "conflicting_text",
        "reasoning_summary",
    ]
    remaining_columns = [
        "listing_id",
        "source_url",
        "review_category",
        "field",
        "current_value",
        "proposed_value",
        "reason",
        "evidence",
        "original_description",
        "address",
        "latitude",
        "longitude",
    ]
    review_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(decisions_path, all_records)
    _write_csv(review_dir / "remaining-human-review.csv", remaining_columns, remaining)
    automatic = [
        item for item in current if item["decision_status"] in {"auto_resolved", "already_resolved"}
    ]
    accepted = [
        item for item in current if item["decision_status"] == "accepted_as_unknown"
    ]
    _write_csv(review_dir / "auto-resolved.csv", decision_columns, _decision_rows(automatic))
    _write_csv(review_dir / "accepted-unknown.csv", decision_columns, _decision_rows(accepted))
    atomic_write_json(summary_path, summary)
    _update_review_index(root, summary)
    return summary


def _update_review_index(run_dir: Path, summary: dict[str, Any]) -> None:
    path = run_dir / "review-index.json"
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
    payload["automated_review"] = {
        "summary_path": "review/review-auto-summary.json",
        "decision_counts": summary["decision_counts"],
        "review_category_counts": summary["review_category_counts"],
        "remaining_human_listing_count": summary["remaining_human_listing_count"],
        "pending_safe_application_count": summary["pending_safe_application_count"],
        "ready_for_approval": summary["ready_for_approval"],
        "validation_warnings": summary["ready_for_approval_blockers"],
    }
    atomic_write_json(path, payload)


def review_status(run_dir: Path) -> dict[str, Any]:
    """Return current review readiness without changing run artifacts."""

    root = run_dir.resolve()
    context = RunContext.resume(root)
    summary_path = root / "review" / "review-auto-summary.json"
    if not summary_path.is_file():
        return {
            "ok": True,
            "run_id": context.manifest.get("run_id"),
            "ready_for_approval": False,
            "ready_for_approval_blockers": ["automated review has not been generated"],
            "external_services_used": False,
        }
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewWorkflowError("Review summary is invalid") from exc
    if not isinstance(summary, dict):
        raise ReviewWorkflowError("Review summary is invalid")
    blockers = list(summary.get("ready_for_approval_blockers", []))
    canonical_hash = sha256_file(context.paths.stage3_canonical)
    decisions = _read_jsonl(root / "review" / "review-decisions.jsonl")
    current_rows = _index(
        _read_csv(context.paths.stage3_canonical)[1], artifact="stage3/canonical.csv"
    )
    if summary.get("canonical_sha256") != canonical_hash:
        blockers.append("review summary canonical fingerprint is stale")
    current_records = [
        item
        for item in decisions
        if item.get("run_id") == context.manifest.get("run_id")
        and item.get("listing_id") in current_rows
        and item.get("input_fingerprint")
        == listing_fingerprint(current_rows[str(item.get("listing_id"))])
    ]
    if summary.get("current_decision_count") != len(current_records):
        blockers.append("review decision set does not match current fingerprints")
    result = copy.deepcopy(summary)
    result["ok"] = True
    result["ready_for_approval_blockers"] = list(dict.fromkeys(blockers))
    result["ready_for_approval"] = not result["ready_for_approval_blockers"]
    return result


def _serialize_csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (dict, list, tuple)):
        return _canonical_json(value)
    return value


def _snapshot(paths: Iterable[Path]) -> dict[Path, Optional[bytes]]:
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


def _restore(snapshot: dict[Path, Optional[bytes]]) -> None:
    for path, content in snapshot.items():
        if content is None:
            if path.is_file():
                path.unlink()
        else:
            _atomic_bytes(path, content)


def _invalidate_approval(manifest: dict[str, Any], *, at_utc: str) -> None:
    approval = manifest.get("approval")
    if isinstance(approval, dict) and (
        approval.get("status") == "approved" or manifest.get("canonical_for_import") is True
    ):
        history = copy.deepcopy(approval.get("history", []))
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "event": "unapproved",
                "at_utc": at_utc,
                "by": "automated_review_workflow",
                "reason": "Review decisions changed canonical evidence",
                "previous_status": approval.get("status"),
                "manifest_fingerprint": approval.get("manifest_fingerprint"),
                "canonical_csv_fingerprint": approval.get("canonical_csv_fingerprint"),
            }
        )
        approval = copy.deepcopy(approval)
        approval.update(
            {
                "status": "unapproved",
                "unapproved_at_utc": at_utc,
                "unapproved_by": "automated_review_workflow",
                "reason": "Review decisions changed canonical evidence",
                "history": history,
            }
        )
        manifest["approval"] = approval
    manifest["canonical_for_import"] = False


def _clear_resolved_geocode_warnings(run_dir: Path, *, at_utc: str) -> None:
    manifest_path = run_dir / "manifest.json"
    with manifest_lock(manifest_path):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stage = manifest.get("stages", {}).get("stage3_qc", {})
        metrics = stage.get("metrics", {}) if isinstance(stage, dict) else {}
        if not isinstance(metrics, dict) or metrics.get("review_required_count") != 0:
            return
        warnings = manifest.get("warnings", [])
        if not isinstance(warnings, list):
            warnings = []
        resolved = [
            warning
            for warning in warnings
            if isinstance(warning, dict)
            and warning.get("stage") == "stage3_qc"
            and "review" in _clean(warning.get("message")).casefold()
        ]
        if resolved:
            workflow = manifest.setdefault("review_workflow", {})
            workflow.setdefault("resolved_warnings", []).append(
                {"at_utc": at_utc, "warnings": resolved}
            )
            manifest["warnings"] = [warning for warning in warnings if warning not in resolved]
        remaining = [
            warning
            for warning in manifest.get("warnings", [])
            if isinstance(warning, dict) and warning.get("stage") == "stage3_qc"
        ]
        if (
            isinstance(stage, dict)
            and stage.get("status") == "completed_with_warnings"
            and not remaining
        ):
            stage["status"] = "completed"
            stage["warning_count"] = 0
        context_paths = RunContext.resume(run_dir).paths
        context = RunContext(context_paths, manifest)
        status, completion_warnings = determine_run_completion(context)
        manifest["status"] = status
        manifest["completion_warnings"] = completion_warnings
        manifest["updated_at_utc"] = at_utc
        atomic_write_json(context_paths.manifest, manifest)


def apply_review_decisions(
    run_dir: Path,
    *,
    now: Optional[datetime] = None,
    config: Optional[ReviewConfig] = None,
    cache_path: Optional[Path] = None,
    _depth: int = 0,
) -> dict[str, Any]:
    """Apply current safe/approved decisions, rebuild canonical, and re-review."""

    root = run_dir.resolve()
    context = RunContext.resume(root)
    decisions_path = root / "review" / "review-decisions.jsonl"
    summary_path = root / "review" / "review-auto-summary.json"
    if not summary_path.is_file():
        raise ReviewWorkflowError("Review summary is missing; run review-auto first")
    try:
        generated_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewWorkflowError("Review summary is invalid") from exc
    if not isinstance(generated_summary, dict):
        raise ReviewWorkflowError("Review summary is invalid")
    if generated_summary.get("canonical_sha256") != sha256_file(
        context.paths.stage3_canonical
    ):
        raise ReviewWorkflowError(
            "Review summary canonical fingerprint is stale; rerun review-auto"
        )
    decisions = _read_jsonl(decisions_path)
    if not decisions:
        raise ReviewWorkflowError("No review decisions exist; run review-auto first")
    canonical_rows = _read_csv(context.paths.stage3_canonical)[1]
    canonical_by_id = _index(canonical_rows, artifact="stage3/canonical.csv")
    reviewed_fields, reviewed_rows = _read_csv(context.paths.stage2_reviewed)
    reviewed_by_id = _index(reviewed_rows, artifact="stage2/reviewed.csv")
    geocoded_fields, geocoded_rows = _read_csv(context.paths.stage3_geocoded)
    geocoded_by_id = _index(geocoded_rows, artifact="stage3/geocoded.csv")
    if set(canonical_by_id) != set(reviewed_by_id) or set(canonical_by_id) != set(geocoded_by_id):
        raise ReviewWorkflowError("Canonical, reviewed, and geocoded listing IDs do not match")

    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    registry = _application_registry(context.manifest)
    already_applied = 0
    for decision in decisions:
        missing = [field for field in REQUIRED_DECISION_FIELDS if field not in decision]
        if missing:
            raise ReviewWorkflowError(f"Decision lacks required fields: {missing}")
        decision_id = _clean(decision.get("decision_id"))
        if decision_id in seen_ids:
            raise ReviewWorkflowError(f"Duplicate decision_id: {decision_id}")
        seen_ids.add(decision_id)
        listing_id = _clean(decision.get("listing_id"))
        row = canonical_by_id.get(listing_id)
        if row is None:
            continue
        if decision.get("run_id") != context.manifest.get("run_id"):
            raise ReviewWorkflowError("Decision run_id does not match selected run")
        if decision.get("input_fingerprint") != listing_fingerprint(row):
            continue
        applied = registry.get(decision_id)
        if (
            isinstance(applied, dict)
            and applied.get("input_fingerprint") == decision.get("input_fingerprint")
        ):
            already_applied += 1
            continue
        status = decision.get("decision_status")
        if (
            decision.get("reviewer_type") == "human"
            and status in SAFE_APPLICATION_STATUSES
            and not _clean(
                decision.get("human_reviewer") or decision.get("reviewer_name")
            )
        ):
            raise ReviewWorkflowError("Human decisions require a reviewer name")
        if status in SAFE_APPLICATION_STATUSES:
            selected.append(decision)
        elif status == "human_review_required" and decision.get("human_approved") is True:
            if not _clean(decision.get("human_reviewer")):
                raise ReviewWorkflowError("Human-approved decisions require human_reviewer")
            selected.append(decision)
        elif status == "excluded" and decision.get("human_approved") is True:
            raise ReviewWorkflowError("Excluded decisions require a separate exclusion policy")
    if not selected and (already_applied or registry):
        current_status = review_status(root)
        return {
            "ok": True,
            "run_id": root.name,
            "idempotent": True,
            "applied_decision_count": 0,
            "applied_decision_ids": [],
            "review_summary": current_status,
            "ready_for_approval": current_status["ready_for_approval"],
            "ready_for_approval_blockers": current_status[
                "ready_for_approval_blockers"
            ],
            "approval_invalidated": context.manifest.get("canonical_for_import")
            is not True,
            "external_services_used": False,
        }
    if not selected:
        raise ReviewWorkflowError("No current safe or explicitly human-approved decisions")

    at = now or datetime.now(timezone.utc)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ReviewWorkflowError("Application timestamp must be timezone-aware")
    at_utc = at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    new_reviewed = copy.deepcopy(reviewed_rows)
    new_geocoded = copy.deepcopy(geocoded_rows)
    reviewed_new_by_id = {row["listing_id"]: row for row in new_reviewed}
    geocoded_new_by_id = {row["listing_id"]: row for row in new_geocoded}
    applied_records: list[dict[str, Any]] = []
    flag_touched_ids: set[str] = set()
    for decision in selected:
        listing_id = str(decision["listing_id"])
        field = str(decision["field"])
        if field in IMMUTABLE_FIELDS:
            raise ReviewWorkflowError(f"Decision cannot modify identity field {field}")
        updates = decision.get("apply_updates")
        if not isinstance(updates, dict):
            updates = {}
        if (
            decision.get("human_approved") is True
            and field != "__listing__"
            and field != "map_ready"
            and field not in updates
        ):
            updates[field] = decision.get("selected_value", decision.get("proposed_value"))
        if (
            decision.get("human_approved") is True
            and field != "__listing__"
            and field not in GEOCODE_FIELDS
            and field != "map_ready"
        ):
            updates[f"{field}_source"] = "manual_review"
        for update_field, value in updates.items():
            if update_field in IMMUTABLE_FIELDS:
                raise ReviewWorkflowError(
                    f"Decision cannot modify identity field {update_field}"
                )
            target = (
                geocoded_new_by_id[listing_id]
                if update_field in GEOCODE_FIELDS
                else reviewed_new_by_id[listing_id]
            )
            target[update_field] = _serialize_csv_value(value)
            if target is reviewed_new_by_id[listing_id] and update_field not in reviewed_fields:
                reviewed_fields.append(update_field)
            if target is geocoded_new_by_id[listing_id] and update_field not in geocoded_fields:
                geocoded_fields.append(update_field)
        if decision.get("human_approved") is True:
            reviewed_row = reviewed_new_by_id[listing_id]
            reviewer = _clean(decision.get("human_reviewer"))
            note = f"Review decision {decision['decision_id']} approved by {reviewer}."
            prior_note = _clean(reviewed_row.get("manual_review_note"))
            if note not in prior_note:
                reviewed_row["manual_review_note"] = (
                    f"{prior_note} {note}".strip() if prior_note else note
                )
            reviewed_row["manual_reviewed"] = "True"
            for provenance_field in ("manual_review_note", "manual_reviewed"):
                if provenance_field not in reviewed_fields:
                    reviewed_fields.append(provenance_field)
        flag = _clean(decision.get("review_flag"))
        if flag:
            row = reviewed_new_by_id[listing_id]
            remaining_flags = [item for item in _parse_flags(row.get("review_flags")) if item != flag]
            row["review_flags"] = _canonical_json(remaining_flags)
            flag_touched_ids.add(listing_id)
        applied_records.append(
            {
                "decision_id": decision["decision_id"],
                "input_fingerprint": decision["input_fingerprint"],
                "decision_status": decision["decision_status"],
                "applied_at_utc": at_utc,
                "human_reviewer": decision.get("human_reviewer"),
            }
        )

    for listing_id in flag_touched_ids:
        row = reviewed_new_by_id[listing_id]
        row["needs_manual_review"] = (
            "True" if _parse_flags(row.get("review_flags")) else "False"
        )
    if "needs_manual_review" not in reviewed_fields:
        reviewed_fields.append("needs_manual_review")
    queue_fields, _ = _read_csv(context.paths.stage2_review_queue)
    new_queue = [
        {field: row.get(field, "") for field in queue_fields}
        for row in new_reviewed
        if _truth(row.get("needs_manual_review"))
    ]

    review_dir = root / "review"
    protected = [
        context.paths.stage2_reviewed,
        context.paths.stage2_review_queue,
        context.paths.stage3_geocoded,
        context.paths.stage3_canonical,
        context.paths.stage3_geocode_review,
        context.paths.manifest,
        root / "review-index.json",
        root / "operator-summary.json",
        *(review_dir / name for name in REVIEW_OUTPUTS),
    ]
    before = _snapshot(protected)
    try:
        _write_csv(context.paths.stage2_reviewed, reviewed_fields, new_reviewed)
        _write_csv(context.paths.stage2_review_queue, queue_fields, new_queue)
        _write_csv(context.paths.stage3_geocoded, geocoded_fields, new_geocoded)
        with manifest_lock(context.paths.manifest):
            manifest = json.loads(context.paths.manifest.read_text(encoding="utf-8"))
            workflow = manifest.setdefault("review_workflow", {})
            registry = workflow.setdefault("applied_decisions", {})
            for record in applied_records:
                registry[record["decision_id"]] = record
            applications = workflow.setdefault("application_history", [])
            application_fingerprint = hashlib.sha256(
                _canonical_json(applied_records).encode("utf-8")
            ).hexdigest()
            if not applications or applications[-1].get("application_fingerprint") != application_fingerprint:
                applications.append(
                    {
                        "at_utc": at_utc,
                        "application_fingerprint": application_fingerprint,
                        "decision_ids": sorted(record["decision_id"] for record in applied_records),
                        "reviewed_sha256": sha256_file(context.paths.stage2_reviewed),
                        "geocoded_sha256": sha256_file(context.paths.stage3_geocoded),
                    }
                )
            stages = manifest.get("stages", {})
            if isinstance(stages, dict):
                stage2 = stages.get("stage2", {})
                manual = stages.get("manual_fixes", {})
                if isinstance(stage2, dict):
                    stage2.setdefault("metrics", {})["review_count"] = len(new_queue)
                if isinstance(manual, dict):
                    manual.setdefault("metrics", {})["review_count"] = len(new_queue)
                    manual["metrics"]["manual_review_fully_resolved"] = not new_queue
            if not new_queue:
                active_warnings = manifest.get("warnings", [])
                if isinstance(active_warnings, list):
                    resolved_warnings = [
                        warning
                        for warning in active_warnings
                        if isinstance(warning, dict)
                        and warning.get("stage") in {"stage2", "manual_fixes"}
                        and "review" in _clean(warning.get("message")).casefold()
                    ]
                    if resolved_warnings:
                        workflow.setdefault("resolved_warnings", []).append(
                            {"at_utc": at_utc, "warnings": resolved_warnings}
                        )
                        manifest["warnings"] = [
                            warning
                            for warning in active_warnings
                            if warning not in resolved_warnings
                        ]
                    for stage_name in ("stage2", "manual_fixes"):
                        stage = stages.get(stage_name, {}) if isinstance(stages, dict) else {}
                        stage_warnings = [
                            warning
                            for warning in manifest.get("warnings", [])
                            if isinstance(warning, dict)
                            and warning.get("stage") == stage_name
                        ]
                        if (
                            isinstance(stage, dict)
                            and stage.get("status") == "completed_with_warnings"
                            and not stage_warnings
                            and not stage.get("metrics", {}).get("ai_error_count", 0)
                        ):
                            stage["status"] = "completed"
                            stage["warning_count"] = 0
            _invalidate_approval(manifest, at_utc=at_utc)
            manifest["updated_at_utc"] = at_utc
            atomic_write_json(context.paths.manifest, manifest)
        rebuild_result = rebuild_canonical(root)
        _clear_resolved_geocode_warnings(root, at_utc=at_utc)
        review_summary = run_automated_review(
            root, config=config, cache_path=cache_path, now=at
        )
        follow_up: Optional[dict[str, Any]] = None
        if review_summary["pending_safe_application_count"]:
            if _depth >= 4:
                raise ReviewWorkflowError(
                    "Safe review decisions did not converge after fingerprint refresh"
                )
            follow_up = apply_review_decisions(
                root,
                now=at,
                config=config,
                cache_path=cache_path,
                _depth=_depth + 1,
            )
            review_summary = follow_up["review_summary"]
        refreshed_manifest = RunContext.resume(root).manifest
        current_metrics = derive_current_metrics(root, refreshed_manifest)
        approval_summary = asdict(evaluate_run_approval(root).summary)
        history = refreshed_manifest.get("error_history", [])
        operator_summary = {
            "run_id": root.name,
            "run_status": refreshed_manifest.get("status"),
            "ai_errors": current_metrics.get("ai_errors"),
            "ai_review_rows": current_metrics.get("ai_review_rows"),
            "manual_review_rows": current_metrics.get("manual_review_rows"),
            "geocode_failures": current_metrics.get("geocode_failures"),
            "geocode_review_rows": current_metrics.get("geocode_review_rows"),
            "missing_monthly_prices": approval_summary.get(
                "missing_monthly_price_rows"
            ),
            "map_ready_rows": approval_summary.get("map_ready_rows"),
            "not_map_ready_rows": approval_summary.get("not_map_ready_rows"),
            "current_active_errors": len(refreshed_manifest.get("errors", [])),
            "historical_resolved_errors": sum(
                isinstance(item, dict) and bool(item.get("resolved_at_utc"))
                for item in history
            )
            if isinstance(history, list)
            else 0,
            "metric_sources": current_metrics.get("metric_sources", {}),
            "metric_discrepancies": current_metrics.get("metric_discrepancies", []),
            "review_auto_summary": review_summary,
            "recommended_next_action": (
                "ready_for_approval"
                if review_summary["ready_for_approval"]
                else "review_required"
            ),
            "generated_at_utc": at_utc,
        }
        atomic_write_json(root / "operator-summary.json", operator_summary)
    except Exception:
        _restore(before)
        raise
    all_applied_ids = sorted(
        {
            *(record["decision_id"] for record in applied_records),
            *((follow_up or {}).get("applied_decision_ids", [])),
        }
    )
    return {
        "ok": True,
        "run_id": root.name,
        "idempotent": False,
        "applied_decision_count": len(all_applied_ids),
        "applied_decision_ids": all_applied_ids,
        "canonical_rebuild": rebuild_result,
        "review_summary": review_summary,
        "ready_for_approval": review_summary["ready_for_approval"],
        "ready_for_approval_blockers": review_summary[
            "ready_for_approval_blockers"
        ],
        "approval_invalidated": True,
        "external_services_used": False,
    }


def staging_commands(run_id: str) -> list[str]:
    """Return, but never execute, the deliberate post-review staging sequence."""

    selected_run_id = validate_run_id(run_id)
    run_dir = f"data\\runs\\{selected_run_id}"
    return [
        f'python -m pipeline.run_context approval-status --run-dir "{run_dir}"',
        f'python -m pipeline.run_context approve --run-dir "{run_dir}" --approved-by "<reviewer>" --note "Automated and human review completed" --acknowledge-warnings --confirm',
        '.\\.venv\\Scripts\\python.exe -m pytest -m postgres -v',
        f'.\\.venv\\Scripts\\python.exe -m pipeline.database_importer --run-dir "{run_dir}" --database-url $env:TEST_DATABASE_URL --missing-run-threshold 2',
        f'psql "$env:TEST_DATABASE_URL" -v ON_ERROR_STOP=1 -c "select run_id, status, import_status, import_completed_at, change_summary from public.housing_pipeline_runs where run_id = ''{selected_run_id}'';"',
        'Invoke-RestMethod http://127.0.0.1:8000/api/listings?limit=1',
    ]
