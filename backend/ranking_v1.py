"""Pure, deterministic, explainable listing-ranking semantics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
from pathlib import Path
import tomllib
from typing import Any, Iterable, Mapping


RANKED = "ranked"
PARTIAL = "partial"
EXCLUDED = "excluded"
TRANSIT_PERIODS = (
    "weekday_morning_commute",
    "weekday_midday",
    "weekday_evening_commute",
    "weekday_late_evening",
    "saturday_daytime",
    "sunday_daytime",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bounded(value: float) -> float:
    return max(0.0, min(100.0, value))


def _score(value: float | None) -> float | None:
    return None if value is None else round(_bounded(value), 2)


def _validate_weight_map(name: str, values: Mapping[str, float]) -> None:
    if any(value < 0 or not isfinite(value) for value in values.values()):
        raise ValueError(f"{name} weights must be finite and non-negative")
    if abs(sum(values.values()) - 1.0) > 1e-9:
        raise ValueError(f"{name} weights must sum to 1")


@dataclass(frozen=True)
class RankingConfig:
    version: str
    minimum_comparable_count: int
    minimum_monthly_price: float
    maximum_monthly_price: float
    required_components: tuple[str, ...]
    system_unavailable_components: tuple[str, ...]
    component_weights: Mapping[str, float]
    value_points_per_iqr: float
    value_minimum_score: float
    value_maximum_score: float
    campus_walking_weight: float
    campus_cycling_weight: float
    walking_midpoint_minutes: float
    cycling_midpoint_minutes: float
    campus_shape: float
    transit_duration_midpoint_minutes: float
    transit_duration_shape: float
    good_walking_share: float
    poor_walking_share: float
    transfer_penalty_points: float
    maximum_range_ratio: float
    walking_better_than_transit_score: float
    no_route_score: float
    transit_component_weights: Mapping[str, float]
    transit_period_weights: Mapping[str, float]
    hotspot_id: str
    provider: str
    provider_profile: str
    network_version: str
    schedule_version: str
    database_url_env: str
    run_root: Path
    fingerprint: str

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("ranking version is required")
        if self.minimum_comparable_count < 2:
            raise ValueError("minimum comparable count must be at least 2")
        if not 0 <= self.minimum_monthly_price < self.maximum_monthly_price:
            raise ValueError("monthly price bounds are invalid")
        _validate_weight_map("component", self.component_weights)
        _validate_weight_map(
            "campus access",
            {
                "walking": self.campus_walking_weight,
                "cycling": self.campus_cycling_weight,
            },
        )
        _validate_weight_map("transit component", self.transit_component_weights)
        _validate_weight_map("transit period", self.transit_period_weights)
        if set(self.transit_period_weights) != set(TRANSIT_PERIODS):
            raise ValueError("transit period weights must cover the six configured periods")
        if not set(self.required_components) <= set(self.component_weights):
            raise ValueError("required components must have configured weights")
        for component in self.system_unavailable_components:
            if self.component_weights.get(component) != 0:
                raise ValueError(
                    f"system-unavailable component {component} must have zero weight"
                )
        if not 0 <= self.good_walking_share < self.poor_walking_share <= 1:
            raise ValueError("transit walking-share thresholds are invalid")

    def with_component_weights(self, **updates: float) -> "RankingConfig":
        values = dict(self.component_weights)
        values.update(updates)
        return replace(self, component_weights=values)


def load_ranking_config(path: Path) -> RankingConfig:
    """Load and validate a path-independent logical configuration fingerprint."""

    parsed = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    ranking = parsed["ranking"]
    weights = {key: float(value) for key, value in parsed["component_weights"].items()}
    value = parsed["value"]
    campus = parsed["campus_access"]
    transit = parsed["transit"]
    access = parsed["accessibility"]
    persistence = parsed["persistence"]
    return RankingConfig(
        version=str(ranking["version"]),
        minimum_comparable_count=int(ranking["minimum_comparable_count"]),
        minimum_monthly_price=float(ranking["minimum_monthly_price"]),
        maximum_monthly_price=float(ranking["maximum_monthly_price"]),
        required_components=tuple(ranking["required_components"]),
        system_unavailable_components=tuple(ranking["system_unavailable_components"]),
        component_weights=weights,
        value_points_per_iqr=float(value["points_per_iqr"]),
        value_minimum_score=float(value["minimum_score"]),
        value_maximum_score=float(value["maximum_score"]),
        campus_walking_weight=float(campus["walking_weight"]),
        campus_cycling_weight=float(campus["cycling_weight"]),
        walking_midpoint_minutes=float(campus["walking_midpoint_minutes"]),
        cycling_midpoint_minutes=float(campus["cycling_midpoint_minutes"]),
        campus_shape=float(campus["shape"]),
        transit_duration_midpoint_minutes=float(transit["duration_midpoint_minutes"]),
        transit_duration_shape=float(transit["duration_shape"]),
        good_walking_share=float(transit["good_walking_share"]),
        poor_walking_share=float(transit["poor_walking_share"]),
        transfer_penalty_points=float(transit["transfer_penalty_points"]),
        maximum_range_ratio=float(transit["maximum_range_ratio"]),
        walking_better_than_transit_score=float(
            transit["walking_better_than_transit_score"]
        ),
        no_route_score=float(transit["no_route_score"]),
        transit_component_weights={
            key: float(value) for key, value in transit["component_weights"].items()
        },
        transit_period_weights={
            key: float(value) for key, value in transit["period_weights"].items()
        },
        hotspot_id=str(access["hotspot_id"]),
        provider=str(access["provider"]),
        provider_profile=str(access["provider_profile"]),
        network_version=str(access["network_version"]),
        schedule_version=str(access["schedule_version"]),
        database_url_env=str(persistence["database_url_env"]),
        run_root=Path(str(persistence["run_root"])),
        fingerprint=_fingerprint(parsed),
    )


@dataclass(frozen=True)
class DirectAccessFeature:
    profile_id: int
    cache_identity: str
    duration_seconds: int
    distance_meters: int | None


@dataclass(frozen=True)
class TransitPeriodFeature:
    profile_id: int
    cache_identity: str
    time_period: str
    representative_duration_seconds: int | None
    minimum_duration_seconds: int | None
    maximum_duration_seconds: int | None
    walking_duration_seconds: int | None
    transfer_count: int | None
    available_sample_count: int
    requested_sample_count: int
    no_route_sample_count: int
    walking_better_sample_count: int
    other_unavailable_sample_count: int
    quality_status: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ListingFeatures:
    listing_id: int
    source_listing_id: str
    property_id: int | None
    listing_status: str
    observation_hash: str
    address: str | None
    price_monthly: float | None
    price_period: str | None
    bedrooms: int | None
    housing_type: str | None
    lease_type: str | None
    is_sublet: bool | None
    documented_amenities: tuple[str, ...]
    review_flags: tuple[str, ...]
    walking: DirectAccessFeature | None = None
    cycling: DirectAccessFeature | None = None
    transit_periods: tuple[TransitPeriodFeature, ...] = ()

    @property
    def rental_structure(self) -> str | None:
        if not self.price_period:
            return None
        return "per_bedroom" if self.price_period == "month_per_bedroom" else "whole_unit"

    @property
    def lease_context(self) -> str:
        return (
            "sublet"
            if self.is_sublet is True
            or self.lease_type == "sublet"
            or self.housing_type == "sublets"
            else "standard"
        )

    @property
    def has_complete_accessibility(self) -> bool:
        return (
            self.walking is not None
            and self.cycling is not None
            and {period.time_period for period in self.transit_periods}
            == set(TRANSIT_PERIODS)
        )


@dataclass(frozen=True)
class MarketBaseline:
    level: str
    key: tuple[Any, ...]
    member_listing_ids: tuple[int, ...]
    prices: tuple[float, ...]
    median: float
    p25: float
    p75: float

    @property
    def count(self) -> int:
        return len(self.prices)

    @property
    def iqr(self) -> float:
        return self.p75 - self.p25


@dataclass(frozen=True)
class RankingResult:
    listing_id: int
    source_listing_id: str
    property_id: int | None
    ranking_version: str
    ranking_status: str
    overall_score: float | None
    value_score: float | None
    campus_access_score: float | None
    transit_score: float | None
    amenity_score: float | None
    data_quality_score: float | None
    explanation: Mapping[str, Any]
    input_fingerprint: str
    computed_at: datetime

    def stable_payload(self) -> dict[str, Any]:
        explanation = dict(self.explanation)
        explanation.pop("computed_at", None)
        return {
            "listing_id": self.listing_id,
            "source_listing_id": self.source_listing_id,
            "property_id": self.property_id,
            "ranking_version": self.ranking_version,
            "ranking_status": self.ranking_status,
            "overall_score": self.overall_score,
            "value_score": self.value_score,
            "campus_access_score": self.campus_access_score,
            "transit_score": self.transit_score,
            "amenity_score": self.amenity_score,
            "data_quality_score": self.data_quality_score,
            "explanation": explanation,
            "input_fingerprint": self.input_fingerprint,
        }


def _quantile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("quantile requires at least one value")
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    offset = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * offset


def _valid_market_row(feature: ListingFeatures, config: RankingConfig) -> bool:
    price = feature.price_monthly
    return (
        feature.listing_status in {"active", "relisted"}
        and price is not None
        and config.minimum_monthly_price <= price <= config.maximum_monthly_price
        and feature.rental_structure is not None
        and bool(feature.housing_type)
        and feature.bedrooms is not None
    )


def _segment_keys(feature: ListingFeatures) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    return (
        (
            "structure_type_bedrooms_lease",
            (
                feature.rental_structure,
                feature.housing_type,
                feature.bedrooms,
                feature.lease_context,
            ),
        ),
        (
            "structure_type_bedrooms",
            (feature.rental_structure, feature.housing_type, feature.bedrooms),
        ),
        (
            "structure_type",
            (feature.rental_structure, feature.housing_type),
        ),
        ("rental_structure", (feature.rental_structure,)),
        ("relevant_market", ("all",)),
    )


def build_market_baselines(
    features: Iterable[ListingFeatures], config: RankingConfig
) -> dict[int, MarketBaseline]:
    """Select the narrowest sufficiently populated robust price cohort."""

    candidates = [feature for feature in features if _valid_market_row(feature, config)]
    groups: dict[tuple[str, tuple[Any, ...]], list[ListingFeatures]] = {}
    for feature in candidates:
        for level, key in _segment_keys(feature):
            groups.setdefault((level, key), []).append(feature)
    selected: dict[int, MarketBaseline] = {}
    for feature in candidates:
        choices = _segment_keys(feature)
        for index, (level, key) in enumerate(choices):
            members = groups[(level, key)]
            prices = tuple(sorted(float(member.price_monthly) for member in members if member.price_monthly is not None))
            p25 = _quantile(prices, 0.25)
            p75 = _quantile(prices, 0.75)
            is_final = index == len(choices) - 1
            if len(prices) < config.minimum_comparable_count and not is_final:
                continue
            if p75 == p25 and not is_final:
                continue
            selected[feature.listing_id] = MarketBaseline(
                level=level,
                key=key,
                member_listing_ids=tuple(sorted(member.listing_id for member in members)),
                prices=prices,
                median=_quantile(prices, 0.5),
                p25=p25,
                p75=p75,
            )
            break
    return selected


def score_value(
    price: float, baseline: MarketBaseline, config: RankingConfig
) -> tuple[float, dict[str, Any]]:
    if baseline.iqr == 0:
        raw = 50.0
    else:
        raw = 50.0 - config.value_points_per_iqr * (
            (price - baseline.median) / baseline.iqr
        )
    value_score = max(config.value_minimum_score, min(config.value_maximum_score, raw))
    less = sum(candidate < price for candidate in baseline.prices)
    equal = sum(candidate == price for candidate in baseline.prices)
    percentile = (
        50.0
        if baseline.count == 1
        else 100.0 * (less + (equal - 1) / 2) / (baseline.count - 1)
    )
    delta = 0.0 if baseline.median == 0 else 100.0 * (price - baseline.median) / baseline.median
    return _bounded(value_score), {
        "monthly_price": round(price, 2),
        "comparable_level": baseline.level,
        "comparable_key": list(baseline.key),
        "comparable_count": baseline.count,
        "market_median": round(baseline.median, 2),
        "market_p25": round(baseline.p25, 2),
        "market_p75": round(baseline.p75, 2),
        "price_delta_percent": round(delta, 2),
        "price_percentile": round(percentile, 2),
    }


def _bounded_inverse(minutes: float, midpoint: float, shape: float) -> float:
    return 100.0 / (1.0 + (max(0.0, minutes) / midpoint) ** shape)


def score_campus_access(
    walking_seconds: int, cycling_seconds: int, config: RankingConfig
) -> tuple[float, dict[str, Any]]:
    walking_minutes = walking_seconds / 60
    cycling_minutes = cycling_seconds / 60
    walking_score = _bounded_inverse(
        walking_minutes, config.walking_midpoint_minutes, config.campus_shape
    )
    cycling_score = _bounded_inverse(
        cycling_minutes, config.cycling_midpoint_minutes, config.campus_shape
    )
    combined = (
        walking_score * config.campus_walking_weight
        + cycling_score * config.campus_cycling_weight
    )
    return _bounded(combined), {
        "walking_minutes": round(walking_minutes, 2),
        "cycling_minutes": round(cycling_minutes, 2),
        "walking_score": _score(walking_score),
        "cycling_score": _score(cycling_score),
    }


def score_transit_period(
    period: TransitPeriodFeature, config: RankingConfig
) -> tuple[float, dict[str, Any]]:
    requested = max(1, period.requested_sample_count)
    itinerary_score: float | None = None
    walking_share: float | None = None
    if (
        period.representative_duration_seconds is not None
        and period.available_sample_count > 0
    ):
        duration_minutes = period.representative_duration_seconds / 60
        duration_score = _bounded_inverse(
            duration_minutes,
            config.transit_duration_midpoint_minutes,
            config.transit_duration_shape,
        )
        if period.walking_duration_seconds is None:
            walking_score = 0.0
        else:
            walking_share = min(
                1.0,
                period.walking_duration_seconds
                / max(1, period.representative_duration_seconds),
            )
            walking_score = 100.0 * (
                1.0
                - max(
                    0.0,
                    min(
                        1.0,
                        (walking_share - config.good_walking_share)
                        / (config.poor_walking_share - config.good_walking_share),
                    ),
                )
            )
        transfer_score = max(
            0.0,
            100.0
            - (period.transfer_count or 0) * config.transfer_penalty_points,
        )
        if (
            period.minimum_duration_seconds is None
            or period.maximum_duration_seconds is None
        ):
            reliability_score = 0.0
        else:
            width_ratio = (
                period.maximum_duration_seconds - period.minimum_duration_seconds
            ) / max(1, period.representative_duration_seconds)
            reliability_score = 100.0 * (
                1.0 - min(1.0, width_ratio / config.maximum_range_ratio)
            )
        components = config.transit_component_weights
        itinerary_score = (
            duration_score * components["duration"]
            + walking_score * components["walking_share"]
            + transfer_score * components["transfers"]
            + reliability_score * components["reliability"]
        )
    numerator = (
        period.available_sample_count * (itinerary_score or 0.0)
        + period.walking_better_sample_count
        * config.walking_better_than_transit_score
        + period.no_route_sample_count * config.no_route_score
    )
    period_score = numerator / requested
    return _bounded(period_score), {
        "time_period": period.time_period,
        "score": _score(period_score),
        "representative_minutes": (
            round(period.representative_duration_seconds / 60, 2)
            if period.representative_duration_seconds is not None
            else None
        ),
        "walking_share": round(walking_share, 4) if walking_share is not None else None,
        "transfers": period.transfer_count,
        "available_samples": period.available_sample_count,
        "requested_samples": requested,
        "no_route_samples": period.no_route_sample_count,
        "walking_better_samples": period.walking_better_sample_count,
        "other_unavailable_samples": period.other_unavailable_sample_count,
        "quality_status": period.quality_status,
        "reason_codes": list(period.reason_codes),
    }


def score_transit(
    periods: Iterable[TransitPeriodFeature], config: RankingConfig
) -> tuple[float, list[dict[str, Any]]]:
    by_period = {period.time_period: period for period in periods}
    summaries: list[dict[str, Any]] = []
    total = 0.0
    for name in TRANSIT_PERIODS:
        period_score, summary = score_transit_period(by_period[name], config)
        summary["weight"] = config.transit_period_weights[name]
        summaries.append(summary)
        total += period_score * config.transit_period_weights[name]
    return _bounded(total), summaries


def ranking_status(
    feature: ListingFeatures, config: RankingConfig
) -> tuple[str, tuple[str, ...]]:
    if feature.listing_status not in {"active", "relisted"}:
        return EXCLUDED, ("listing_not_active",)
    if feature.property_id is None:
        return EXCLUDED, ("missing_property",)
    if not feature.has_complete_accessibility:
        return EXCLUDED, ("incomplete_trusted_accessibility",)
    missing: list[str] = []
    if feature.price_monthly is None:
        missing.append("missing_monthly_price")
    elif not (
        config.minimum_monthly_price
        <= feature.price_monthly
        <= config.maximum_monthly_price
    ):
        missing.append("implausible_monthly_price")
    if feature.bedrooms is None:
        missing.append("missing_bedrooms")
    if not feature.housing_type:
        missing.append("missing_housing_type")
    if feature.rental_structure is None:
        missing.append("missing_rental_structure")
    return (PARTIAL, tuple(missing)) if missing else (RANKED, ())


def _feature_payload(feature: ListingFeatures) -> dict[str, Any]:
    return asdict(feature)


def score_with_baselines(
    features: Iterable[ListingFeatures],
    baselines: Mapping[int, MarketBaseline],
    config: RankingConfig,
    *,
    computed_at: datetime | None = None,
) -> list[RankingResult]:
    """Score listing inputs without I/O; timestamps never affect score values."""

    timestamp = computed_at or datetime.now(timezone.utc)
    results: list[RankingResult] = []
    for feature in sorted(features, key=lambda item: item.listing_id):
        status, eligibility_reasons = ranking_status(feature, config)
        campus_score: float | None = None
        campus_signals: dict[str, Any] | None = None
        transit_score: float | None = None
        transit_signals: list[dict[str, Any]] = []
        if feature.has_complete_accessibility:
            assert feature.walking is not None and feature.cycling is not None
            campus_score, campus_signals = score_campus_access(
                feature.walking.duration_seconds,
                feature.cycling.duration_seconds,
                config,
            )
            transit_score, transit_signals = score_transit(
                feature.transit_periods, config
            )
        value_score: float | None = None
        value_signals: dict[str, Any] | None = None
        baseline = baselines.get(feature.listing_id)
        if status == RANKED:
            if feature.price_monthly is None or baseline is None:
                raise ValueError(
                    f"Ranked listing {feature.listing_id} has no market baseline"
                )
            value_score, value_signals = score_value(
                feature.price_monthly, baseline, config
            )
        components = {
            "value": _score(value_score),
            "campus_access": _score(campus_score),
            "transit": _score(transit_score),
            "amenities": None,
            "data_quality": None,
        }
        overall: float | None = None
        if status == RANKED:
            assert value_score is not None and campus_score is not None and transit_score is not None
            overall = sum(
                components[name] * weight
                for name, weight in config.component_weights.items()
                if components.get(name) is not None
            )
        warning_codes = sorted(
            {
                code
                for period in feature.transit_periods
                for code in period.reason_codes
            }
        )
        fingerprint_payload = {
            "ranking_version": config.version,
            "config_fingerprint": config.fingerprint,
            "listing": _feature_payload(feature),
            "market_baseline": asdict(baseline) if baseline else None,
        }
        input_fingerprint = _fingerprint(fingerprint_payload)
        explanation: dict[str, Any] = {
            "ranking_version": config.version,
            "ranking_status": status,
            "overall_score": _score(overall),
            "component_scores": components,
            "component_weights": dict(config.component_weights),
            "key_signals": {
                "value": value_signals,
                "campus_access": campus_signals,
                "transit_periods": transit_signals,
            },
            "eligibility_reasons": list(eligibility_reasons),
            "confidence": {
                "accessibility_review_required": "insufficient_samples"
                in warning_codes,
                "accessibility_warning_reason_codes": warning_codes,
                "listing_review_flags": list(feature.review_flags),
            },
            "system_unavailable_components": list(
                config.system_unavailable_components
            ),
            "amenity_status": "not_implemented",
            "data_quality_component_status": "not_scored",
            "input_fingerprint": input_fingerprint,
            "config_fingerprint": config.fingerprint,
            "computed_at": timestamp.isoformat(),
        }
        results.append(
            RankingResult(
                listing_id=feature.listing_id,
                source_listing_id=feature.source_listing_id,
                property_id=feature.property_id,
                ranking_version=config.version,
                ranking_status=status,
                overall_score=_score(overall),
                value_score=_score(value_score),
                campus_access_score=_score(campus_score),
                transit_score=_score(transit_score),
                amenity_score=None,
                data_quality_score=None,
                explanation=explanation,
                input_fingerprint=input_fingerprint,
                computed_at=timestamp,
            )
        )
    return results


def compute_rankings(
    features: Iterable[ListingFeatures],
    config: RankingConfig,
    *,
    computed_at: datetime | None = None,
) -> tuple[list[RankingResult], dict[int, MarketBaseline]]:
    rows = list(features)
    baselines = build_market_baselines(rows, config)
    return score_with_baselines(rows, baselines, config, computed_at=computed_at), baselines


def ranking_input_fingerprint(results: Iterable[RankingResult]) -> str:
    return _fingerprint(
        [
            {"listing_id": result.listing_id, "input_fingerprint": result.input_fingerprint}
            for result in sorted(results, key=lambda item: item.listing_id)
        ]
    )


def ranking_output_fingerprint(results: Iterable[RankingResult]) -> str:
    return _fingerprint(
        [
            result.stable_payload()
            for result in sorted(results, key=lambda item: item.listing_id)
        ]
    )
