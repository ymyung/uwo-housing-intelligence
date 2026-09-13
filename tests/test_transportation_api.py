from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import backend.main as main_module
from backend.accessibility_repository import InMemoryAccessibilityRepository
from backend.domain import (
    AccessibilityOrigin,
    AccessibilityProfile,
    AccessibilityResultType,
    Coordinates,
    DayType,
    OriginType,
    RouteItinerary,
    RouteLeg,
    RouteStop,
    TimePeriod,
    TravelMode,
    TravelStatus,
    TravelTimeSample,
)
from backend.main import create_app
from backend.repository import InMemoryListingRepository
from backend.transportation import profile_summary


NOW = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
POLYLINE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"


class CountingAccessibilityRepository(InMemoryAccessibilityRepository):
    def __init__(self, profiles: list[AccessibilityProfile]) -> None:
        super().__init__(profiles)
        self.batch_reads = 0
        self.sample_reads = 0

    def find_current_property_profiles(self, *args, **kwargs):
        self.batch_reads += 1
        return super().find_current_property_profiles(*args, **kwargs)

    def load_samples(self, profile_id: int) -> list[TravelTimeSample]:
        self.sample_reads += 1
        return super().load_samples(profile_id)


def origin() -> AccessibilityOrigin:
    return AccessibilityOrigin(
        Coordinates(43.0, -81.25),
        property_id=1,
        origin_type=OriginType.PROPERTY,
    )


def direct_itinerary(mode: str) -> RouteItinerary:
    return RouteItinerary(
        (RouteLeg(mode, 600, 1800, POLYLINE, 3),),
        departure_at=NOW,
        arrival_at=NOW + timedelta(minutes=10),
    )


def transit_itinerary(*, transfer: bool = False) -> RouteItinerary:
    board = RouteStop("1:BOARD", "Richmond at Oxford", Coordinates(43.0, -81.25))
    transfer_stop = RouteStop(
        "1:TRANSFER", "Downtown transfer", Coordinates(43.004, -81.26)
    )
    exit_stop = RouteStop("1:EXIT", "Natural Sciences", Coordinates(43.009, -81.274))
    legs = [
        RouteLeg("WALK", 240, 300, POLYLINE, 3, to_stop=board),
        RouteLeg(
            "BUS",
            600,
            3000,
            POLYLINE,
            3,
            route_id="1:13",
            route_short_name="13",
            route_long_name="Route 13",
            from_stop=board,
            to_stop=transfer_stop if transfer else exit_stop,
            scheduled_departure_at=NOW + timedelta(minutes=5),
            scheduled_arrival_at=NOW + timedelta(minutes=15),
        ),
    ]
    if transfer:
        legs.append(
            RouteLeg(
                "BUS",
                420,
                1800,
                POLYLINE,
                3,
                route_id="1:27",
                route_short_name="27",
                from_stop=transfer_stop,
                to_stop=exit_stop,
                scheduled_departure_at=NOW + timedelta(minutes=18),
                scheduled_arrival_at=NOW + timedelta(minutes=25),
            )
        )
    legs.append(RouteLeg("WALK", 180, 220, POLYLINE, 3, from_stop=exit_stop))
    return RouteItinerary(
        tuple(legs),
        departure_at=NOW,
        arrival_at=NOW + timedelta(minutes=28),
        transfer_count=1 if transfer else 0,
        is_live=False,
    )


