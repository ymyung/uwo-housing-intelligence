"""Deterministic, student-safe listing history projections.

The persisted observation hash intentionally tracks a broad canonical record.
This module applies the narrower product contract: a change is student-relevant
only when a normalized housing-opportunity value changed, and it is public only
when stored source text independently supports a source-origin change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Mapping


class ChangeOrigin(str, Enum):
    SOURCE_CHANGE = "SOURCE_CHANGE"
    PIPELINE_REINTERPRETATION = "PIPELINE_REINTERPRETATION"
    UNKNOWN_CHANGE_ORIGIN = "UNKNOWN_CHANGE_ORIGIN"


class MeaningfulChangeType(str, Enum):
    LISTING_ADDED = "LISTING_ADDED"
    PRICE_CHANGED = "PRICE_CHANGED"
    AVAILABILITY_CHANGED = "AVAILABILITY_CHANGED"
    LEASE_CHANGED = "LEASE_CHANGED"
    HOUSING_TYPE_CHANGED = "HOUSING_TYPE_CHANGED"
    UTILITIES_CHANGED = "UTILITIES_CHANGED"
    FURNISHING_CHANGED = "FURNISHING_CHANGED"
    SUBLET_CHANGED = "SUBLET_CHANGED"
    GENDER_PREFERENCE_CHANGED = "GENDER_PREFERENCE_CHANGED"
    ADDRESS_CHANGED = "ADDRESS_CHANGED"
    LISTING_BECAME_INACTIVE = "LISTING_BECAME_INACTIVE"
    LISTING_REACTIVATED = "LISTING_REACTIVATED"


@dataclass(frozen=True)
class MeaningfulChange:
    change_type: MeaningfulChangeType
    origin: ChangeOrigin
    previous_value: Any
    current_value: Any
    changed_fields: tuple[str, ...]

    @property
    def student_visible(self) -> bool:
        return (
            self.origin is ChangeOrigin.SOURCE_CHANGE
            and self.change_type is not MeaningfulChangeType.LISTING_ADDED
        )


SEMANTIC_FIELD_GROUPS: tuple[
    tuple[MeaningfulChangeType, tuple[str, ...]], ...
] = (
    (MeaningfulChangeType.PRICE_CHANGED, ("price_monthly",)),
    (
        MeaningfulChangeType.AVAILABILITY_CHANGED,
        (
            "availability_text",
            "available_now",
            "date_available",
            "available_from",
            "available_to",
            "availability_category",
        ),
    ),
    (
        MeaningfulChangeType.LEASE_CHANGED,
        ("lease_type", "lease_term_months"),
    ),
    (MeaningfulChangeType.HOUSING_TYPE_CHANGED, ("housing_type",)),
    (
        MeaningfulChangeType.UTILITIES_CHANGED,
        ("utilities_included", "utilities_status"),
    ),
    (MeaningfulChangeType.FURNISHING_CHANGED, ("furnished",)),
    (MeaningfulChangeType.SUBLET_CHANGED, ("is_sublet",)),
    (
        MeaningfulChangeType.GENDER_PREFERENCE_CHANGED,
        ("preferred_gender",),
    ),
    (
        MeaningfulChangeType.ADDRESS_CHANGED,
        ("normalized_address", "address", "unit_identifier"),
    ),
)

_NUMERIC_FIELDS = {"price_monthly", "lease_term_months"}
_BOOLEAN_FIELDS = {
    "available_now",
    "utilities_included",
    "furnished",
    "is_sublet",
}
_SOURCE_EVIDENCE_FIELDS = ("title", "description")
_AVAILABILITY_SOURCE_EVIDENCE_FIELDS = (
    "title",
    "description",
    "availability_text",
)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split()).casefold()
    return normalized or None


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().casefold().replace(",", "")
    if text.startswith("cad"):
        text = text.removeprefix("cad").strip()
    text = text.removeprefix("$").strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
        return parsed.normalize() if parsed.is_finite() else None
    except InvalidOperation:
        return None


def _boolean(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _normalized(field: str, value: Any) -> Any:
    if field in _NUMERIC_FIELDS:
        return _number(value)
    if field in _BOOLEAN_FIELDS:
        return _boolean(value)
    return _text(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        integral = value.to_integral_value()
        return int(integral) if value == integral else float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _group_value(data: Mapping[str, Any], fields: tuple[str, ...]) -> Any:
    values = {field: _normalized(field, data.get(field)) for field in fields}
    if len(fields) == 1:
        return values[fields[0]]
    return values


def _display_value(data: Mapping[str, Any], fields: tuple[str, ...]) -> Any:
    values = {field: _json_value(data.get(field)) for field in fields}
    if len(fields) == 1:
        return values[fields[0]]
    return values


def _source_origin(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    change_type: MeaningfulChangeType,
    previous_comparison: Mapping[str, Any],
    current_comparison: Mapping[str, Any],
) -> ChangeOrigin:
    fields = (
        _AVAILABILITY_SOURCE_EVIDENCE_FIELDS
        if change_type is MeaningfulChangeType.AVAILABILITY_CHANGED
        else _SOURCE_EVIDENCE_FIELDS
    )
    pairs = [(_text(previous.get(field)), _text(current.get(field))) for field in fields]
    if not any(before is not None or after is not None for before, after in pairs):
        return ChangeOrigin.UNKNOWN_CHANGE_ORIGIN
    if not any(before != after for before, after in pairs):
        return ChangeOrigin.PIPELINE_REINTERPRETATION
    if change_type is MeaningfulChangeType.AVAILABILITY_CHANGED and (
        _text(previous.get("availability_text"))
        != _text(current.get("availability_text"))
    ):
        return ChangeOrigin.SOURCE_CHANGE
    if change_type is MeaningfulChangeType.PRICE_CHANGED:
        before_price = _number(previous_comparison.get("price_monthly"))
        after_price = _number(current_comparison.get("price_monthly"))
        before_text = " ".join(
            str(previous.get(field) or "") for field in _SOURCE_EVIDENCE_FIELDS
        )
        after_text = " ".join(
            str(current.get(field) or "") for field in _SOURCE_EVIDENCE_FIELDS
        )

        def mentions(text: str, price: Decimal | None) -> bool:
            if price is None:
                return False
            token = format(price, "f")
            if "." in token:
                token = token.rstrip("0").rstrip(".")
            return bool(re.search(rf"(?<!\d){re.escape(token)}(?:\.0+)?(?!\d)", text))

        if mentions(before_text, before_price) and mentions(after_text, after_price):
            return ChangeOrigin.SOURCE_CHANGE
    # Changed prose may coincide with several parser corrections. Without a
    # field-specific structured source value, old history cannot prove which
    # semantic difference the advertiser made.
    return ChangeOrigin.UNKNOWN_CHANGE_ORIGIN


def compare_listing_observations(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> tuple[MeaningfulChange, ...]:
    """Compare two persisted observation projections.

    ``comparison_data`` supplies canonical semantic values. Top-level title,
    description, and availability text are independent source evidence. A
    semantic difference with unchanged source evidence is deliberately treated
    as a pipeline reinterpretation rather than a landlord update.
    """

    current_comparison = current.get("comparison_data")
    if not isinstance(current_comparison, Mapping):
        current_comparison = current
    if previous is None:
        return (
            MeaningfulChange(
                change_type=MeaningfulChangeType.LISTING_ADDED,
                origin=ChangeOrigin.UNKNOWN_CHANGE_ORIGIN,
                previous_value=None,
                current_value=None,
                changed_fields=(),
            ),
        )

    previous_comparison = previous.get("comparison_data")
    if not isinstance(previous_comparison, Mapping):
        previous_comparison = previous
    changes: list[MeaningfulChange] = []
    if str(current.get("change_type") or "").casefold() == "relisted":
        changes.append(
            MeaningfulChange(
                change_type=MeaningfulChangeType.LISTING_REACTIVATED,
                origin=ChangeOrigin.SOURCE_CHANGE,
                previous_value="removed",
                current_value="active",
                changed_fields=(),
            )
        )

    for change_type, fields in SEMANTIC_FIELD_GROUPS:
        before = _group_value(previous_comparison, fields)
        after = _group_value(current_comparison, fields)
        if before == after:
            continue
        changed_fields = tuple(
            field
            for field in fields
            if _normalized(field, previous_comparison.get(field))
            != _normalized(field, current_comparison.get(field))
        )
        changes.append(
            MeaningfulChange(
                change_type=change_type,
                origin=_source_origin(
                    previous,
                    current,
                    change_type,
                    previous_comparison,
                    current_comparison,
                ),
                previous_value=_display_value(previous_comparison, fields),
                current_value=_display_value(current_comparison, fields),
                changed_fields=changed_fields,
            )
        )
    return tuple(changes)


def compare_listing_lifecycle(
    previous_status: str,
    current_status: str,
) -> tuple[MeaningfulChange, ...]:
    """Classify the explicit current-state transitions used by Stage 4."""

    before = previous_status.casefold()
    after = current_status.casefold()
    if before != "removed" and after == "removed":
        change_type = MeaningfulChangeType.LISTING_BECAME_INACTIVE
    elif before == "removed" and after == "relisted":
        change_type = MeaningfulChangeType.LISTING_REACTIVATED
    else:
        return ()
    return (
        MeaningfulChange(
            change_type=change_type,
            origin=ChangeOrigin.SOURCE_CHANGE,
            previous_value=previous_status,
            current_value=current_status,
            changed_fields=(),
        ),
    )


def observation_freshness(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return truthful inventory-observation dates without update claims."""

    return {
        "first_observed_at": _json_value(row.get("first_seen_at")),
        "last_observed_at": _json_value(row.get("last_seen_at")),
    }


