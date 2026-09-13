"""Stable API projection for persisted, precomputed Ranking v1 rows."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from math import isfinite
from typing import Any, Mapping


COMPONENT_COLUMNS = {
    "value": "ranking_value_score",
    "campus_access": "ranking_campus_access_score",
    "transit": "ranking_transit_score",
    "amenities": "ranking_amenity_score",
    "data_quality": "ranking_data_quality_score",
}
VALUE_SIGNALS = {
    "monthly_price",
    "comparable_level",
    "comparable_key",
    "comparable_count",
    "market_median",
    "market_p25",
    "market_p75",
    "price_delta_percent",
    "price_percentile",
}
CAMPUS_SIGNALS = {
    "walking_minutes",
    "cycling_minutes",
    "walking_score",
    "cycling_score",
}
TRANSIT_PERIOD_SIGNALS = {
    "time_period",
    "score",
    "weight",
    "representative_minutes",
    "walking_share",
    "transfers",
    "available_samples",
    "requested_samples",
    "no_route_samples",
    "walking_better_samples",
    "other_unavailable_samples",
    "quality_status",
    "reason_codes",
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if item not in (None, "")]


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _project_fields(value: Any, allowed: set[str]) -> dict[str, Any] | None:
    source = _mapping(value)
    if not source:
        return None
    return {
        key: _json_safe(source.get(key))
        for key in allowed
        if key in source
    }


def _project_periods(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        projected
        for item in value
        if (projected := _project_fields(item, TRANSIT_PERIOD_SIGNALS)) is not None
    ]


def _student_reasons(signals: Mapping[str, Any]) -> list[dict[str, str]]:
    """Translate persisted Ranking v1 evidence into restrained product copy."""

    reasons: list[dict[str, str]] = []
    value = _mapping(signals.get("value"))
    price_delta = _number(value.get("price_delta_percent"))
    comparable_count = _number(value.get("comparable_count"))
    if price_delta is not None and price_delta < 0 and comparable_count:
        reasons.append(
            {
                "kind": "value",
                "title": "Good value",
                "detail": (
                    f"{abs(price_delta):.0f}% below the comparison median"
                ),
            }
        )

    campus = _mapping(signals.get("campus_access"))
    walking_minutes = _number(campus.get("walking_minutes"))
    if walking_minutes is not None:
        reasons.append(
            {
                "kind": "campus_access",
                "title": "Campus commute",
                "detail": f"{round(walking_minutes)} min walk to Western",
            }
        )

    periods = signals.get("transit_periods")
    if isinstance(periods, list):
        morning = next(
            (
                _mapping(period)
                for period in periods
                if _mapping(period).get("time_period")
                == "weekday_morning_commute"
            ),
            {},
        )
        transit_minutes = _number(morning.get("representative_minutes"))
        if transit_minutes is not None:
            reasons.append(
                {
                    "kind": "transit",
                    "title": "Morning transit",
                    "detail": (
                        f"{round(transit_minutes)} min typical weekday morning trip"
                    ),
                }
            )
    return reasons


def project_persisted_ranking(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the whitelisted Ranking v1 API contract without recalculation."""

    version = str(row.get("ranking_version") or "").strip()
    status = str(row.get("ranking_status") or "").strip()
    if not version or not status:
        return None
    if version != "ranking-v1" or status not in {"ranked", "partial", "excluded"}:
        return None

    explanation = _mapping(row.get("ranking_explanation"))
    components = {
        name: _number(row.get(column))
        for name, column in COMPONENT_COLUMNS.items()
    }

    weights_source = _mapping(explanation.get("component_weights"))
    weights = {
        name: _number(weights_source.get(name))
        for name in ("value", "campus_access", "transit", "amenities")
    }
    signals = _mapping(explanation.get("key_signals"))
    confidence = _mapping(explanation.get("confidence"))
    computed_at = row.get("ranking_computed_at") or explanation.get("computed_at")
    overall_score = _number(row.get("ranking_overall_score"))

    return {
        "version": version,
        "status": status,
        "overall_score": overall_score,
        "components": components,
        "weights": weights,
        "signals": {
            "value": _project_fields(signals.get("value"), VALUE_SIGNALS),
            "campus_access": _project_fields(
                signals.get("campus_access"), CAMPUS_SIGNALS
            ),
            "transit_periods": _project_periods(signals.get("transit_periods")),
        },
        "reasons": _student_reasons(signals),
        "warnings": {
            "accessibility_reason_codes": _strings(
                confidence.get("accessibility_warning_reason_codes")
            ),
            "accessibility_review_required": bool(
                confidence.get("accessibility_review_required", False)
            ),
            "listing_review_flags": _strings(
                confidence.get("listing_review_flags")
            ),
        },
        "eligibility_reasons": _strings(explanation.get("eligibility_reasons")),
        "unavailable_components": _strings(
            explanation.get("system_unavailable_components")
        ),
        "amenity_status": str(
            explanation.get("amenity_status") or "not_implemented"
        ),
        "data_quality_status": str(
            explanation.get("data_quality_component_status") or "not_scored"
        ),
        "computed_at": _json_safe(computed_at),
        "input_fingerprint": str(
            row.get("ranking_input_fingerprint")
            or explanation.get("input_fingerprint")
            or ""
        ).strip()
        or None,
    }
