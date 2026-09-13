from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.accessibility import (
    AccessibilityResolver,
    aggregate_transit_profile,
)
from backend.accessibility_repository import (
    InMemoryAccessibilityRepository,
    PostgresAccessibilityRepository,
    profile_cache_identity,
)
from backend.domain import (
    AccessibilityOrigin,
    AccessibilityProfile,
    AccessibilityRequest,
    AccessibilityResultType,
    Coordinates,
    DayType,
    Hotspot,
    OriginType,
    TimePeriod,
    TravelMode,
    TravelStatus,
    TravelTimeSample,
)
from backend.hotspots import DEFAULT_HOTSPOTS
from backend.main import create_app
from backend.origin_zones import CoordinateGridOriginZoneResolver
from backend.providers import haversine_distance_meters
from backend.ranking import score_listings
from backend.repository import FixtureCsvListingRepository, InMemoryListingRepository

NOW = datetime.now(timezone.utc)
WESTERN = DEFAULT_HOTSPOTS[0]


def origin(
    *,
    latitude: float = 43.0,
    longitude: float = -81.25,
    property_id: int | None = 1,
    entrance: str | None = None,
    stop: str | None = None,
    walking_to_stop: int | None = None,
) -> AccessibilityOrigin:
    return AccessibilityOrigin(
        coordinates=Coordinates(latitude, longitude),
        property_id=property_id,
        origin_type=OriginType.PROPERTY if property_id else OriginType.COORDINATE,
        entrance_fingerprint=entrance,
        nearest_stop_id=stop,
        walking_to_stop_seconds=walking_to_stop,
    )


def profile(
    *,
    profile_id: int | None = None,
    profile_origin: AccessibilityOrigin | None = None,
    hotspot_id: str = "western-main-campus",
    mode: TravelMode = TravelMode.WALKING,
    period: TimePeriod | None = None,
    provider: str = "fixture",
    provider_profile: str = "v1",
    schedule_version: str | None = None,
    network_version: str | None = "streets-1",
    expires_at: datetime | None = None,
    stale: bool = False,
    duration: int = 1800,
    minimum: int | None = 1700,
    maximum: int | None = 1900,
    distance: int = 2400,
    walking: int | None = None,
    transfers: int | None = None,
    nearest_stop: str | None = None,
    stop_component: int | None = None,
    confidence: float = 0.95,
) -> AccessibilityProfile:
    if mode is TravelMode.TRANSIT:
        period = period or TimePeriod.WEEKDAY_MORNING_COMMUTE
        day_type = DayType.WEEKDAY
    else:
        day_type = None
    return AccessibilityProfile(
        profile_id=profile_id,
        origin=profile_origin or origin(),
        hotspot_id=hotspot_id,
        travel_mode=mode,
        day_type=day_type,
        time_period=period,
        representative_duration_seconds=duration,
        minimum_duration_seconds=minimum,
        maximum_duration_seconds=maximum,
        distance_meters=distance,
        walking_duration_seconds=walking,
        transfer_count=transfers,
        nearest_stop_id=nearest_stop,
        stop_to_destination_seconds=stop_component,
        provider=provider,
        provider_profile=provider_profile,
        result_type=AccessibilityResultType.EXACT_ROUTE,
        confidence=confidence,
        sample_count=3,
        calculated_at=NOW - timedelta(days=3),
        schedule_version=schedule_version,
        network_version=network_version,
        expires_at=expires_at or NOW + timedelta(days=30),
        is_stale=stale,
    )


def request(
    *,
    request_origin: AccessibilityOrigin | None = None,
    hotspot: Hotspot = WESTERN,
    mode: TravelMode = TravelMode.WALKING,
    period: TimePeriod | None = None,
    provider: str = "fixture",
    provider_profile: str = "v1",
    schedule_version: str | None = None,
    network_version: str | None = "streets-1",
) -> AccessibilityRequest:
    return AccessibilityRequest(
        origin=request_origin or origin(),
        hotspot=hotspot,
        travel_mode=mode,
        time_period=period,
        provider=provider,
        provider_profile=provider_profile,
        requested_at=NOW,
        schedule_version=schedule_version,
        network_version=network_version,
    )


