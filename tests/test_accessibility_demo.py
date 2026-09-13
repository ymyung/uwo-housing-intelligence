from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.accessibility_repository import FixtureAccessibilityRepository
from backend.main import create_app
from backend.repository import FixtureCsvListingRepository


FIXTURE_ROOT = Path("tests/fixtures/accessibility_demo")
LISTINGS = FIXTURE_ROOT / "listings.csv"
PROFILES = FIXTURE_ROOT / "profiles.json"
FIXTURE_WARNING = (
    "The demo accessibility durations are deterministic fixture values for "
    "development and testing. They are not verified live routing results."
)


def demo_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("HOUSING_FIXTURE_CSV", str(LISTINGS))
    monkeypatch.setenv("ACCESSIBILITY_FIXTURE_PATH", str(PROFILES))
    # Fixture mode must win even when real adapters appear configured.
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-connect.invalid/fixture")
    monkeypatch.setenv("SUPABASE_URL", "https://must-not-connect.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fixture-not-a-secret")
    return TestClient(create_app())


def accessibility(
    client: TestClient,
    listing_id: str,
    mode: str,
    period: str | None = None,
):
    query = f"mode={mode}"
    if period:
        query += f"&time_period={period}"
    response = client.get(f"/api/listings/{listing_id}/accessibility?{query}")
    assert response.status_code == 200
    return response.json()


def test_demo_fixture_loads_without_database_or_supabase(monkeypatch) -> None:
    from backend import main

    monkeypatch.setattr(
        main.SupabaseListingRepository,
        "from_environment",
        lambda: (_ for _ in ()).throw(AssertionError("Supabase initialized")),
    )
    monkeypatch.setattr(
        main.PostgresAccessibilityRepository,
        "from_environment",
        lambda: (_ for _ in ()).throw(AssertionError("PostgreSQL initialized")),
    )
    client = demo_client(monkeypatch)
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/listings?page_size=20").json()["total"] == 6


def test_demo_fixture_profile_and_property_ids_match() -> None:
    listings = FixtureCsvListingRepository(LISTINGS).list_summaries()
    listing_property_ids = {
        int(row["property_id"])
        for row in listings
        if row.get("property_id") is not None
    }
    repository = FixtureAccessibilityRepository(PROFILES)
    assert {profile.origin.property_id for profile in repository.profiles} <= listing_property_ids
    assert {profile.profile_id for profile in repository.profiles} == {
        71001,
        71002,
        71003,
        71004,
    }
    assert len(repository.samples[71003]) == 3
    assert len(repository.reuse_history) == 2


def test_demo_profiles_contain_auditable_metadata() -> None:
    payload = json.loads(PROFILES.read_text(encoding="utf-8"))
    assert payload["warning"] == FIXTURE_WARNING
    required = {
        "result_type",
        "representative_duration_seconds",
        "minimum_duration_seconds",
        "maximum_duration_seconds",
        "distance_meters",
        "walking_duration_seconds",
        "transfer_count",
        "confidence",
        "provider",
        "calculated_at",
        "expires_at",
        "source_profile_id",
        "estimation_method",
        "reuse_explanation",
    }
    assert all(required <= profile.keys() for profile in payload["profiles"])


def test_demo_api_smoke_covers_exact_profiles_and_transit_range(monkeypatch) -> None:
    client = demo_client(monkeypatch)
    assert client.get("/api/hotspots").json()["hotspots"][0]["id"] == "western-main-campus"
    assert client.get("/api/accessibility/time-periods").json()["count"] == 6

    walking = accessibility(client, "demo-exact", "walking")
    cycling = accessibility(client, "demo-exact", "cycling")
    transit = accessibility(
        client,
        "demo-exact",
        "transit",
        "weekday_morning_commute",
    )
    assert walking["result_type"] == "cached_exact_property"
    assert walking["representative_duration_seconds"] == 1680
    assert walking["is_estimate"] is False
    assert cycling["result_type"] == "cached_exact_property"
    assert cycling["representative_duration_seconds"] == 660
    assert transit["result_type"] == "cached_exact_property"
    assert (
        transit["minimum_duration_seconds"],
        transit["representative_duration_seconds"],
        transit["maximum_duration_seconds"],
        transit["sample_count"],
    ) == (1260, 1500, 1740, 3)
    assert transit["fixture_notice"] == FIXTURE_WARNING
    assert "payload" not in transit and "database" not in transit


def test_demo_api_covers_nearby_same_stop_fallback_stale_and_unavailable(
    monkeypatch,
) -> None:
    client = demo_client(monkeypatch)
    nearby = accessibility(client, "demo-nearby", "walking")
    assert nearby["result_type"] == "nearby_origin_estimate"
    assert nearby["source_profile_id"] == 71001
    assert nearby["estimation_distance_meters"] > 3
    assert "connector" in nearby["estimation_method"]

    same_stop = accessibility(
        client,
        "demo-same-stop",
        "transit",
        "weekday_morning_commute",
    )
    assert same_stop["result_type"] == "same_stop_reuse"
    assert same_stop["source_profile_id"] == 71003
    assert same_stop["nearest_stop_id"] == "demo-stop-west"
    assert same_stop["schedule_version"] == "fixture-schedule-v1"
    assert "same stop" in same_stop["reuse_explanation"].lower()

    fallback = accessibility(client, "demo-fallback", "walking")
    assert fallback["result_type"] == "straight_line_fallback"
    assert fallback["is_estimate"] is True
    assert fallback["estimation_method"] == "haversine_speed_estimate"

    stale = accessibility(client, "demo-stale", "walking")
    assert stale["result_type"] == "stale"
    assert stale["freshness"] == "stale"
    assert stale["is_stale"] is True
    assert "must not be treated as current" in stale["reuse_explanation"]

    pending = accessibility(
        client,
        "demo-fallback",
        "transit",
        "weekday_morning_commute",
    )
    assert pending["result_type"] == "pending_provider"
    assert pending["representative_duration_seconds"] is None

    unavailable = accessibility(
        client,
        "demo-no-coordinates",
        "transit",
        "weekday_morning_commute",
    )
    assert unavailable["result_type"] == "unavailable"
    assert unavailable["representative_duration_seconds"] is None


def test_demo_api_validates_requests_without_requiring_period_for_walking(
    monkeypatch,
) -> None:
    client = demo_client(monkeypatch)
    assert client.get(
        "/api/listings/demo-exact/accessibility?mode=walking"
    ).status_code == 200
    assert client.get(
        "/api/listings/demo-exact/accessibility?mode=transit"
    ).status_code == 422
    assert client.get(
        "/api/listings/demo-exact/accessibility?mode=transit&time_period=invalid"
    ).status_code == 422
    assert client.get(
        "/api/listings/demo-exact/accessibility?mode=walking&hotspot_id=invalid"
    ).status_code == 404


def test_coordinate_less_demo_listing_remains_visible(monkeypatch) -> None:
    rows = demo_client(monkeypatch).get("/api/listings?page_size=20").json()["listings"]
    listing = next(row for row in rows if row["listing_id"] == "demo-no-coordinates")
    assert listing["map_ready"] is False
    assert listing["selected_hotspot_distance_meters"] is None