def profile(
    mode: TravelMode,
    *,
    profile_id: int,
    property_id: int = 1,
    period: TimePeriod | None = None,
    itinerary: RouteItinerary | None = None,
    duration: int | None = 1200,
    quality: str = "complete",
    reasons: tuple[str, ...] = (),
) -> AccessibilityProfile:
    return AccessibilityProfile(
        profile_id=profile_id,
        origin=AccessibilityOrigin(
            Coordinates(43.0, -81.25),
            property_id=property_id,
            origin_type=OriginType.PROPERTY,
        ),
        hotspot_id="western-main-campus",
        travel_mode=mode,
        day_type=DayType.WEEKDAY if period else None,
        time_period=period,
        representative_duration_seconds=duration,
        minimum_duration_seconds=duration,
        maximum_duration_seconds=duration,
        distance_meters=2200 if duration is not None else None,
        walking_duration_seconds=300 if mode is TravelMode.TRANSIT else duration,
        transfer_count=0 if mode is TravelMode.TRANSIT and duration is not None else None,
        nearest_stop_id="1:BOARD" if mode is TravelMode.TRANSIT else None,
        stop_to_destination_seconds=None,
        provider="opentripplanner",
        provider_profile="transport-v2",
        result_type=AccessibilityResultType.EXACT_ROUTE,
        confidence=0.95,
        sample_count=3 if mode is TravelMode.TRANSIT else 1,
        calculated_at=NOW,
        schedule_version="gtfs-v1" if mode is TravelMode.TRANSIT else None,
        network_version="osm-v1",
        expires_at=NOW + timedelta(days=365),
        requested_sample_count=3 if mode is TravelMode.TRANSIT else 1,
        quality_status=quality,
        quality_reason_codes=reasons,
        representative_sample_departure_at=NOW if mode is TravelMode.TRANSIT and duration else None,
        route_itinerary=itinerary if mode is not TravelMode.TRANSIT else None,
    )


def client_and_repository() -> tuple[TestClient, CountingAccessibilityRepository]:
    profiles = [
        profile(TravelMode.WALKING, profile_id=1, itinerary=direct_itinerary("WALK")),
        profile(
            TravelMode.CYCLING,
            profile_id=2,
            itinerary=direct_itinerary("BICYCLE"),
            duration=600,
        ),
        profile(
            TravelMode.TRANSIT,
            profile_id=3,
            period=TimePeriod.WEEKDAY_MORNING_COMMUTE,
            duration=1680,
        ),
    ]
    repository = CountingAccessibilityRepository(profiles)
    repository.save_samples(
        3,
        [
            TravelTimeSample(
                NOW,
                1680,
                walking_duration_seconds=420,
                transfer_count=0,
                distance_meters=3520,
                status=TravelStatus.AVAILABLE,
                itinerary=transit_itinerary(),
            )
        ],
    )
    listing = {
        "listing_id": "listing-1",
        "property_id": 1,
        "address": "1 Test Street",
        "latitude": 43.0,
        "longitude": -81.25,
        "map_ready": True,
        "price_monthly": 900,
    }
    app = create_app(
        repository=InMemoryListingRepository([listing]),
        accessibility_repository=repository,
        accessibility_provider_name="opentripplanner",
        accessibility_provider_profile="transport-v2",
        schedule_version="gtfs-v1",
        network_version="osm-v1",
    )
    return TestClient(app), repository


def test_collection_uses_one_compact_batch_read_and_never_loads_geometry() -> None:
    client, repository = client_and_repository()
    response = client.get("/api/listings?page_size=100")
    row = response.json()["listings"][0]
    assert row["transportation"]["walking"]["duration_minutes"] == 20
    assert row["transportation"]["cycling"]["duration_minutes"] == 10
    assert row["transportation"]["transit"]["duration_minutes"] == 28
    assert repository.batch_reads == 1
    assert repository.sample_reads == 0
    serialized = response.text
    assert POLYLINE not in serialized
    assert "encoded_polyline" not in serialized