def resolve(
    stored_profiles: list[AccessibilityProfile],
    accessibility_request: AccessibilityRequest,
    **resolver_options: object,
):
    repository = InMemoryAccessibilityRepository(stored_profiles)
    decision = AccessibilityResolver(repository, **resolver_options).resolve(
        accessibility_request, listing_id="listing-1"
    )
    return decision, repository


def test_removed_listing_leaves_property_profile_intact() -> None:
    listings = [{"listing_id": "old", "property_id": 1}]
    repository = InMemoryAccessibilityRepository([profile()])
    listings.clear()
    assert not listings
    assert repository.find_exact_property(request())[0].origin.property_id == 1


def test_new_listing_at_same_property_reuses_exact_profile() -> None:
    decision, repository = resolve([profile()], request())
    assert decision.result_type is AccessibilityResultType.CACHED_EXACT_PROPERTY
    assert decision.is_estimate is False
    assert repository.reuse_history[-1]["result_type"] == "cached_exact_property"


def test_same_coordinate_for_different_property_reuses_exact_origin() -> None:
    decision, _ = resolve(
        [profile(profile_origin=origin(property_id=1))],
        request(request_origin=origin(property_id=2)),
    )
    assert decision.result_type is AccessibilityResultType.CACHED_EXACT_ORIGIN


def test_same_entrance_fingerprint_reuses_exact_origin() -> None:
    decision, _ = resolve(
        [profile(profile_origin=origin(property_id=None, entrance="door-a"))],
        request(
            request_origin=origin(
                latitude=43.00002, property_id=None, entrance="door-a"
            )
        ),
    )
    assert decision.result_type is AccessibilityResultType.CACHED_EXACT_ORIGIN


@pytest.mark.parametrize(
    "changed_request",
    [
        request(
            hotspot=Hotspot(
                id="different",
                name="Different",
                coordinates=Coordinates(43.01, -81.27),
            )
        ),
        request(mode=TravelMode.CYCLING),
        request(provider="different-provider"),
        request(provider_profile="v2"),
        request(network_version="streets-2"),
    ],
)
def test_incompatible_cache_dimensions_do_not_reuse(
    changed_request: AccessibilityRequest,
) -> None:
    decision, _ = resolve([profile()], changed_request)
    assert decision.result_type is AccessibilityResultType.STRAIGHT_LINE_FALLBACK


def test_different_transit_period_does_not_reuse() -> None:
    stored = profile(
        mode=TravelMode.TRANSIT,
        period=TimePeriod.WEEKDAY_MORNING_COMMUTE,
        schedule_version="gtfs-1",
        nearest_stop="stop-a",
        stop_component=900,
    )
    transit_request = request(
        request_origin=origin(stop="stop-a", walking_to_stop=300),
        mode=TravelMode.TRANSIT,
        period=TimePeriod.WEEKDAY_MIDDAY,
        schedule_version="gtfs-1",
    )
    decision, _ = resolve([stored], transit_request)
    assert decision.result_type is AccessibilityResultType.PENDING_PROVIDER


def test_expired_profile_is_retained_but_not_current_exact_data() -> None:
    expired = profile(expires_at=NOW - timedelta(seconds=1))
    decision, repository = resolve([expired], request())
    assert decision.result_type is AccessibilityResultType.STALE
    assert decision.freshness == "stale"
    assert decision.result.is_stale is True
    assert repository.profiles[0].profile_id is not None


