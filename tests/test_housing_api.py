from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.domain import Coordinates, Location, RouteRequest, TravelMode, TravelStatus
from backend.main import create_app
from backend.providers import (
    FixtureRoutingProvider,
    StraightLineEstimateProvider,
    UnavailableTransitProvider,
    haversine_distance_meters,
)
from backend.ranking import RankingWeights, score_listings
from backend.repository import (
    InMemoryListingRepository,
    ListingQuery,
    PostgresListingRepository,
)


@pytest.fixture
def listings() -> list[dict[str, object]]:
    common = {
        "listing_url": "https://offcampus.uwo.ca/Listings/Details/1",
        "lease_type": "standard",
        "furnished": True,
        "utilities_included": True,
        "parking_available": False,
        "laundry": True,
        "dishwasher": False,
        "air_conditioning": False,
        "geocode_status": "ok",
        "geocode_confidence": 0.95,
        "first_seen_at": "2026-07-15T00:00:00Z",
        "last_seen_at": "2026-08-01T00:00:00Z",
        "review_flags": [],
    }
    return [
        {
            **common,
            "listing_id": "a",
            "title": "Affordable summer room",
            "description": "Full private detail for A",
            "address": "1 Test Street",
            "price_monthly": 700,
            "price_text": "$700 per month",
            "bedrooms": 1,
            "housing_type": "room",
            "preferred_gender": "male_preferred",
            "is_sublet": False,
            "availability_category": "summer_only",
            "latitude": 43.01,
            "longitude": -81.27,
            "map_ready": True,
            "amenities": ["Laundry"],
        },
        {
            **common,
            "listing_id": "b",
            "title": "Explicit sublet apartment",
            "address": "2 Test Street",
            "price_monthly": 1200,
            "bedrooms": 2,
            "housing_type": "apartment",
            "preferred_gender": "female_preferred",
            "lease_type": "sublet",
            "is_sublet": True,
            "availability_category": "fall_start",
            "latitude": 43.025,
            "longitude": -81.29,
            "map_ready": True,
            "parking_available": True,
        },
        {
            **common,
            "listing_id": "c",
            "title": "Coordinate pending room",
            "address": None,
            "price_monthly": 850,
            "bedrooms": 1,
            "housing_type": "room",
            "preferred_gender": "any",
            "is_sublet": False,
            "availability_category": None,
            "latitude": None,
            "longitude": None,
            "map_ready": False,
            "parking_available": None,
            "laundry": None,
            "geocode_status": "missing_address",
            "needs_manual_review": True,
            "review_flags": ["missing_address"],
        },
        {
            **common,
            "listing_id": "d",
            "title": "Suspicious price",
            "address": "4 Test Street",
            "price_monthly": 1,
            "bedrooms": 1,
            "housing_type": "room",
            "preferred_gender": "not_specified",
            "is_sublet": False,
            "availability_category": "fall_start",
            "latitude": 43.04,
            "longitude": -81.3,
            "map_ready": True,
        },
    ]


@pytest.fixture
def client(listings: list[dict[str, object]]) -> TestClient:
    return TestClient(create_app(repository=InMemoryListingRepository(listings)))


def test_listing_pagination_is_bounded_and_reports_total(client: TestClient) -> None:
    response = client.get("/api/listings?page=2&page_size=2&sort=price_low")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    assert body["page"] == 2
    assert len(body["listings"]) == 2
    assert "description" not in body["listings"][0]


def test_filtered_map_results_ignore_sidebar_pagination(client: TestClient) -> None:
    first = client.get("/api/listings/map?page=1&page_size=1").json()
    later = client.get("/api/listings/map?page=99&page_size=2").json()

    assert first == later
    assert first["count"] == 3
    assert {row["listing_id"] for row in first["listings"]} == {"a", "b", "d"}


