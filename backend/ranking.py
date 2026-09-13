"""Legacy in-memory ranking prototype retained for compatibility tests.

The housing API does not call this module. Authoritative Ranking v1 scores are
precomputed by ``backend.ranking_v1`` and read through the persistence layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Iterable


@dataclass(frozen=True)
class RankingWeights:
    price: float = 0.30
    distance: float = 0.25
    amenities: float = 0.15
    convenience: float = 0.10
    data_quality: float = 0.10
    freshness: float = 0.10

    def __post_init__(self) -> None:
        values = (
            self.price,
            self.distance,
            self.amenities,
            self.convenience,
            self.data_quality,
            self.freshness,
        )
        if any(value < 0 for value in values) or sum(values) <= 0:
            raise ValueError("ranking weights must be non-negative with a positive total")

    def normalized(self) -> dict[str, float]:
        values = {
            "price": self.price,
            "distance": self.distance,
            "amenities": self.amenities,
            "convenience": self.convenience,
            "data_quality": self.data_quality,
            "freshness": self.freshness,
        }
        total = sum(values.values())
        return {key: value / total for key, value in values.items()}


DEFAULT_RANKING_WEIGHTS = RankingWeights()
SUSPICIOUS_MONTHLY_PRICE_MIN = 300.0
SUSPICIOUS_MONTHLY_PRICE_MAX = 10_000.0


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _truth(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def listing_quality(listing: dict[str, Any]) -> dict[str, Any]:
    flags = listing.get("review_flags") or []
    if isinstance(flags, str):
        flags = [part.strip() for part in flags.strip("[]").split(",") if part.strip()]
    needs_review = _truth(listing.get("needs_manual_review")) is True or bool(flags)
    coordinate_ready = _truth(listing.get("map_ready")) is True
    geocode_confidence = _number(listing.get("geocode_confidence"))
    required = ("listing_id", "listing_url", "price_monthly", "housing_type", "bedrooms")
    known = sum(listing.get(field) not in (None, "") for field in required)
    completeness = known / len(required)
    score = completeness * 70
    if coordinate_ready:
        score += 20
    if geocode_confidence is not None:
        score += min(10, max(0, geocode_confidence * 10))
    if needs_review:
        score = min(score, 55)
    status = "needs-review" if needs_review else "confirmed" if score >= 85 else "parsed"
    return {
        "status": status,
        "score": round(score, 1),
        "needs_review": needs_review,
        "map_ready": coordinate_ready,
        "label": "Needs review" if needs_review else "Verified" if status == "confirmed" else "Parsed",
    }


def field_quality(listing: dict[str, Any], field_name: str) -> dict[str, Any]:
    if listing.get(field_name) in (None, ""):
        return {"status": "unknown", "label": "Information unavailable"}
    provenance = listing.get("provenance_data") or {}
    source = provenance.get(field_name) if isinstance(provenance, dict) else None
    source_text = str(source or listing.get(f"{field_name}_source") or "").lower()
    if "manual" in source_text or "human" in source_text:
        status, label = "human-reviewed", "Verified"
    elif "ai" in source_text:
        status, label = "AI-assisted", "AI-assisted"
    elif "rule" in source_text or "pars" in source_text:
        status, label = "parsed", "Parsed"
    else:
        status, label = "confirmed", "Verified"
    return {"status": status, "label": label}


def _comparison_key(listing: dict[str, Any]) -> tuple[str, int | None]:
    bedrooms = _number(listing.get("bedrooms"))
    return str(listing.get("housing_type") or "unknown").lower(), (
        round(bedrooms) if bedrooms is not None else None
    )


def _price_score(listing: dict[str, Any], comparable: list[dict[str, Any]]) -> float:
    price = _number(listing.get("price_monthly"))
    if price is None:
        return 25.0
    if not SUSPICIOUS_MONTHLY_PRICE_MIN <= price <= SUSPICIOUS_MONTHLY_PRICE_MAX:
        return 0.0
    prices = sorted(
        candidate
        for row in comparable
        if (candidate := _number(row.get("price_monthly"))) is not None
        and SUSPICIOUS_MONTHLY_PRICE_MIN <= candidate <= SUSPICIOUS_MONTHLY_PRICE_MAX
    )
    if len(prices) < 2 or prices[-1] == prices[0]:
        return 60.0
    return max(0.0, min(100.0, 100 * (prices[-1] - price) / (prices[-1] - prices[0])))


def _distance_score(listing: dict[str, Any]) -> float:
    accessibility = listing.get("accessibility")
    accessibility_distance = (
        _number(accessibility.get("distance_meters")) / 1000
        if isinstance(accessibility, dict)
        and _number(accessibility.get("distance_meters")) is not None
        and accessibility.get("result_type")
        not in {"pending_provider", "unavailable", "stale"}
        else None
    )
    distance_km = accessibility_distance
    if distance_km is None:
        distance_km = _number(listing.get("selected_hotspot_distance_km"))
    if distance_km is None:
        distance_km = _number(listing.get("distance_to_western_km"))
    if distance_km is None:
        return 20.0
    return max(0.0, min(100.0, 100 - distance_km * 16))


def _amenity_score(listing: dict[str, Any]) -> float:
    fields = (
        "furnished",
        "utilities_included",
        "parking_available",
        "laundry",
        "dishwasher",
        "air_conditioning",
    )
    states = [_truth(listing.get(field)) for field in fields]
    known = [state for state in states if state is not None]
    if not known:
        return 20.0
    # Unknown values receive no credit, so missing information is never perfect.
    return 100 * sum(state is True for state in states) / len(fields)


def _convenience_score(listing: dict[str, Any]) -> float:
    accessibility = listing.get("accessibility")
    if isinstance(accessibility, dict):
        result_type = accessibility.get("result_type")
        base = {
            "exact_route": 100.0,
            "cached_exact_property": 100.0,
            "cached_exact_origin": 95.0,
            "same_stop_reuse": 82.0,
            "nearby_origin_estimate": 70.0,
            "straight_line_fallback": 50.0,
            "stale": 15.0,
            "pending_provider": 10.0,
            "unavailable": 10.0,
        }.get(result_type, 20.0)
        confidence = _number(accessibility.get("confidence"))
        if confidence is None:
            confidence = 1.0 if result_type in {"exact_route", "cached_exact_property"} else 0.5
        return max(0.0, min(100.0, base * confidence))
    transit = _number(listing.get("transit_score"))
    if transit is not None:
        return max(0.0, min(100.0, transit))
    indicators = (
        _truth(listing.get("has_direct_western_route")),
        listing.get("nearest_stop_name") not in (None, ""),
    )
    if not any(indicator is not None for indicator in indicators):
        return 20.0
    return 50.0 * sum(indicator is True for indicator in indicators)


def _convenience_explanation(listing: dict[str, Any]) -> str:
    accessibility = listing.get("accessibility")
    if not isinstance(accessibility, dict):
        return "Convenience score uses available normalized transit indicators."
    labels = {
        "exact_route": "Convenience uses a current exact accessibility profile.",
        "cached_exact_property": "Convenience uses a current cached property profile.",
        "cached_exact_origin": "Convenience uses a current shared-origin profile.",
        "same_stop_reuse": (
            "Transit convenience is reduced because the result reuses the same "
            "stop with a new walking connection."
        ),
        "nearby_origin_estimate": "Convenience is reduced because the route is estimated from a nearby origin.",
        "straight_line_fallback": "Convenience is reduced because only a straight-line estimate is available.",
        "stale": "Stale accessibility is retained but receives little ranking credit.",
        "pending_provider": "Missing live accessibility does not count as zero travel time.",
        "unavailable": "Unknown accessibility receives conservative ranking credit.",
    }
    return labels.get(
        accessibility.get("result_type"),
        "Unknown accessibility receives conservative ranking credit.",
    )


def _freshness_score(listing: dict[str, Any], now: datetime) -> float:
    value = listing.get("last_seen_at")
    if not value:
        return 30.0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return 30.0
    age_days = max(0.0, (now - parsed).total_seconds() / 86_400)
    return max(0.0, 100 - age_days * 2)


def score_listings(
    listings: Iterable[dict[str, Any]],
    *,
    weights: RankingWeights = DEFAULT_RANKING_WEIGHTS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return copies of listings with transparent component scores."""

    rows = [dict(listing) for listing in listings]
    groups: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_comparison_key(row), []).append(row)
    normalized_weights = weights.normalized()
    scored: list[dict[str, Any]] = []
    current_time = now or datetime.now(timezone.utc)
    for row in rows:
        components = {
            "price_score": _price_score(row, groups[_comparison_key(row)]),
            "distance_score": _distance_score(row),
            "amenity_score": _amenity_score(row),
            "convenience_score": _convenience_score(row),
            "data_quality_score": listing_quality(row)["score"],
            "freshness_score": _freshness_score(row, current_time),
        }
        overall = (
            components["price_score"] * normalized_weights["price"]
            + components["distance_score"] * normalized_weights["distance"]
            + components["amenity_score"] * normalized_weights["amenities"]
            + components["convenience_score"] * normalized_weights["convenience"]
            + components["data_quality_score"] * normalized_weights["data_quality"]
            + components["freshness_score"] * normalized_weights["freshness"]
        )
        best = sorted(
            (
                (components["price_score"], "price relative to comparable listings"),
                (components["distance_score"], "distance to the selected destination"),
                (components["amenity_score"], "documented amenities"),
                (components["convenience_score"], "available convenience data"),
            ),
            reverse=True,
        )[:2]
        row["ranking"] = {
            "overall_score": round(overall, 1),
            **{key: round(value, 1) for key, value in components.items()},
            "score_explanation": [label for score, label in best if score >= 50],
            "weights": normalized_weights,
            "is_subjective": True,
            "convenience_explanation": _convenience_explanation(row),
        }
        row["data_quality"] = listing_quality(row)
        scored.append(row)
    return scored