def test_compatible_schedule_reuses_transit_property_profile() -> None:
    stored = profile(
        mode=TravelMode.TRANSIT,
        schedule_version="gtfs-1",
        nearest_stop="stop-a",
        stop_component=900,
    )
    transit_request = request(
        request_origin=origin(stop="stop-a", walking_to_stop=300),
        mode=TravelMode.TRANSIT,
        period=TimePeriod.WEEKDAY_MORNING_COMMUTE,
        schedule_version="gtfs-1",
    )
    decision, _ = resolve([stored], transit_request)
    assert decision.result_type is AccessibilityResultType.CACHED_EXACT_PROPERTY


def test_incompatible_schedule_blocks_transit_reuse() -> None:
    stored = profile(
        mode=TravelMode.TRANSIT,
        schedule_version="gtfs-old",
        nearest_stop="stop-a",
        stop_component=900,
    )
    transit_request = request(
        request_origin=origin(stop="stop-a", walking_to_stop=300),
        mode=TravelMode.TRANSIT,
        period=TimePeriod.WEEKDAY_MORNING_COMMUTE,
        schedule_version="gtfs-new",
    )
    decision, _ = resolve([stored], transit_request)
    assert decision.result_type is AccessibilityResultType.PENDING_PROVIDER


@pytest.mark.parametrize("mode", [TravelMode.WALKING, TravelMode.CYCLING])
def test_nearby_walk_and_cycle_estimate_under_threshold(mode: TravelMode) -> None:
    source_origin = origin(latitude=43.0, property_id=1)
    new_origin = origin(latitude=43.00045, property_id=2)
    decision, _ = resolve(
        [profile(profile_origin=source_origin, mode=mode)],
        request(request_origin=new_origin, mode=mode),
    )
    assert decision.result_type is AccessibilityResultType.NEARBY_ORIGIN_ESTIMATE
    assert decision.is_estimate is True
    assert decision.result.source_profile_id is not None


def test_nearby_profile_over_threshold_is_rejected() -> None:
    decision, _ = resolve(
        [profile(profile_origin=origin(latitude=43.0, property_id=1))],
        request(request_origin=origin(latitude=43.002, property_id=2)),
    )
    assert decision.result_type is AccessibilityResultType.STRAIGHT_LINE_FALLBACK


def test_connector_duration_uses_speed_and_detour_factor() -> None:
    source_origin = origin(latitude=43.0, property_id=1)
    new_origin = origin(latitude=43.00045, property_id=2)
    source = profile(profile_origin=source_origin, duration=1800)
    decision, _ = resolve(
        [source],
        request(request_origin=new_origin),
        connector_detour_factor=1.35,
        walking_speed_kmh=4.8,
    )
    distance = haversine_distance_meters(
        new_origin.coordinates, source_origin.coordinates
    )
    expected_connector = ceil(ceil(distance * 1.35) / (4.8 * 1000 / 3600))
    assert decision.result.representative_duration_seconds == 1800 + expected_connector
    assert decision.result.estimation_distance_meters == distance
    assert "x1.35" in (decision.result.estimation_method or "")


def test_barrier_policy_can_reject_nearby_reuse() -> None:
    class RejectBarrier:
        def allows_reuse(self, origin, candidate):
            return False

    decision, _ = resolve(
        [profile(profile_origin=origin(latitude=43.0, property_id=1))],
        request(request_origin=origin(latitude=43.00045, property_id=2)),
        barrier_policy=RejectBarrier(),
    )
    assert decision.result_type is AccessibilityResultType.STRAIGHT_LINE_FALLBACK


def transit_profile(*, stop: str = "stop-a", schedule: str = "gtfs-1") -> AccessibilityProfile:
    return profile(
        profile_origin=origin(
            latitude=43.0,
            property_id=1,
            stop=stop,
            walking_to_stop=300,
        ),
        mode=TravelMode.TRANSIT,
        period=TimePeriod.WEEKDAY_MORNING_COMMUTE,
        schedule_version=schedule,
        duration=1200,
        minimum=1100,
        maximum=1300,
        walking=300,
        transfers=1,
        nearest_stop=stop,
        stop_component=900,
    )


