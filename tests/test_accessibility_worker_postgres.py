from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.accessibility_inputs import ReviewedProperty, VerifiedHotspot
from backend.accessibility_repository import PostgresAccessibilityRepository
from backend.accessibility_worker import AccessibilityWorker, WorkUnit, build_work_units, period_departures
from backend.domain import (
    Coordinates,
    Hotspot,
    ProviderMetadata,
    RouteItinerary,
    RouteLeg,
    RouteResult,
    RouteStop,
    RoutingDiagnostic,
    TravelMode,
    TravelStatus,
    TravelTimeSample,
)
from backend.routing_provider import FixtureAccessibilityRoutingProvider, RoutingGraphMetadata


pytestmark = pytest.mark.postgres
NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
SERVICE_WEEK = datetime(2026, 9, 14, tzinfo=timezone.utc)
GRAPH = RoutingGraphMetadata("otp-test", "network-v1", "schedule-v1", NOW)


def _property(connection, match_key: str = "accessibility-worker-property") -> ReviewedProperty:
    property_id = connection.execute(
        """
        insert into public.housing_properties (
            normalized_address, display_address, latitude, longitude,
            geocode_status, address_complete, match_key
        ) values (%s, %s, 43.0, -81.25, 'ok', true, %s)
        returning id
        """,
        (match_key, match_key, match_key),
    ).fetchone()[0]
    return ReviewedProperty(
        property_id, match_key, Coordinates(43.0, -81.25), "approved"
    )


def _hotspot() -> VerifiedHotspot:
    return VerifiedHotspot(
        Hotspot(
            "western-main-campus",
            "Western main campus",
            Coordinates(43.0096, -81.2737),
            category="campus",
            source="postgres-fixture",
        ),
        "verified",
        NOW,
    )


def _route(mode: TravelMode) -> RouteResult:
    provider_mode = "WALK" if mode is TravelMode.WALKING else "BICYCLE"
    return RouteResult(
        Coordinates(43.0, -81.25),
        Coordinates(43.0096, -81.2737),
        mode,
        2400,
        1800 if mode is TravelMode.WALKING else 600,
        TravelStatus.AVAILABLE,
        ProviderMetadata("fixture", "exact_route", NOW),
        False,
        1.0,
        provider_mode=provider_mode.lower(),
        itinerary=RouteItinerary(
            legs=(
                RouteLeg(
                    provider_mode,
                    1800 if mode is TravelMode.WALKING else 600,
                    2400,
                    encoded_polyline="_p~iF~ps|U_ulLnnqC_mqNvxq`@",
                    geometry_point_count=3,
                ),
            ),
            transfer_count=0,
        ),
    )


def _provider(graph=GRAPH, *, samples=None):
    return FixtureAccessibilityRoutingProvider(
        graph,
        routes={mode: _route(mode) for mode in (TravelMode.WALKING, TravelMode.CYCLING)},
        samples=samples,
    )


def _worker(repository, fixture, graph=GRAPH) -> AccessibilityWorker:
    return AccessibilityWorker(
        repository,
        fixture,
        provider_profile="postgres-worker-fixture-v1",
        graph_metadata=graph,
        reference_service_week=SERVICE_WEEK,
        minimum_transit_samples=2,
        max_retries=0,
        persist=True,
        now=lambda: NOW,
        sleeper=lambda _: None,
    )