def test_postgres_map_query_is_one_compact_unpaginated_database_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[object]]] = []

    class Result:
        @staticmethod
        def fetchall() -> list[dict[str, object]]:
            return [{"listing_id": "marker"}]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def execute(sql: str, parameters: list[object]) -> Result:
            calls.append((sql, parameters))
            return Result()

    repository = PostgresListingRepository("postgresql://test.invalid/db")
    monkeypatch.setattr(repository, "_connect", lambda: Connection())

    rows = repository.query_map_markers(
        ListingQuery(housing_type="room", laundry=True, offset=200, limit=1)
    )

    assert rows == [{"listing_id": "marker"}]
    assert len(calls) == 1
    sql, parameters = calls[0]
    assert "location_map_visible" in sql
    assert "latitude is not null" in sql and "longitude is not null" in sql
    assert "offset" not in sql.casefold() and "limit" not in sql.casefold()
    assert "description" not in sql and "provenance_data" not in sql
    assert parameters == [True, "room"]


@pytest.mark.parametrize(
    "query",
    [
        "min_price=800&max_price=1200",
        "bedrooms=1",
        "housing_type=apartment",
        "is_sublet=true",
        "summer_available=true",
        "preferred_gender=male_preferred",
        "furnished=true&utilities_included=true&laundry=true",
        "map_ready=true",
    ],
)
def test_filtered_map_results_match_map_visible_collection_identities(
    client: TestClient, query: str
) -> None:
    collection = client.get(f"/api/listings?{query}&page_size=200").json()
    expected = {
        row["listing_id"]
        for row in collection["listings"]
        if row["location"]["map_visible"]
    }
    map_body = client.get(f"/api/listings/map?{query}").json()
    map_ids = [row["listing_id"] for row in map_body["listings"]]

    assert set(map_ids) == expected
    assert map_body["count"] == len(expected)
    assert len(map_ids) == len(set(map_ids))
    assert all(row["location"]["map_visible"] for row in map_body["listings"])


def test_map_marker_payload_is_lightweight_and_map_ready_false_is_empty(
    client: TestClient,
) -> None:
    marker = client.get("/api/listings/map").json()["listings"][0]
    assert set(marker) == {
        "listing_id",
        "title",
        "address",
        "price_monthly",
        "housing_type",
        "latitude",
        "longitude",
        "location",
    }
    assert client.get("/api/listings/map?map_ready=false").json() == {
        "count": 0,
        "listings": [],
    }


def test_listing_ids_return_a_shortlist_independently_of_other_inventory(
    client: TestClient,
) -> None:
    response = client.get("/api/listings?listing_ids=a,c&page_size=100")

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert {row["listing_id"] for row in response.json()["listings"]} == {"a", "c"}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("min_price=800&max_price=900", {"c"}),
        ("bedrooms=2", {"b"}),
        ("housing_type=apartment", {"b"}),
        ("is_sublet=true", {"b"}),
        ("summer_available=true", {"a"}),
        ("preferred_gender=male_preferred", {"a"}),
        ("preferred_gender=female_preferred", {"b"}),
        ("map_ready=false", {"c"}),
        ("furnished=true&utilities_included=true&laundry=true", {"a", "b", "d"}),
    ],
)
def test_normalized_listing_filters(client: TestClient, query: str, expected: set[str]) -> None:
    response = client.get(f"/api/listings?{query}&page_size=200")
    assert response.status_code == 200
    assert {row["listing_id"] for row in response.json()["listings"]} == expected


def test_positive_amenity_filters_exclude_explicitly_unavailable_and_unknown_values(
    client: TestClient,
) -> None:
    laundry = client.get("/api/listings?laundry=true&page_size=200")
    parking = client.get("/api/listings?parking_available=true&page_size=200")

    assert {row["listing_id"] for row in laundry.json()["listings"]} == {"a", "b", "d"}
    assert {row["listing_id"] for row in parking.json()["listings"]} == {"b"}


def test_summer_availability_does_not_imply_sublet(client: TestClient) -> None:
    summer = client.get("/api/listings?summer_available=true").json()["listings"]
    assert summer[0]["listing_id"] == "a"
    assert summer[0]["is_sublet"] is False
    assert client.get("/api/listings?summer_available=true&is_sublet=true").json()["total"] == 0