def transit_request(
    *, stop: str | None = "stop-a", walking: int | None = 420
) -> AccessibilityRequest:
    return request(
        request_origin=origin(
            latitude=43.001,
            property_id=2,
            stop=stop,
            walking_to_stop=walking,
        ),
        mode=TravelMode.TRANSIT,
        period=TimePeriod.WEEKDAY_MORNING_COMMUTE,
        schedule_version="gtfs-1",
    )


def test_transit_is_not_reused_only_because_origins_are_close() -> None:
    decision, _ = resolve([transit_profile(stop="stop-a")], transit_request(stop="stop-b"))
    assert decision.result_type is AccessibilityResultType.PENDING_PROVIDER


def test_same_stop_transit_reuses_stop_component_and_new_walk() -> None:
    decision, repository = resolve([transit_profile()], transit_request())
    assert decision.result_type is AccessibilityResultType.SAME_STOP_REUSE
    assert decision.result.representative_duration_seconds == 1320
    assert decision.result.minimum_duration_seconds == 1220
    assert decision.result.maximum_duration_seconds == 1420
    assert decision.result.walking_duration_seconds == 420
    assert decision.result.stop_to_destination_seconds == 900
    assert repository.reuse_history[-1]["source_profile_id"] == 1


@pytest.mark.parametrize(
    "unsafe_request",
    [transit_request(stop="different"), transit_request(stop=None), transit_request(walking=None)],
)
def test_missing_different_or_disconnected_stop_blocks_transit_reuse(
    unsafe_request: AccessibilityRequest,
) -> None:
    decision, _ = resolve([transit_profile()], unsafe_request)
    assert decision.result_type is AccessibilityResultType.PENDING_PROVIDER
    assert decision.result.representative_duration_seconds is None


def test_transit_requires_time_period() -> None:
    with pytest.raises(ValueError, match="time_period"):
        resolve(
            [],
            request(mode=TravelMode.TRANSIT, period=None, schedule_version="gtfs-1"),
        )


def sample(
    minute: int,
    duration: int | None,
    *,
    walking: int | None = 300,
    transfers: int | None = 1,
    status: TravelStatus = TravelStatus.AVAILABLE,
) -> TravelTimeSample:
    return TravelTimeSample(
        departure_at=NOW.replace(hour=8, minute=minute),
        duration_seconds=duration,
        walking_duration_seconds=walking,
        transfer_count=transfers,
        distance_meters=5000 if duration is not None else None,
        stop_to_destination_seconds=(duration - walking if duration is not None and walking is not None else None),
        status=status,
    )


def test_transit_aggregation_is_deterministic_and_preserves_range_and_count() -> None:
    samples = [sample(0, 1800), sample(30, 1500, walking=240, transfers=0), sample(15, 2100)]
    aggregated = aggregate_transit_profile(
        origin=origin(stop="stop-a"),
        hotspot_id=WESTERN.id,
        time_period=TimePeriod.WEEKDAY_MORNING_COMMUTE,
        samples=samples,
        provider="fixture",
        provider_profile="v1",
        calculated_at=NOW,
        schedule_version="gtfs-1",
        network_version="streets-1",
        expected_sample_count=3,
    )
    assert aggregated.representative_duration_seconds == 1800
    assert aggregated.minimum_duration_seconds == 1500
    assert aggregated.maximum_duration_seconds == 2100
    assert aggregated.sample_count == 3
    assert aggregated.walking_duration_seconds == 300
    assert aggregated.transfer_count == 1
    assert aggregated.confidence == 0.95


def test_missing_samples_never_become_zero() -> None:
    aggregated = aggregate_transit_profile(
        origin=origin(stop="stop-a"),
        hotspot_id=WESTERN.id,
        time_period=TimePeriod.WEEKDAY_MIDDAY,
        samples=[sample(0, None, walking=None, transfers=None, status=TravelStatus.UNAVAILABLE)],
        provider="fixture",
        provider_profile="v1",
        calculated_at=NOW,
        schedule_version="gtfs-1",
        network_version=None,
        expected_sample_count=3,
    )
    assert aggregated.representative_duration_seconds is None
    assert aggregated.minimum_duration_seconds is None
    assert aggregated.maximum_duration_seconds is None
    assert aggregated.sample_count == 0
    assert aggregated.confidence == 0