def _samples(unit: WorkUnit, count: int = 3):
    departures = period_departures(unit.period, SERVICE_WEEK)
    return {
        departure: TravelTimeSample(
            departure,
            1500 + index * 60,
            walking_duration_seconds=300,
            transfer_count=0,
            distance_meters=5200,
            itinerary=RouteItinerary(
                legs=(
                    RouteLeg(
                        "BUS",
                        900,
                        4800,
                        encoded_polyline="_p~iF~ps|U_ulLnnqC_mqNvxq`@",
                        geometry_point_count=3,
                        route_id="route-27",
                        route_short_name="27",
                        from_stop=RouteStop("stop-a", "University & Sunset"),
                        to_stop=RouteStop("stop-b", "Natural Sciences"),
                        scheduled_departure_at=departure,
                        scheduled_arrival_at=departure + timedelta(minutes=15),
                    ),
                ),
                departure_at=departure,
                arrival_at=departure + timedelta(minutes=15),
                transfer_count=0,
            ),
        )
        for index, departure in enumerate(departures[:count])
    }


def _walking_better_samples(unit: WorkUnit):
    return {
        departure: TravelTimeSample(
            departure,
            None,
            status=TravelStatus.UNAVAILABLE,
            routing_diagnostics=(
                RoutingDiagnostic(
                    "walking_better_than_transit",
                    "Origin is within a trivial distance of the destination.",
                ),
            ),
        )
        for departure in period_departures(unit.period, SERVICE_WEEK)
    }


def _mixed_deterministic_samples(unit: WorkUnit):
    departures = period_departures(unit.period, SERVICE_WEEK)
    return {
        departures[0]: TravelTimeSample(
            departures[0],
            None,
            status=TravelStatus.UNAVAILABLE,
            routing_diagnostics=(RoutingDiagnostic("no_route", "No route."),),
        ),
        departures[1]: TravelTimeSample(
            departures[1],
            1500,
            walking_duration_seconds=300,
            transfer_count=0,
            distance_meters=5200,
            status=TravelStatus.AVAILABLE,
        ),
        departures[2]: TravelTimeSample(
            departures[2],
            None,
            status=TravelStatus.UNAVAILABLE,
            routing_diagnostics=(
                RoutingDiagnostic(
                    "walking_better_than_transit",
                    "Walking is better than transit.",
                ),
            ),
        ),
    }


def test_worker_creates_walk_cycle_profiles_and_cache_is_idempotent(
    postgres_database, postgres_target
) -> None:
    property_ = _property(postgres_database)
    hotspot = _hotspot()
    repository = PostgresAccessibilityRepository(postgres_target.url)
    fixture = _provider()
    service = _worker(repository, fixture)
    for mode in (TravelMode.WALKING, TravelMode.CYCLING):
        outcome = service.process(WorkUnit(property_, hotspot, mode), worker_run_id="pg-run-1")
        assert outcome.profile.quality_status == "complete"
    repeated = service.process(
        WorkUnit(property_, hotspot, TravelMode.WALKING), worker_run_id="pg-run-2"
    )
    assert repeated.action == "cache_hit"
    assert repeated.profile.route_itinerary is not None
    assert repeated.profile.route_itinerary.legs[0].has_geometry is True
    rows = postgres_database.execute(
        """
        select travel_mode, provider_metadata ->> 'worker_run_id',
               provider_metadata ->> 'quality_status'
        from public.housing_accessibility_profiles order by travel_mode
        """
    ).fetchall()
    assert rows == [
        ("cycling", "pg-run-1", "complete"),
        ("walking", "pg-run-1", "complete"),
    ]