def test_summer_projection_preserves_explicit_false_and_unknown_categories() -> None:
    from backend.main import _summer_available

    assert _summer_available({"availability_category": "non_summer"}) is False
    assert _summer_available({"availability_category": "fall_start"}) is None
    assert _summer_available({"availability_category": None}) is None


def test_fidelity_spot_check_preserves_preferences_unknowns_and_lease_types(
    client: TestClient,
) -> None:
    rows = {
        row["listing_id"]: row
        for row in client.get("/api/listings?page_size=200").json()["listings"]
    }

    assert rows["a"]["preferred_gender"] == "male_preferred"
    assert rows["a"]["lease_type"] == "standard"
    assert rows["b"]["preferred_gender"] == "female_preferred"
    assert rows["b"]["lease_type"] == "sublet"
    assert rows["c"]["availability_category"] is None


def test_api_does_not_convert_missing_furnishing_or_utilities_to_false() -> None:
    listing = {
        "listing_id": "unknowns",
        "listing_url": "https://offcampus.uwo.ca/Listings/Details/999",
        "title": "Unresolved source fields",
        "address": "9 Test Street",
        "furnished": None,
        "utilities_included": None,
        "utilities_status": None,
        "map_ready": False,
    }
    unknown_client = TestClient(
        create_app(repository=InMemoryListingRepository([listing]))
    )

    row = unknown_client.get("/api/listings").json()["listings"][0]

    assert row["furnished"] is None
    assert row["utilities_included"] is None
    assert row["utilities_status"] is None


def test_coordinate_less_listing_stays_in_general_results(client: TestClient) -> None:
    body = client.get("/api/listings?page_size=200").json()
    row = next(row for row in body["listings"] if row["listing_id"] == "c")
    assert row["map_ready"] is False
    assert row["selected_hotspot_distance_km"] is None


def test_explicit_unavailable_location_suppresses_coordinates_and_routes() -> None:
    listing = {
        "listing_id": "unsafe",
        "listing_url": "https://offcampus.uwo.ca/Listings/Details/unsafe",
        "title": "Location review listing",
        "address": "10 Test Street",
        "property_id": 10,
        "latitude": 43.02,
        "longitude": -81.28,
        "map_ready": True,
        "location_status": "unavailable",
        "location_map_visible": False,
        "location_route_available": False,
        "location_reason_codes": ["city_reference_disagreement"],
    }
    unsafe_client = TestClient(
        create_app(repository=InMemoryListingRepository([listing]))
    )

    summary = unsafe_client.get("/api/listings").json()["listings"][0]
    detail = unsafe_client.get("/api/listings/unsafe").json()

    assert summary["location"]["status"] == "unavailable"
    assert summary["location"]["map_visible"] is False
    assert summary["location"]["route_available"] is False
    assert summary["latitude"] is None and summary["longitude"] is None
    assert detail["latitude"] is None and detail["longitude"] is None
    assert unsafe_client.get("/api/listings?map_ready=true").json()["total"] == 0
    map_response = unsafe_client.get("/api/listings/map")
    assert map_response.json() == {"count": 0, "listings": []}
    assert "43.02" not in map_response.text and "-81.28" not in map_response.text
    assert unsafe_client.get(
        "/api/listings/unsafe/accessibility?mode=walking"
    ).status_code == 422


def test_hotspot_endpoint_and_invalid_hotspot(client: TestClient) -> None:
    hotspots = client.get("/api/hotspots").json()
    assert hotspots["count"] == 1
    assert hotspots["hotspots"][0]["coordinate_status"] == "verified"
    assert client.get("/api/listings?hotspot_id=missing").status_code == 404


def test_inactive_unverified_hotspots_are_explicitly_available_for_configuration(client: TestClient) -> None:
    body = client.get("/api/hotspots?include_inactive=true").json()
    pending = [row for row in body["hotspots"] if not row["is_active"]]
    assert pending
    assert all(row["latitude"] is None and row["coordinate_status"] == "pending_verification" for row in pending)