def test_exact_precedes_nearby_and_nearby_precedes_fallback() -> None:
    exact = profile(profile_origin=origin(property_id=2), duration=1000)
    nearby = profile(profile_origin=origin(latitude=43.00045, property_id=3), duration=900)
    target = request(request_origin=origin(property_id=2))
    exact_decision, _ = resolve([nearby, exact], target)
    assert exact_decision.result.representative_duration_seconds == 1000
    assert exact_decision.result_type is AccessibilityResultType.CACHED_EXACT_PROPERTY
    nearby_decision, _ = resolve([nearby], target)
    assert nearby_decision.result_type is AccessibilityResultType.NEARBY_ORIGIN_ESTIMATE


def test_stale_invalidation_retains_profile_and_lists_it_for_refresh() -> None:
    repository = InMemoryAccessibilityRepository(
        [profile(expires_at=NOW - timedelta(seconds=1))]
    )
    assert repository.invalidate_stale(NOW) == 1
    assert len(repository.profiles) == 1
    assert repository.profiles[0].is_stale is True
    assert repository.profiles_due_for_refresh(NOW) == repository.profiles


def test_schedule_change_marks_transit_stale_without_deleting_history() -> None:
    repository = InMemoryAccessibilityRepository([transit_profile(schedule="old")])
    assert repository.invalidate_stale(NOW, schedule_version="new") == 1
    assert repository.profiles[0].stale_reason == "version_changed"


def test_in_memory_repository_upsert_is_idempotent_and_auditable() -> None:
    repository = InMemoryAccessibilityRepository()
    first = repository.save_profile(profile(duration=1000))
    second = repository.save_profile(profile(duration=1100))
    assert first.profile_id == second.profile_id
    assert len(repository.profiles) == 1
    repository.save_samples(first.profile_id, [sample(0, 1000), sample(0, 1100)])
    assert len(repository.samples[first.profile_id]) == 1
    repository.save_reuse_decision({"result_type": "cached_exact_property"})
    assert repository.reuse_history == [{"result_type": "cached_exact_property"}]
    assert len(profile_cache_identity(profile())) == 64


def test_origin_zone_is_deterministic_and_not_a_distance_decision() -> None:
    resolver = CoordinateGridOriginZoneResolver()
    coordinates = Coordinates(43.0, -81.25)
    assert resolver.zone_for(coordinates) == resolver.zone_for(coordinates)
    assert resolver.zone_for(coordinates).startswith("grid:")


def test_ranking_penalizes_estimates_stale_and_missing_accessibility() -> None:
    base = {"housing_type": "room", "bedrooms": 1, "price_monthly": 800}
    rows = [
        {**base, "listing_id": "exact", "accessibility": {"result_type": "cached_exact_property", "confidence": 1}},
        {**base, "listing_id": "nearby", "accessibility": {"result_type": "nearby_origin_estimate", "confidence": 0.7}},
        {**base, "listing_id": "stale", "accessibility": {"result_type": "stale", "confidence": 1}},
        {**base, "listing_id": "missing"},
    ]
    scores = {row["listing_id"]: row["ranking"] for row in score_listings(rows, now=NOW)}
    assert scores["exact"]["convenience_score"] > scores["nearby"]["convenience_score"]
    assert scores["nearby"]["convenience_score"] > scores["stale"]["convenience_score"]
    assert scores["missing"]["convenience_score"] < 50
    assert "nearby" in scores["nearby"]["convenience_explanation"].lower()