def test_worker_stores_transit_samples_and_partial_resume_only_requests_missing(
    postgres_database, postgres_target
) -> None:
    property_ = _property(postgres_database)
    hotspot = _hotspot()
    unit = build_work_units([property_], [hotspot], (TravelMode.TRANSIT,))[0]
    all_samples = _samples(unit)
    first_departure = next(iter(all_samples))
    repository = PostgresAccessibilityRepository(postgres_target.url)
    first = _worker(
        repository, _provider(samples={first_departure: all_samples[first_departure]})
    ).process(unit, worker_run_id="pg-partial")
    assert first.profile.is_stale is True
    remaining = {key: value for key, value in all_samples.items() if key != first_departure}
    second_provider = _provider(samples=remaining)
    second = _worker(repository, second_provider).process(unit, worker_run_id="pg-resume")
    assert second.profile.sample_count == 3
    assert second.profile.is_stale is False
    requested = [call for call in second_provider.calls if call[0] == "samples"][0][1]
    assert len(requested) == 2
    loaded_samples = repository.load_samples(second.profile.profile_id)
    assert len(loaded_samples) == 3
    assert second.profile.representative_sample_departure_at is not None
    assert loaded_samples[0].itinerary is not None
    assert loaded_samples[0].itinerary.legs[0].route_short_name == "27"
    assert postgres_database.execute(
        "select count(*) from public.housing_accessibility_profiles"
    ).fetchone()[0] == 2
    assert postgres_database.execute(
        """
        select count(*) from public.housing_accessibility_samples
        where provider_metadata ->> 'worker_run_id' = 'pg-resume'
        """
    ).fetchone()[0] == 3


def test_walking_better_than_transit_is_persistent_and_cache_idempotent(
    postgres_database, postgres_target
) -> None:
    property_ = _property(postgres_database)
    hotspot = _hotspot()
    unit = build_work_units([property_], [hotspot], (TravelMode.TRANSIT,))[0]
    repository = PostgresAccessibilityRepository(postgres_target.url)
    first_service = _worker(
        repository, _provider(samples=_walking_better_samples(unit))
    )

    first = first_service.process(unit, worker_run_id="pg-walking-alternative")

    assert first.profile.is_stale is False
    assert first.profile.quality_status == "no_route"
    assert first.profile.quality_reason_codes == ("walking_better_than_transit",)
    assert first.quality.decision == "accepted_with_warning"
    assert len(repository.load_samples(first.profile.profile_id)) == 3

    repeated_provider = _provider()
    repeated_service = _worker(repository, repeated_provider)
    repeated = repeated_service.process(unit, worker_run_id="pg-walking-cache")

    assert repeated.action == "cache_hit"
    assert repeated.quality.decision == "accepted_with_warning"
    assert repeated_service.metrics.provider_calls == 0
    assert repeated_service.metrics.database_writes == 0
    assert repeated_provider.calls == []
    assert postgres_database.execute(
        "select count(*) from public.housing_accessibility_profiles where not is_stale"
    ).fetchone()[0] == 1
    assert postgres_database.execute(
        "select count(*) from public.housing_accessibility_samples"
    ).fetchone()[0] == 3


def test_mixed_deterministic_transit_is_persistent_and_cache_idempotent(
    postgres_database, postgres_target
) -> None:
    property_ = _property(postgres_database, "mixed-deterministic-property")
    hotspot = _hotspot()
    unit = build_work_units([property_], [hotspot], (TravelMode.TRANSIT,))[0]
    repository = PostgresAccessibilityRepository(postgres_target.url)
    first_service = _worker(
        repository, _provider(samples=_mixed_deterministic_samples(unit))
    )

    first = first_service.process(unit, worker_run_id="pg-mixed-deterministic")

    assert first.profile.is_stale is False
    assert first.profile.sample_count == 1
    assert first.profile.quality_status == "insufficient_samples"
    assert first.profile.quality_reason_codes == (
        "insufficient_samples",
        "no_route",
        "walking_better_than_transit",
    )
    assert first.quality.decision == "manual_review_required"
    assert len(repository.load_samples(first.profile.profile_id)) == 3

    repeated_provider = _provider()
    repeated_service = _worker(repository, repeated_provider)
    repeated = repeated_service.process(unit, worker_run_id="pg-mixed-cache")

    assert repeated.action == "cache_hit"
    assert repeated.quality.decision == "manual_review_required"
    assert repeated.quality.reason_codes == first.quality.reason_codes
    assert repeated_service.metrics.provider_calls == 0
    assert repeated_service.metrics.database_writes == 0
    assert repeated_provider.calls == []
    assert postgres_database.execute(
        "select count(*) from public.housing_accessibility_profiles where not is_stale"
    ).fetchone()[0] == 1
    assert postgres_database.execute(
        "select count(*) from public.housing_accessibility_samples"
    ).fetchone()[0] == 3
    assert postgres_database.execute(
        """
        select count(*) from (
            select cache_identity from public.housing_accessibility_profiles
            where not is_stale group by cache_identity having count(*) > 1
        ) duplicates
        """
    ).fetchone()[0] == 0