def test_student_discovery_filters_use_persisted_profiles_and_compose() -> None:
    profiles = [
        profile(TravelMode.WALKING, profile_id=11, property_id=1, duration=900),
        profile(
            TravelMode.TRANSIT,
            profile_id=12,
            property_id=1,
            period=TimePeriod.WEEKDAY_MORNING_COMMUTE,
            duration=1200,
        ),
        profile(TravelMode.WALKING, profile_id=21, property_id=2, duration=1800),
        profile(
            TravelMode.TRANSIT,
            profile_id=22,
            property_id=2,
            period=TimePeriod.WEEKDAY_MORNING_COMMUTE,
            duration=2400,
        ),
    ]
    accessibility = CountingAccessibilityRepository(profiles)
    listings = [
        {
            "listing_id": "share-fast",
            "property_id": 1,
            "title": "House share",
            "housing_type": "house_to_share",
            "price_monthly": 800,
            "bedrooms": 4,
            "map_ready": True,
        },
        {
            "listing_id": "share-slow",
            "property_id": 2,
            "title": "Apartment share",
            "housing_type": "apartment_to_share",
            "price_monthly": 850,
            "bedrooms": 4,
            "map_ready": True,
        },
        {
            "listing_id": "text-only",
            "property_id": 3,
            "title": "Looking for a roommate",
            "description": "Roommate and tenant welcome",
            "housing_type": "house",
            "price_monthly": 825,
            "bedrooms": 4,
            "map_ready": True,
        },
        {
            "listing_id": "apartment-text-only",
            "property_id": 4,
            "title": "Apartment near friends",
            "description": "Roommate welcome",
            "housing_type": "apartment",
            "price_monthly": 875,
            "bedrooms": 4,
            "map_ready": True,
        },
    ]
    client = TestClient(
        create_app(
            repository=InMemoryListingRepository(listings),
            accessibility_repository=accessibility,
            accessibility_provider_name="opentripplanner",
            accessibility_provider_profile="transport-v2",
            schedule_version="gtfs-v1",
            network_version="osm-v1",
        )
    )

    roommates = client.get("/api/listings?roommates_wanted=true").json()
    assert {row["listing_id"] for row in roommates["listings"]} == {
        "share-fast",
        "share-slow",
    }
    exact_share = client.get(
        "/api/listings?roommates_wanted=true&housing_type=apartment_to_share"
    ).json()
    assert [row["listing_id"] for row in exact_share["listings"]] == [
        "share-slow"
    ]
    assert client.get(
        "/api/listings?roommates_wanted=true&housing_type=house"
    ).json()["total"] == 0

    walk = client.get("/api/listings?max_walk_minutes=20").json()
    assert [row["listing_id"] for row in walk["listings"]] == ["share-fast"]
    transit = client.get(
        "/api/listings?roommates_wanted=true&max_transit_minutes=30"
    ).json()
    assert [row["listing_id"] for row in transit["listings"]] == ["share-fast"]
    composed = client.get(
        "/api/listings?roommates_wanted=true&max_price=900&bedrooms=4&max_walk_minutes=35"
    ).json()
    assert {row["listing_id"] for row in composed["listings"]} == {
        "share-fast",
        "share-slow",
    }
    assert client.get("/api/listings").json()["total"] == 4
    assert accessibility.batch_reads == 7
    assert accessibility.sample_reads == 0


def test_collection_returns_unavailable_transportation_for_unprofiled_property() -> None:
    listing = {
        "listing_id": "rejected-listing",
        "property_id": 157,
        "address": "157 Review Queue Road",
        "latitude": 43.0,
        "longitude": -81.25,
        "map_ready": True,
    }
    app = create_app(
        repository=InMemoryListingRepository([listing]),
        accessibility_repository=InMemoryAccessibilityRepository(),
        accessibility_provider_name="opentripplanner",
        accessibility_provider_profile="transport-v2",
        schedule_version="gtfs-v1",
        network_version="osm-v1",
    )

    response = TestClient(app).get("/api/listings")

    assert response.status_code == 200
    transportation = response.json()["listings"][0]["transportation"]
    assert transportation["availability"] == "unavailable"
    assert transportation["walking"]["duration_minutes"] is None
    assert transportation["transit"]["high_walking_share"] is False