def project_listing_history(
    listing: Mapping[str, Any],
    observations: Iterable[Mapping[str, Any]],
    *,
    event_limit: int = 20,
) -> dict[str, Any]:
    """Build a bounded public history containing only proven source changes."""

    if event_limit < 1:
        raise ValueError("event_limit must be positive")

    ordered = sorted(
        (dict(observation) for observation in observations),
        key=lambda row: (str(row.get("observed_at") or ""), int(row.get("id") or 0)),
    )
    source_events: list[dict[str, Any]] = []
    previous: Mapping[str, Any] | None = None
    for observation in ordered:
        for change in compare_listing_observations(previous, observation):
            if not change.student_visible:
                continue
            source_events.append(
                {
                    "type": change.change_type.value,
                    "observed_at": _json_value(observation.get("observed_at")),
                    "previous_value": change.previous_value,
                    "current_value": change.current_value,
                }
            )
        previous = observation

    source_events.reverse()
    visible = source_events[:event_limit]
    freshness = observation_freshness(listing)
    freshness["last_meaningful_source_change_at"] = (
        source_events[0]["observed_at"] if source_events else None
    )
    reported_totals = [
        int(row.get("total_observation_count") or 0) for row in ordered
    ]
    total_observations = max(reported_totals) if any(reported_totals) else len(ordered)
    return {
        "listing_id": str(listing.get("listing_id")),
        "observation_count": total_observations,
        **freshness,
        "events": visible,
        "has_more": total_observations > len(ordered) or len(source_events) > event_limit,
    }