def test_routing_diagnostics_round_trip_as_sanitized_sample_metadata(
    postgres_database, postgres_target
) -> None:
    property_ = _property(postgres_database)
    hotspot = _hotspot()
    unit = build_work_units([property_], [hotspot], (TravelMode.TRANSIT,))[0]
    samples = _samples(unit)
    departure = list(samples)[-1]
    samples[departure] = TravelTimeSample(
        departure,
        None,
        status=TravelStatus.UNAVAILABLE,
        routing_diagnostics=(
            RoutingDiagnostic(
                "no_transit_connection",
                "No transit connection was found.",
                "DATE_TIME",
            ),
        ),
    )
    repository = PostgresAccessibilityRepository(postgres_target.url)
    outcome = _worker(repository, _provider(samples=samples)).process(
        unit, worker_run_id="pg-routing-diagnostic"
    )
    loaded = repository.load_samples(outcome.profile.profile_id)
    unavailable = next(sample for sample in loaded if sample.status is TravelStatus.UNAVAILABLE)
    assert unavailable.duration_seconds is None
    assert unavailable.routing_diagnostics[0].code == "no_transit_connection"
    metadata = postgres_database.execute(
        """
        select provider_metadata::text
        from public.housing_accessibility_samples
        where profile_id = %s and departure_at = %s
        """,
        (outcome.profile.profile_id, departure),
    ).fetchone()[0]
    assert "no_transit_connection" in metadata
    assert "planConnection" not in metadata


def test_incompatible_network_creates_current_replacement_and_retains_history(
    postgres_database, postgres_target
) -> None:
    property_ = _property(postgres_database)
    hotspot = _hotspot()
    repository = PostgresAccessibilityRepository(postgres_target.url)
    _worker(repository, _provider()).process(
        WorkUnit(property_, hotspot, TravelMode.WALKING), worker_run_id="network-1"
    )
    graph2 = replace(GRAPH, network_version="network-v2")
    _worker(repository, _provider(graph2), graph2).process(
        WorkUnit(property_, hotspot, TravelMode.WALKING), worker_run_id="network-2"
    )
    rows = postgres_database.execute(
        "select network_version, is_stale from public.housing_accessibility_profiles order by id"
    ).fetchall()
    assert rows == [("network-v1", False), ("network-v2", False)]


def test_failed_profile_sample_transaction_preserves_prior_profile(
    postgres_database, postgres_target, monkeypatch
) -> None:
    property_ = _property(postgres_database)
    hotspot = _hotspot()
    repository = PostgresAccessibilityRepository(postgres_target.url)
    original = _worker(repository, _provider()).process(
        WorkUnit(property_, hotspot, TravelMode.WALKING), worker_run_id="before-failure"
    ).profile
    postgres_database.execute(
        "update public.housing_accessibility_profiles set expires_at = %s where id = %s",
        (NOW, original.profile_id),
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected sample persistence failure")

    monkeypatch.setattr(repository, "_save_samples", fail)
    with pytest.raises(RuntimeError, match="injected"):
        _worker(repository, _provider()).process(
            WorkUnit(property_, hotspot, TravelMode.WALKING), worker_run_id="failed-run"
        )
    rows = postgres_database.execute(
        "select id, is_stale, provider_metadata ->> 'worker_run_id' "
        "from public.housing_accessibility_profiles"
    ).fetchall()
    assert rows == [(original.profile_id, False, "before-failure")]