def test_local_accessibility_database_environment_selects_postgres_repository(
    monkeypatch,
) -> None:
    _, repository = client_and_repository()
    listing = {
        "listing_id": "listing-1",
        "property_id": 1,
        "address": "1 Test Street",
        "latitude": 43.0,
        "longitude": -81.25,
        "map_ready": True,
    }
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ACCESSIBILITY_DATABASE_URL", "postgresql://local/test")
    monkeypatch.setattr(
        main_module.PostgresAccessibilityRepository,
        "from_environment",
        classmethod(lambda _class: repository),
    )
    app = create_app(
        repository=InMemoryListingRepository([listing]),
        accessibility_provider_name="opentripplanner",
        accessibility_provider_profile="transport-v2",
        schedule_version="gtfs-v1",
        network_version="osm-v1",
    )

    response = TestClient(app).get("/api/listings")

    assert response.status_code == 200
    assert response.json()["listings"][0]["transportation"]["walking"][
        "duration_minutes"
    ] == 20


def test_overview_exposes_all_six_periods_without_geometry() -> None:
    client, repository = client_and_repository()
    body = client.get("/api/listings/listing-1/accessibility").json()
    transportation = body["transportation"]
    assert transportation["walking"]["duration_minutes"] == 20
    assert len(transportation["transit"]["periods"]) == 6
    assert transportation["schedule"]["live"] is False
    assert repository.sample_reads == 0
    assert "encoded_polyline" not in json.dumps(transportation)


def test_route_geometry_and_student_useful_transit_legs_load_on_demand() -> None:
    client, repository = client_and_repository()
    body = client.get(
        "/api/listings/listing-1/accessibility"
        "?mode=transit&time_period=weekday_morning_commute"
    ).json()
    route = body["route"]
    assert route["duration_minutes"] == 28
    assert route["itinerary"]["geometry_available"] is True
    bus = next(leg for leg in route["itinerary"]["legs"] if leg["mode"] == "BUS")
    assert bus["route_short_name"] == "13"
    assert bus["from_stop"]["name"] == "Richmond at Oxford"
    assert bus["to_stop"]["name"] == "Natural Sciences"
    assert route["transfer_count"] == 0
    assert route["itinerary"]["is_live"] is False
    assert repository.sample_reads == 1
    assert "planConnection" not in json.dumps(body)


def test_geometry_can_be_omitted_without_losing_itinerary_details() -> None:
    client, _ = client_and_repository()
    route = client.get(
        "/api/listings/listing-1/accessibility"
        "?mode=walking&include_geometry=false"
    ).json()["route"]
    assert route["itinerary"]["legs"][0]["encoded_polyline"] is None
    assert route["itinerary"]["legs"][0]["geometry_point_count"] == 3


def test_special_transit_outcomes_remain_distinct() -> None:
    wbt = profile(
        TravelMode.TRANSIT,
        profile_id=8,
        period=TimePeriod.WEEKDAY_LATE_EVENING,
        duration=None,
        quality="no_route",
        reasons=("walking_better_than_transit",),
    )
    no_route = profile(
        TravelMode.TRANSIT,
        profile_id=9,
        period=TimePeriod.SUNDAY_DAYTIME,
        duration=None,
        quality="no_route",
        reasons=("no_route",),
    )
    manual = profile(
        TravelMode.TRANSIT,
        profile_id=10,
        period=TimePeriod.SATURDAY_DAYTIME,
        quality="insufficient_samples",
        reasons=("insufficient_samples", "no_route"),
    )
    technical = profile(
        TravelMode.TRANSIT,
        profile_id=11,
        period=TimePeriod.WEEKDAY_MIDDAY,
        duration=None,
        quality="provider_error",
        reasons=("provider_error",),
    )
    assert profile_summary(wbt)["status"] == "walking_better_than_transit"
    assert profile_summary(no_route)["status"] == "no_route"
    assert profile_summary(manual)["status"] == "partial"
    assert profile_summary(manual)["review_required"] is True
    assert profile_summary(technical)["status"] == "technical_failure"