def api_listing(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "listing_id": "api-1",
        "listing_url": "https://offcampus.uwo.ca/Listings/Details/1",
        "title": "API listing",
        "address": "1 Test Street",
        "price_monthly": 800,
        "bedrooms": 1,
        "housing_type": "room",
        "latitude": 43.0,
        "longitude": -81.25,
        "map_ready": True,
        "property_id": 1,
        "nearest_stop_id": "stop-a",
        "walking_minutes_to_nearest_stop": 5,
    }
    row.update(updates)
    return row


def api_client(
    stored_profiles: list[AccessibilityProfile], listing: dict[str, object]
) -> TestClient:
    return TestClient(
        create_app(
            repository=InMemoryListingRepository([listing]),
            accessibility_repository=InMemoryAccessibilityRepository(stored_profiles),
            accessibility_provider_name="fixture",
            accessibility_provider_profile="v1",
            schedule_version="gtfs-1",
            network_version="streets-1",
        )
    )


def test_api_validates_period_and_exposes_representative_periods() -> None:
    client = api_client([], api_listing())
    assert client.get("/api/listings/api-1/accessibility?mode=transit").status_code == 422
    assert client.get("/api/listings/api-1/accessibility?mode=transit&time_period=invalid").status_code == 422
    periods = client.get("/api/accessibility/time-periods").json()
    assert periods["count"] == 6
    assert periods["live_departures_supported"] is False
    assert periods["time_periods"][0]["departures"] == ["07:30", "08:00", "08:30"]


def test_api_walking_without_period_serializes_exact_property() -> None:
    client = api_client([profile()], api_listing())
    body = client.get("/api/listings/api-1/accessibility?mode=walking").json()
    assert body["property_id"] == 1
    assert body["result_type"] == "cached_exact_property"
    assert body["representative_duration_seconds"] == 1800
    assert body["is_estimate"] is False


def test_api_serializes_nearby_origin_estimate() -> None:
    source = profile(profile_origin=origin(latitude=43.00045, property_id=2))
    client = api_client([source], api_listing(property_id=3))
    body = client.get("/api/listings/api-1/accessibility?mode=walking").json()
    assert body["result_type"] == "nearby_origin_estimate"
    assert body["source_profile_id"] is not None
    assert body["estimation_distance_meters"] > 0
    assert body["is_estimate"] is True


def test_api_serializes_same_stop_reuse() -> None:
    client = api_client(
        [transit_profile()],
        api_listing(property_id=2, latitude=43.001, walking_minutes_to_nearest_stop=7),
    )
    body = client.get(
        "/api/listings/api-1/accessibility"
        "?mode=transit&time_period=weekday_morning_commute"
    ).json()
    assert body["result_type"] == "same_stop_reuse"
    assert body["walking_duration_seconds"] == 420
    assert body["representative_duration_seconds"] == 1320
    assert body["route"]["status"] == "available"
    assert body["route"]["duration_minutes"] == 22
    assert body["route"]["result_type"] == "same_stop_reuse"
    assert body["route"]["minimum_duration_seconds"] == 1700
    assert body["route"]["maximum_duration_seconds"] == 1900
    assert body["route"]["geometry_available"] is False
    assert body["route"]["itinerary"] is None


def test_fixture_launch_remains_functional() -> None:
    fixture = Path("tests/fixtures/runs/postgres_valid/stage3/canonical.csv")
    client = TestClient(create_app(repository=FixtureCsvListingRepository(fixture)))
    assert client.get("/api/listings").json()["total"] == 1
    walking = client.get("/api/listings/900001/accessibility?mode=walking").json()
    assert walking["result_type"] == "straight_line_fallback"


def test_postgres_repository_is_lazy_and_has_parameterized_queries() -> None:
    repository = PostgresAccessibilityRepository("postgresql://not-contacted.invalid/test")
    assert repository.database_url.endswith("/test")
    source = Path("backend/accessibility_repository.py").read_text(encoding="utf-8")
    assert "origin_latitude between %s and %s" in source
    assert "on conflict (cache_identity) where not is_stale" in source
    assert "requests." not in source