def test_accessibility_estimates_walk_and_cycle_but_not_transit(client: TestClient) -> None:
    body = client.get("/api/listings/a/accessibility").json()
    results = {row["mode"]: row for row in body["results"]}
    assert results["walking"]["status"] == "estimated"
    assert results["cycling"]["duration_seconds"] < results["walking"]["duration_seconds"]
    assert results["walking"]["distance_type"] == "straight_line"
    assert results["transit"]["status"] == "pending_provider"
    assert results["transit"]["duration_seconds"] is None
    assert results["driving"]["duration_seconds"] is None


def test_missing_listing_coordinates_are_not_zero(client: TestClient) -> None:
    results = client.get("/api/listings/c/accessibility").json()["results"]
    assert all(result["status"] == "invalid_origin" for result in results)
    assert all(result["distance_meters"] is None for result in results)


def test_detail_includes_description_and_quality_metadata(client: TestClient) -> None:
    detail = client.get("/api/listings/a").json()
    assert detail["description"] == "Full private detail for A"
    assert detail["field_quality"]["price_monthly"]["status"] == "confirmed"
    needs_review = client.get("/api/listings/c").json()
    assert needs_review["data_quality"]["status"] == "needs-review"


def test_freshness_summary_is_batched_and_detail_history_is_source_safe(
    listings: list[dict[str, object]],
) -> None:
    class CountingHistoryRepository(InMemoryListingRepository):
        history_calls = 0

        def list_observations(self, listing_id: str, *, limit: int = 51):
            self.history_calls += 1
            return super().list_observations(listing_id, limit=limit)

    observations = {
        "a": [
            {
                "id": 1,
                "observed_at": "2026-07-15T00:00:00Z",
                "change_type": "new",
                "title": "Room for $700",
                "description": "Full private detail for A",
                "availability_text": "Summer",
                "comparison_data": {"price_monthly": 700, "lease_type": "short_term"},
            },
            {
                "id": 2,
                "observed_at": "2026-08-01T00:00:00Z",
                "change_type": "updated",
                "title": "Room for $650",
                "description": "Full private detail for A",
                "availability_text": "Summer",
                "comparison_data": {"price_monthly": 650, "lease_type": "standard"},
            },
        ]
    }
    repository = CountingHistoryRepository(listings, observations=observations)
    history_client = TestClient(create_app(repository=repository))

    summary = history_client.get("/api/listings?page_size=20").json()["listings"][0]
    assert summary["freshness"] == {
        "first_observed_at": "2026-07-15T00:00:00Z",
        "last_observed_at": "2026-08-01T00:00:00Z",
    }
    assert repository.history_calls == 0

    detail = history_client.get("/api/listings/a").json()
    assert repository.history_calls == 1
    assert detail["freshness"]["last_meaningful_source_change_at"] == (
        "2026-08-01T00:00:00Z"
    )
    assert [event["type"] for event in detail["history"]["events"]] == [
        "PRICE_CHANGED"
    ]

    endpoint = history_client.get("/api/listings/a/history")
    assert endpoint.status_code == 200
    assert endpoint.json()["events"] == detail["history"]["events"]
    assert history_client.get("/api/listings/missing/history").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/listings?min_price=1000&max_price=500",
        "/api/listings?page=0",
        "/api/listings?page_size=201",
        "/api/listings?data_quality=perfect",
        "/api/listings?max_walk_minutes=0",
        "/api/listings?max_transit_minutes=181",
        "/api/hotspots?category=restaurant",
    ],
)
def test_invalid_queries_return_clear_validation_errors(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 422


def test_haversine_identical_known_and_invalid_coordinates() -> None:
    western = Coordinates(43.0096, -81.2737)
    assert haversine_distance_meters(western, western) == 0
    london_example = Coordinates(43.02, -81.27)
    assert 1_100 <= haversine_distance_meters(western, london_example) <= 1_300
    with pytest.raises(ValueError, match="latitude"):
        Coordinates(91, -81.2)
    with pytest.raises(ValueError, match="longitude"):
        Coordinates(43, -181)


def test_straight_line_rounding_and_configurable_speeds() -> None:
    fixed_now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    origin = Location("a", "A", Coordinates(43.0096, -81.2737))
    destination = Location("b", "B", Coordinates(43.02, -81.27))
    provider = StraightLineEstimateProvider(
        walking_speed_kmh=5,
        cycling_speed_kmh=10,
        now=lambda: fixed_now,
    )
    results = provider.get_travel_times(origin, [destination], [TravelMode.WALKING, TravelMode.CYCLING])
    assert results[0].distance_meters == results[1].distance_meters
    assert results[0].duration_seconds == results[1].duration_seconds * 2
    assert results[0].to_dict()["calculated_at"] == "2026-08-01T00:00:00+00:00"


def test_unavailable_transit_and_fixture_routing_are_replaceable() -> None:
    origin = Coordinates(43.0, -81.2)
    destination = Coordinates(43.1, -81.3)
    route = FixtureRoutingProvider({TravelMode.WALKING: (1000, 700)}).get_route(
        RouteRequest(origin, destination, TravelMode.WALKING)
    )
    assert route.status is TravelStatus.AVAILABLE
    assert route.metadata.provider == "fixture"
    pending = UnavailableTransitProvider().get_travel_times(
        Location("a", "A", origin),
        [Location("b", "B", destination)],
        [TravelMode.TRANSIT],
    )[0]
    assert pending.status is TravelStatus.PENDING_PROVIDER
    assert pending.duration_seconds is None


def test_ranking_is_deterministic_and_explainable(listings: list[dict[str, object]]) -> None:
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    first = score_listings(listings, now=now)
    second = score_listings(listings, now=now)
    assert first == second
    assert first[0]["ranking"]["is_subjective"] is True
    assert set(first[0]["ranking"]["weights"]) == {
        "price", "distance", "amenities", "convenience", "data_quality", "freshness"
    }


def test_lower_comparable_price_and_shorter_distance_improve_scores() -> None:
    rows = [
        {"listing_id": "low", "housing_type": "room", "bedrooms": 1, "price_monthly": 700, "selected_hotspot_distance_km": 1, "map_ready": True, "listing_url": "x"},
        {"listing_id": "high", "housing_type": "room", "bedrooms": 1, "price_monthly": 1000, "selected_hotspot_distance_km": 4, "map_ready": True, "listing_url": "x"},
    ]
    scores = {row["listing_id"]: row["ranking"] for row in score_listings(rows)}
    assert scores["low"]["price_score"] > scores["high"]["price_score"]
    assert scores["low"]["distance_score"] > scores["high"]["distance_score"]


def test_missing_and_suspicious_values_are_not_rewarded() -> None:
    rows = [
        {"listing_id": "missing", "housing_type": "room", "bedrooms": 1, "price_monthly": None},
        {"listing_id": "suspicious", "housing_type": "room", "bedrooms": 1, "price_monthly": 1},
    ]
    scores = {row["listing_id"]: row["ranking"] for row in score_listings(rows)}
    assert scores["missing"]["price_score"] < 50
    assert scores["missing"]["amenity_score"] < 50
    assert scores["suspicious"]["price_score"] == 0


def test_ranking_weight_changes_are_predictable() -> None:
    rows = [
        {"listing_id": "cheap", "housing_type": "room", "bedrooms": 1, "price_monthly": 500, "selected_hotspot_distance_km": 8},
        {"listing_id": "close", "housing_type": "room", "bedrooms": 1, "price_monthly": 1000, "selected_hotspot_distance_km": 0.2},
    ]
    price_only = score_listings(rows, weights=RankingWeights(price=1, distance=0, amenities=0, convenience=0, data_quality=0, freshness=0))
    distance_only = score_listings(rows, weights=RankingWeights(price=0, distance=1, amenities=0, convenience=0, data_quality=0, freshness=0))
    assert price_only[0]["ranking"]["overall_score"] > price_only[1]["ranking"]["overall_score"]
    assert distance_only[1]["ranking"]["overall_score"] > distance_only[0]["ranking"]["overall_score"]


def test_legacy_map_endpoint_remains_compatible(client: TestClient) -> None:
    response = client.get("/listings/map?limit=20")
    assert response.status_code == 200
    assert {row["listing_id"] for row in response.json()["listings"]} == {"a", "b", "d"}
