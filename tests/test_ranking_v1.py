from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.ranking_v1 import (
    EXCLUDED,
    PARTIAL,
    RANKED,
    TRANSIT_PERIODS,
    DirectAccessFeature,
    ListingFeatures,
    MarketBaseline,
    TransitPeriodFeature,
    build_market_baselines,
    compute_rankings,
    load_ranking_config,
    ranking_output_fingerprint,
    score_campus_access,
    score_transit_period,
    score_value,
)


CONFIG_PATH = Path("config/ranking-v1.toml")


@pytest.fixture
def config():
    return load_ranking_config(CONFIG_PATH)


def direct(profile_id: int, seconds: int) -> DirectAccessFeature:
    return DirectAccessFeature(profile_id, f"{profile_id:064x}", seconds, seconds * 2)


def period(
    name: str,
    *,
    duration: int | None = 1200,
    walking: int | None = 240,
    transfers: int | None = 0,
    available: int = 3,
    requested: int = 3,
    no_route: int = 0,
    walking_better: int = 0,
    other: int = 0,
    reasons: tuple[str, ...] = (),
    quality: str = "complete",
) -> TransitPeriodFeature:
    index = TRANSIT_PERIODS.index(name) + 100
    return TransitPeriodFeature(
        profile_id=index,
        cache_identity=f"{index:064x}",
        time_period=name,
        representative_duration_seconds=duration,
        minimum_duration_seconds=duration,
        maximum_duration_seconds=duration,
        walking_duration_seconds=walking,
        transfer_count=transfers,
        available_sample_count=available,
        requested_sample_count=requested,
        no_route_sample_count=no_route,
        walking_better_sample_count=walking_better,
        other_unavailable_sample_count=other,
        quality_status=quality,
        reason_codes=reasons,
    )


def listing(
    listing_id: int,
    *,
    price: float | None = 800,
    property_id: int | None = 1,
    housing_type: str | None = "house",
    bedrooms: int | None = 4,
    walking_seconds: int = 1200,
    cycling_seconds: int = 480,
    periods: tuple[TransitPeriodFeature, ...] | None = None,
) -> ListingFeatures:
    return ListingFeatures(
        listing_id=listing_id,
        source_listing_id=str(listing_id),
        property_id=property_id,
        listing_status="active",
        observation_hash=f"{listing_id:064x}",
        address=f"{listing_id} Test St",
        price_monthly=price,
        price_period="month_per_bedroom" if price is not None else None,
        bedrooms=bedrooms,
        housing_type=housing_type,
        lease_type="standard",
        is_sublet=False,
        documented_amenities=(),
        review_flags=(),
        walking=direct(listing_id * 10 + 1, walking_seconds) if property_id else None,
        cycling=direct(listing_id * 10 + 2, cycling_seconds) if property_id else None,
        transit_periods=(
            periods
            if periods is not None
            else tuple(period(name) for name in TRANSIT_PERIODS)
        )
        if property_id
        else (),
    )


def baseline(*prices: float) -> MarketBaseline:
    ordered = tuple(sorted(prices))
    return MarketBaseline(
        level="test",
        key=("test",),
        member_listing_ids=tuple(range(1, len(prices) + 1)),
        prices=ordered,
        median=(ordered[(len(ordered) - 1) // 2] + ordered[len(ordered) // 2]) / 2,
        p25=ordered[1],
        p75=ordered[-2],
    )


def test_config_is_explicit_versioned_and_has_valid_weights(config) -> None:
    assert config.version == "ranking-v1"
    assert config.minimum_comparable_count == 8
    assert config.component_weights == {
        "value": 0.45,
        "campus_access": 0.35,
        "transit": 0.20,
        "amenities": 0.0,
    }
    assert sum(config.transit_period_weights.values()) == pytest.approx(1)
    assert config.system_unavailable_components == ("amenities",)


def test_lower_price_never_has_lower_value_score(config) -> None:
    market = baseline(600, 700, 800, 900, 1000, 1100, 1200, 1300)
    scores = [score_value(price, market, config)[0] for price in market.prices]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] <= 100 and scores[-1] >= 0


def test_value_score_uses_robust_clipping_and_structured_market_signals(config) -> None:
    market = baseline(800, 850, 900, 950, 1000, 1050, 1100, 10_000)
    score, signals = score_value(300, market, config)
    assert score == config.value_maximum_score
    assert signals["market_median"] == 975
    assert signals["comparable_count"] == 8
    assert signals["price_delta_percent"] < 0


def test_shorter_walk_and_cycle_never_reduce_campus_score(config) -> None:
    close = score_campus_access(600, 240, config)[0]
    farther_walk = score_campus_access(1200, 240, config)[0]
    farther_cycle = score_campus_access(600, 480, config)[0]
    assert close > farther_walk
    assert close > farther_cycle


def test_transit_duration_walking_share_and_transfers_are_monotonic(config) -> None:
    good = score_transit_period(
        period(TRANSIT_PERIODS[0], duration=900, walking=180, transfers=0), config
    )[0]
    slower = score_transit_period(
        period(TRANSIT_PERIODS[0], duration=1800, walking=180, transfers=0), config
    )[0]
    more_walking = score_transit_period(
        period(TRANSIT_PERIODS[0], duration=900, walking=650, transfers=0), config
    )[0]
    more_transfers = score_transit_period(
        period(TRANSIT_PERIODS[0], duration=900, walking=180, transfers=2), config
    )[0]
    assert good > slower
    assert good > more_walking
    assert good > more_transfers


def test_deterministic_alternatives_and_manual_review_use_evidence(config) -> None:
    full = score_transit_period(period(TRANSIT_PERIODS[0]), config)[0]
    walking_better = score_transit_period(
        period(
            TRANSIT_PERIODS[0],
            duration=None,
            walking=None,
            transfers=None,
            available=0,
            walking_better=3,
            reasons=("walking_better_than_transit",),
            quality="no_route",
        ),
        config,
    )[0]
    no_route = score_transit_period(
        period(
            TRANSIT_PERIODS[0],
            duration=None,
            walking=None,
            transfers=None,
            available=0,
            no_route=3,
            reasons=("no_route",),
            quality="no_route",
        ),
        config,
    )[0]
    mixed = score_transit_period(
        period(
            TRANSIT_PERIODS[0],
            available=1,
            no_route=1,
            walking_better=1,
            reasons=("insufficient_samples", "no_route", "walking_better_than_transit"),
            quality="insufficient_samples",
        ),
        config,
    )[0]
    assert no_route == config.no_route_score
    assert walking_better == config.walking_better_than_transit_score
    assert 0 < mixed < full


def test_warning_cannot_increase_an_otherwise_identical_transit_period(config) -> None:
    normal = score_transit_period(period(TRANSIT_PERIODS[0]), config)[0]
    warned = score_transit_period(
        period(TRANSIT_PERIODS[0], reasons=("high_walking_share",)), config
    )[0]
    assert warned <= normal


def test_close_property_can_have_high_campus_and_low_transit(config) -> None:
    alternatives = tuple(
        period(
            name,
            duration=None,
            walking=None,
            transfers=None,
            available=0,
            walking_better=3,
            reasons=("walking_better_than_transit",),
            quality="no_route",
        )
        for name in TRANSIT_PERIODS
    )
    rows = [listing(index, price=600 + index * 20) for index in range(1, 8)]
    rows.append(
        listing(
            8,
            price=760,
            walking_seconds=300,
            cycling_seconds=120,
            periods=alternatives,
        )
    )
    results, _ = compute_rankings(rows, config)
    close = next(result for result in results if result.listing_id == 8)
    assert close.campus_access_score > 90
    assert close.transit_score == config.walking_better_than_transit_score


def test_missing_price_is_partial_and_missing_accessibility_is_excluded(config) -> None:
    rows = [listing(index, price=600 + index * 20) for index in range(1, 9)]
    rows.extend([listing(9, price=None), listing(10, property_id=None)])
    results, _ = compute_rankings(rows, config)
    by_id = {result.listing_id: result for result in results}
    assert by_id[9].ranking_status == PARTIAL
    assert by_id[9].overall_score is None
    assert by_id[9].value_score is None
    assert by_id[9].campus_access_score is not None
    assert by_id[10].ranking_status == EXCLUDED
    assert by_id[10].overall_score is None
    assert by_id[1].ranking_status == RANKED


def test_listing_grain_allows_same_property_to_have_different_value(config) -> None:
    rows = [listing(index, price=600 + index * 50) for index in range(1, 9)]
    rows[1] = replace(rows[1], property_id=rows[0].property_id)
    results, _ = compute_rankings(rows, config)
    by_id = {result.listing_id: result for result in results}
    assert by_id[1].property_id == by_id[2].property_id
    assert by_id[1].value_score > by_id[2].value_score


def test_comparable_hierarchy_falls_back_without_tiny_cohorts(config) -> None:
    rows = [listing(index, price=600 + index * 20) for index in range(1, 9)]
    sparse = replace(rows[0], listing_id=20, source_listing_id="20", housing_type="room")
    rows.append(sparse)
    baselines = build_market_baselines(rows, config)
    assert baselines[20].count >= config.minimum_comparable_count
    assert baselines[20].level in {"rental_structure", "relevant_market"}


def test_identical_inputs_are_score_and_fingerprint_deterministic(config) -> None:
    rows = [listing(index, price=600 + index * 20) for index in range(1, 9)]
    first, _ = compute_rankings(
        rows, config, computed_at=datetime(2026, 8, 9, tzinfo=timezone.utc)
    )
    second, _ = compute_rankings(
        rows, config, computed_at=datetime(2026, 8, 10, tzinfo=timezone.utc)
    )
    assert [result.overall_score for result in first] == [
        result.overall_score for result in second
    ]
    assert ranking_output_fingerprint(first) == ranking_output_fingerprint(second)
    assert first[0].input_fingerprint == second[0].input_fingerprint


def test_scores_and_overall_remain_bounded(config) -> None:
    rows = [listing(index, price=300 + index * 1000) for index in range(1, 9)]
    results, _ = compute_rankings(rows, config)
    for result in results:
        for value in (
            result.overall_score,
            result.value_score,
            result.campus_access_score,
            result.transit_score,
        ):
            assert value is None or 0 <= value <= 100
        assert result.amenity_score is None
        assert result.data_quality_score is None
