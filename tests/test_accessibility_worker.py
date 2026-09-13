from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import backend.accessibility_runs as accessibility_runs
from backend.accessibility import AccessibilityResolver, TRANSIT_PROFILE_TTL
from backend.accessibility_inputs import (
    AccessibilityInputError,
    CoordinateBounds,
    ReviewedProperty,
    VerifiedHotspot,
    load_reviewed_properties,
    load_verified_hotspots,
)
from backend.accessibility_quality import assess_direct_route
from backend.accessibility_repository import InMemoryAccessibilityRepository
from backend.accessibility_runs import AccessibilityRunStore
from backend.accessibility_worker import (
    AccessibilityWorker,
    WorkUnit,
    build_work_units,
    period_departures,
)
from backend.domain import (
    AccessibilityOrigin,
    AccessibilityProfile,
    AccessibilityRequest,
    AccessibilityResultType,
    Coordinates,
    Hotspot,
    OriginType,
    ProviderMetadata,
    RouteResult,
    RoutingDiagnostic,
    TravelMode,
    TravelStatus,
    TravelTimeSample,
)
from backend.routing_provider import (
    FixtureAccessibilityRoutingProvider,
    NoRouteError,
    RoutingGraphMetadata,
    RoutingValidationError,
    TransientRoutingError,
)


NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
SERVICE_WEEK = datetime(2026, 9, 14, tzinfo=timezone.utc)
BOUNDS = CoordinateBounds(42.8, 43.2, -81.5, -80.9)
PROPERTY = ReviewedProperty(
    10, "123 Test Street, London, Ontario", Coordinates(43.0, -81.25), "approved"
)
HOTSPOT = VerifiedHotspot(
    Hotspot(
        "western-main-campus",
        "Western University main campus",
        Coordinates(43.0096, -81.2737),
        category="campus",
        source="existing_project_campus_reference",
    ),
    "verified",
    NOW,
)
GRAPH = RoutingGraphMetadata("otp-test", "network-v1", "schedule-v1", NOW)


def route(mode: TravelMode = TravelMode.WALKING, *, duration: int = 1800) -> RouteResult:
    return RouteResult(
        PROPERTY.coordinates,
        HOTSPOT.hotspot.coordinates,
        mode,
        2400,
        duration,
        TravelStatus.AVAILABLE,
        ProviderMetadata("fixture", "exact_route", NOW),
        False,
        1.0,
        provider_mode="walk" if mode is TravelMode.WALKING else "bicycle",
    )


def provider(*, samples=None, failures=None):
    return FixtureAccessibilityRoutingProvider(
        GRAPH,
        routes={
            TravelMode.WALKING: route(),
            TravelMode.CYCLING: route(TravelMode.CYCLING, duration=600),
        },
        samples=samples,
        failures=failures,
    )


def worker(repository, fixture_provider, **overrides) -> AccessibilityWorker:
    values = {
        "provider_profile": "fixture-profile-v1",
        "graph_metadata": GRAPH,
        "reference_service_week": SERVICE_WEEK,
        "minimum_transit_samples": 2,
        "max_retries": 2,
        "retry_delay_seconds": 0,
        "persist": True,
        "now": lambda: NOW,
        "sleeper": lambda _: None,
    }
    values.update(overrides)
    return AccessibilityWorker(repository, fixture_provider, **values)


def direct_unit(mode: TravelMode = TravelMode.WALKING) -> WorkUnit:
    return WorkUnit(PROPERTY, HOTSPOT, mode)


def transit_unit(index: int = 0) -> WorkUnit:
    return build_work_units([PROPERTY], [HOTSPOT], (TravelMode.TRANSIT,))[index]


def transit_samples(unit: WorkUnit, count: int = 3) -> dict[datetime, TravelTimeSample]:
    assert unit.period is not None
    departures = period_departures(unit.period, SERVICE_WEEK)
    return {
        departure: TravelTimeSample(
            departure,
            1500 + index * 60,
            walking_duration_seconds=300,
            waiting_duration_seconds=120,
            in_vehicle_duration_seconds=1080 + index * 60,
            transfer_count=index % 2,
            distance_meters=5200,
            status=TravelStatus.AVAILABLE,
        )
        for index, departure in enumerate(departures[:count])
    }


def walking_better_samples(unit: WorkUnit) -> dict[datetime, TravelTimeSample]:
    assert unit.period is not None
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


def mixed_deterministic_samples(
    unit: WorkUnit,
) -> dict[datetime, TravelTimeSample]:
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


def test_bounded_selection_rejects_unreviewed_missing_out_of_bounds_and_over_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "properties.csv"
    path.write_text(
        "property_id,latitude,longitude,normalized_address,review_status\n"
        "1,43.0,-81.25,One Street,pending\n",
        encoding="utf-8",
    )
    with pytest.raises(AccessibilityInputError, match="not reviewed"):
        load_reviewed_properties(path, bounds=BOUNDS, reviewed_only=True)
    path.write_text(
        "property_id,latitude,longitude,normalized_address,review_status\n"
        "1,,,-,approved\n",
        encoding="utf-8",
    )
    with pytest.raises(AccessibilityInputError, match="coordinates"):
        load_reviewed_properties(path, bounds=BOUNDS, reviewed_only=True)
    path.write_text(
        "property_id,latitude,longitude,normalized_address,review_status\n"
        "1,44,-81.25,Outside,approved\n",
        encoding="utf-8",
    )
    with pytest.raises(AccessibilityInputError, match="outside"):
        load_reviewed_properties(path, bounds=BOUNDS, reviewed_only=True)
    path.write_text(
        "property_id,latitude,longitude,normalized_address,review_status\n"
        "1,43,-81.25,One,approved\n2,43,-81.26,Two,approved\n",
        encoding="utf-8",
    )
    with pytest.raises(AccessibilityInputError, match="safety limit"):
        load_reviewed_properties(
            path, bounds=BOUNDS, reviewed_only=True, limit=1
        )
    assert len(
        load_reviewed_properties(
            path,
            bounds=BOUNDS,
            reviewed_only=True,
            limit=1,
            allow_larger_run=True,
        )
    ) == 2


def test_hotspot_loader_rejects_unverified_and_unknown_selection(tmp_path: Path) -> None:
    path = tmp_path / "hotspots.json"
    path.write_text(
        json.dumps(
            {
                "hotspots": [
                    {
                        "hotspot_id": "pending",
                        "name": "Pending",
                        "category": "campus",
                        "latitude": 43,
                        "longitude": -81.2,
                        "verification_status": "pending",
                        "source": "fixture",
                        "verified_at": NOW.isoformat(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AccessibilityInputError, match="not verified"):
        load_verified_hotspots(path, bounds=BOUNDS)
    with pytest.raises(AccessibilityInputError, match="Unknown"):
        load_verified_hotspots(path, bounds=BOUNDS, selected_ids={"unknown"})


def test_work_units_use_six_existing_periods_and_fixed_service_dates() -> None:
    units = build_work_units(
        [PROPERTY],
        [HOTSPOT],
        (TravelMode.WALKING, TravelMode.CYCLING, TravelMode.TRANSIT),
    )
    assert len(units) == 8
    transit = [unit for unit in units if unit.mode is TravelMode.TRANSIT]
    assert len(transit) == 6
    assert len(period_departures(transit[0].period, SERVICE_WEEK)) == 3
    assert period_departures(transit[-1].period, SERVICE_WEEK)[0].weekday() == 6


def test_dry_run_estimate_makes_no_provider_calls_or_database_writes() -> None:
    repository = InMemoryAccessibilityRepository()
    fixture = provider()
    service = worker(repository, fixture)
    estimate = service.estimate([direct_unit()])
    assert estimate["expected_provider_calls"] == 1
    assert estimate["expected_database_writes"] == 1
    assert fixture.calls == []
    assert repository.profiles == []


def test_walking_and_cycling_profiles_are_normalized_and_idempotent() -> None:
    repository = InMemoryAccessibilityRepository()
    fixture = provider()
    service = worker(repository, fixture)
    walking = service.process(direct_unit(), worker_run_id="run-1")
    cycling = service.process(direct_unit(TravelMode.CYCLING), worker_run_id="run-1")
    repeated = service.process(direct_unit(), worker_run_id="run-2")
    assert walking.profile.representative_duration_seconds == 1800
    assert cycling.profile.representative_duration_seconds == 600
    assert walking.profile.worker_run_id == "run-1"
    assert repeated.action == "cache_hit"
    assert len(repository.profiles) == 2
    assert len([call for call in fixture.calls if call[0] == "route"]) == 2


def test_cached_profile_preserves_warning_decision() -> None:
    repository = InMemoryAccessibilityRepository()
    first = worker(repository, provider()).process(
        direct_unit(), worker_run_id="warning-source"
    )
    repository.profiles[0] = replace(
        first.profile,
        quality_reason_codes=("excessive_detour",),
    )

    repeated = worker(repository, provider()).process(
        direct_unit(), worker_run_id="warning-cache"
    )

    assert repeated.action == "cache_hit"
    assert repeated.quality.decision == "accepted_with_warning"
    assert repeated.quality.reason_codes == ("excessive_detour",)


def test_worker_profile_remains_compatible_with_existing_api_resolver() -> None:
    repository = InMemoryAccessibilityRepository()
    worker(repository, provider()).process(direct_unit(), worker_run_id="api-run")
    decision = AccessibilityResolver(repository).resolve(
        AccessibilityRequest(
            origin=AccessibilityOrigin(
                PROPERTY.coordinates,
                property_id=PROPERTY.property_id,
                origin_type=OriginType.PROPERTY,
            ),
            hotspot=HOTSPOT.hotspot,
            travel_mode=TravelMode.WALKING,
            time_period=None,
            provider="fixture",
            provider_profile="fixture-profile-v1",
            requested_at=NOW,
            network_version="network-v1",
        ),
        record_history=False,
    )
    assert decision.result_type.value == "cached_exact_property"
    assert decision.result.quality_status == "complete"


def test_expired_profile_is_archived_and_replaced_without_losing_history() -> None:
    repository = InMemoryAccessibilityRepository()
    fixture = provider()
    service = worker(repository, fixture)
    first = service.process(direct_unit(), worker_run_id="run-1").profile
    repository.profiles[0] = replace(first, expires_at=NOW - timedelta(seconds=1))
    second = service.process(direct_unit(), worker_run_id="run-2").profile
    assert second.profile_id != first.profile_id
    assert len(repository.profiles) == 2
    assert repository.profiles[0].is_stale is True
    assert service.metrics.profiles_refreshed == 1


def test_changed_network_version_does_not_reuse_old_profile() -> None:
    repository = InMemoryAccessibilityRepository()
    first_provider = provider()
    worker(repository, first_provider).process(direct_unit(), worker_run_id="run-1")
    graph = replace(GRAPH, network_version="network-v2")
    second_provider = FixtureAccessibilityRoutingProvider(
        graph, routes={TravelMode.WALKING: route()}
    )
    worker(
        repository, second_provider, graph_metadata=graph
    ).process(direct_unit(), worker_run_id="run-2")
    assert len(repository.profiles) == 2
    assert {profile.network_version for profile in repository.profiles} == {
        "network-v1",
        "network-v2",
    }


def test_transit_aggregation_is_deterministic_and_stores_all_samples() -> None:
    unit = transit_unit()
    sample_map = transit_samples(unit)
    repository = InMemoryAccessibilityRepository()
    outcome = worker(repository, provider(samples=sample_map)).process(
        unit, worker_run_id="transit-run"
    )
    assert outcome.profile.representative_duration_seconds == 1560
    assert outcome.profile.minimum_duration_seconds == 1500
    assert outcome.profile.maximum_duration_seconds == 1620
    assert outcome.profile.sample_count == 3
    assert outcome.profile.requested_sample_count == 3
    assert outcome.quality.quality_status == "complete"
    assert len(repository.load_samples(outcome.profile.profile_id)) == 3


def test_walking_better_than_transit_is_current_and_fully_cacheable() -> None:
    unit = transit_unit()
    repository = InMemoryAccessibilityRepository()
    first_service = worker(repository, provider(samples=walking_better_samples(unit)))

    first = first_service.process(unit, worker_run_id="walking-alternative")

    assert first.action == "created"
    assert first.profile.is_stale is False
    assert first.profile.representative_duration_seconds is None
    assert first.profile.sample_count == 0
    assert first.profile.requested_sample_count == 3
    assert first.profile.quality_status == "no_route"
    assert first.profile.quality_reason_codes == ("walking_better_than_transit",)
    assert first.quality.decision == "accepted_with_warning"
    assert len(repository.load_samples(first.profile.profile_id)) == 3

    repeated_provider = provider()
    repeated_service = worker(repository, repeated_provider)
    repeated = repeated_service.process(unit, worker_run_id="walking-cache")

    assert repeated.action == "cache_hit"
    assert repeated.quality.decision == "accepted_with_warning"
    assert repeated.quality.reason_codes == ("walking_better_than_transit",)
    assert repeated_service.metrics.cache_hits == 1
    assert repeated_service.metrics.provider_calls == 0
    assert repeated_service.metrics.database_writes == 0
    assert repeated_provider.calls == []
    assert len(repository.profiles) == 1


def test_stale_walking_alternative_samples_are_reused_without_provider_call() -> None:
    unit = transit_unit()
    repository = InMemoryAccessibilityRepository()
    first = worker(
        repository, provider(samples=walking_better_samples(unit))
    ).process(unit, worker_run_id="walking-source")
    repository.profiles[0] = replace(
        first.profile,
        is_stale=True,
        stale_reason="legacy_insufficient_samples",
        expires_at=NOW,
    )
    fixture = provider()
    service = worker(repository, fixture)

    refreshed = service.process(unit, worker_run_id="walking-refresh")

    assert refreshed.action == "created"
    assert refreshed.profile.is_stale is False
    assert refreshed.profile.quality_reason_codes == ("walking_better_than_transit",)
    assert service.metrics.sample_cache_hits == 3
    assert service.metrics.provider_calls == 0
    assert service.metrics.database_writes == 1
    assert fixture.calls == []
    assert len(repository.profiles) == 2
    assert sum(not profile.is_stale for profile in repository.profiles) == 1
    assert repository.profiles[0].stale_reason == "legacy_insufficient_samples"


def test_ordinary_unavailable_samples_remain_stale_and_retryable() -> None:
    unit = transit_unit()
    departures = period_departures(unit.period, SERVICE_WEEK)
    unavailable = {
        departure: TravelTimeSample(
            departure,
            None,
            status=TravelStatus.UNAVAILABLE,
            routing_diagnostics=(RoutingDiagnostic("no_route", "No route."),),
        )
        for departure in departures
    }
    repository = InMemoryAccessibilityRepository()
    first = worker(repository, provider(samples=unavailable)).process(
        unit, worker_run_id="unavailable"
    )
    repeated_provider = provider(samples=unavailable)
    repeated_service = worker(repository, repeated_provider)

    repeated = repeated_service.process(unit, worker_run_id="unavailable-retry")

    assert first.profile.is_stale is True
    assert repeated.profile.is_stale is True
    assert repeated_service.metrics.provider_calls == 3
    assert [call[0] for call in repeated_provider.calls] == ["samples"]


def test_mixed_deterministic_transit_is_current_reviewable_and_cacheable() -> None:
    unit = transit_unit()
    repository = InMemoryAccessibilityRepository()
    first_service = worker(
        repository, provider(samples=mixed_deterministic_samples(unit))
    )

    first = first_service.process(unit, worker_run_id="mixed-deterministic")

    assert first.action == "created"
    assert first.profile.is_stale is False
    assert first.profile.stale_reason is None
    assert first.profile.expires_at == NOW + TRANSIT_PROFILE_TTL
    assert first.profile.sample_count == 1
    assert first.profile.requested_sample_count == 3
    assert first.profile.representative_duration_seconds == 1500
    assert first.profile.quality_status == "insufficient_samples"
    assert first.profile.quality_reason_codes == (
        "insufficient_samples",
        "no_route",
        "walking_better_than_transit",
    )
    assert first.quality.decision == "manual_review_required"
    assert [sample.status for sample in first.samples] == [
        TravelStatus.UNAVAILABLE,
        TravelStatus.AVAILABLE,
        TravelStatus.UNAVAILABLE,
    ]
    assert [
        diagnostic.code for diagnostic in first.samples[0].routing_diagnostics
    ] == ["no_route"]
    assert [
        diagnostic.code for diagnostic in first.samples[2].routing_diagnostics
    ] == ["walking_better_than_transit"]

    repeated_provider = provider()
    repeated_service = worker(repository, repeated_provider)
    repeated = repeated_service.process(unit, worker_run_id="mixed-cache")

    assert repeated.action == "cache_hit"
    assert repeated.quality.quality_status == "insufficient_samples"
    assert repeated.quality.decision == "manual_review_required"
    assert repeated.quality.reason_codes == first.quality.reason_codes
    assert repeated_service.metrics.cache_hits == 1
    assert repeated_service.metrics.provider_calls == 0
    assert repeated_service.metrics.database_writes == 0
    assert repeated_provider.calls == []
    assert len(repository.profiles) == 1


@pytest.mark.parametrize("version_field", ["network_version", "schedule_version"])
def test_mixed_deterministic_cache_respects_graph_compatibility(
    version_field: str,
) -> None:
    unit = transit_unit()
    repository = InMemoryAccessibilityRepository()
    worker(
        repository, provider(samples=mixed_deterministic_samples(unit))
    ).process(unit, worker_run_id="mixed-v1")
    graph = replace(GRAPH, **{version_field: f"changed-{version_field}"})
    fixture = FixtureAccessibilityRoutingProvider(
        graph, samples=mixed_deterministic_samples(unit)
    )
    service = worker(repository, fixture, graph_metadata=graph)

    outcome = service.process(unit, worker_run_id="mixed-v2")

    assert outcome.action == "created"
    assert service.metrics.cache_hits == 0
    assert service.metrics.provider_calls == 3
    assert len(repository.profiles) == 2


def test_mixed_deterministic_cache_respects_property_coordinate_fingerprint() -> None:
    unit = transit_unit()
    repository = InMemoryAccessibilityRepository()
    worker(
        repository, provider(samples=mixed_deterministic_samples(unit))
    ).process(unit, worker_run_id="mixed-coordinate-v1")
    changed_unit = replace(
        unit,
        property=replace(
            PROPERTY, coordinates=Coordinates(43.0005, -81.2505)
        ),
    )
    fixture = provider(samples=mixed_deterministic_samples(changed_unit))
    service = worker(repository, fixture)

    outcome = service.process(changed_unit, worker_run_id="mixed-coordinate-v2")

    assert outcome.action == "created"
    assert service.metrics.cache_hits == 0
    assert service.metrics.provider_calls == 3
    assert len(repository.profiles) == 2


@pytest.mark.parametrize("version_field", ["network_version", "schedule_version"])
def test_walking_alternative_cache_respects_graph_compatibility(
    version_field: str,
) -> None:
    unit = transit_unit()
    repository = InMemoryAccessibilityRepository()
    worker(repository, provider(samples=walking_better_samples(unit))).process(
        unit, worker_run_id="alternative-v1"
    )
    graph = replace(GRAPH, **{version_field: f"changed-{version_field}"})
    fixture = FixtureAccessibilityRoutingProvider(
        graph, samples=walking_better_samples(unit)
    )
    service = worker(repository, fixture, graph_metadata=graph)

    outcome = service.process(unit, worker_run_id="alternative-v2")

    assert outcome.action == "created"
    assert service.metrics.cache_hits == 0
    assert service.metrics.provider_calls == 3
    assert len(repository.profiles) == 2


def test_walking_alternative_cache_respects_property_coordinate_fingerprint() -> None:
    unit = transit_unit()
    repository = InMemoryAccessibilityRepository()
    worker(repository, provider(samples=walking_better_samples(unit))).process(
        unit, worker_run_id="coordinate-v1"
    )
    changed_property = replace(
        PROPERTY, coordinates=Coordinates(43.0005, -81.2505)
    )
    changed_unit = replace(unit, property=changed_property)
    fixture = provider(samples=walking_better_samples(changed_unit))
    service = worker(repository, fixture)

    outcome = service.process(changed_unit, worker_run_id="coordinate-v2")

    assert outcome.action == "created"
    assert service.metrics.cache_hits == 0
    assert service.metrics.provider_calls == 3
    assert len(repository.profiles) == 2


def test_transit_quality_preserves_provider_reasons_and_publishability() -> None:
    unit = transit_unit()
    sample_map = transit_samples(unit)
    last_departure = list(sample_map)[-1]
    sample_map[last_departure] = TravelTimeSample(
        last_departure,
        None,
        status=TravelStatus.UNAVAILABLE,
        routing_diagnostics=(
            RoutingDiagnostic(
                "no_transit_connection",
                "No transit connection was found for this departure.",
            ),
        ),
    )
    repository = InMemoryAccessibilityRepository()
    outcome = worker(repository, provider(samples=sample_map)).process(
        unit, worker_run_id="partial-with-reason"
    )
    assert outcome.profile.sample_count == 2
    assert outcome.profile.is_stale is False
    assert outcome.quality.quality_status == "partial"
    assert "no_transit_connection" in outcome.quality.reason_codes
    assert outcome.quality.decision == "accepted_with_warning"
    assert "unexpected_no_route" not in outcome.quality.reason_codes
    assert outcome.samples[-1].duration_seconds is None
    assert outcome.to_dict()["samples"][-1]["routing_reason_codes"] == [
        "no_transit_connection"
    ]


def test_below_threshold_profile_keeps_actual_no_route_reason_codes() -> None:
    unit = transit_unit()
    departures = period_departures(unit.period, SERVICE_WEEK)
    sample_map = {
        departures[0]: TravelTimeSample(departures[0], 1500),
        departures[1]: TravelTimeSample(
            departures[1],
            None,
            status=TravelStatus.UNAVAILABLE,
            routing_diagnostics=(
                RoutingDiagnostic(
                    "outside_service_period", "Departure is outside the feed range."
                ),
            ),
        ),
        departures[2]: TravelTimeSample(
            departures[2],
            None,
            status=TravelStatus.UNAVAILABLE,
            routing_diagnostics=(
                RoutingDiagnostic(
                    "no_stops_in_range", "No transit stops are reachable."
                ),
            ),
        ),
    }
    outcome = worker(
        InMemoryAccessibilityRepository(), provider(samples=sample_map)
    ).process(unit, worker_run_id="insufficient-with-reasons")
    assert outcome.profile.sample_count == 1
    assert outcome.profile.is_stale is True
    assert outcome.quality.quality_status == "insufficient_samples"
    assert set(outcome.quality.reason_codes) == {
        "insufficient_samples",
        "outside_service_period",
        "no_stops_in_range",
    }


def test_quality_report_preserves_sanitized_provider_reason_codes(
    tmp_path: Path,
) -> None:
    unit = transit_unit()
    departures = period_departures(unit.period, SERVICE_WEEK)
    samples = {
        departure: TravelTimeSample(
            departure,
            None,
            status=TravelStatus.UNAVAILABLE,
            routing_diagnostics=(
                RoutingDiagnostic(
                    "no_transit_connection",
                    "No transit connection was found.",
                ),
            ),
        )
        for departure in departures
    }
    outcome = worker(
        InMemoryAccessibilityRepository(), provider(samples=samples)
    ).process(unit, worker_run_id="quality-report-reasons")
    store = AccessibilityRunStore.create(
        tmp_path,
        properties=[PROPERTY],
        hotspots=[HOTSPOT],
        modes=["transit"],
        provider="fixture",
        provider_profile="v1",
        graph_metadata=GRAPH.to_dict(),
        input_fingerprints={"gtfs_sha256": "a" * 64},
        config_path=tmp_path / "config.toml",
        properties_path=tmp_path / "properties.csv",
    )
    store.record(outcome, {"provider_calls": 3})
    store.finalize()
    report = store.paths.quality_csv.read_text(encoding="utf-8")
    route_results = store.paths.route_results.read_text(encoding="utf-8")
    assert "no_transit_connection" in report
    assert "no_transit_connection" in route_results
    assert "provider_metadata" not in route_results


def test_partial_sample_resume_requests_only_missing_departures() -> None:
    unit = transit_unit()
    all_samples = transit_samples(unit)
    one_departure = next(iter(all_samples))
    repository = InMemoryAccessibilityRepository()
    first_provider = provider(samples={one_departure: all_samples[one_departure]})
    first = worker(repository, first_provider).process(unit, worker_run_id="partial")
    assert first.profile.is_stale
    assert first.quality.quality_status == "insufficient_samples"
    remaining = {key: value for key, value in all_samples.items() if key != one_departure}
    second_provider = provider(samples=remaining)
    second_service = worker(repository, second_provider)
    second = second_service.process(unit, worker_run_id="resume")
    requested = [call for call in second_provider.calls if call[0] == "samples"][0][1]
    assert len(requested) == 2
    assert one_departure not in requested
    assert second_service.metrics.sample_cache_hits == 1
    assert second.profile.sample_count == 3
    assert second.profile.is_stale is False
    assert len(repository.profiles) == 2


def test_no_route_validation_errors_and_retry_limit_are_classified() -> None:
    for error in (NoRouteError("none"), RoutingValidationError("bad schema")):
        fixture = provider(failures=[error])
        outcome = worker(InMemoryAccessibilityRepository(), fixture).process(
            direct_unit(), worker_run_id="failed"
        )
        assert outcome.action == "failed"
        assert len(fixture.calls) == 1
    fixture = provider(
        failures=[TransientRoutingError("timeout")] * 3
    )
    service = worker(InMemoryAccessibilityRepository(), fixture)
    outcome = service.process(direct_unit(), worker_run_id="retry")
    assert outcome.action == "failed"
    assert service.metrics.provider_calls == 3
    assert service.metrics.provider_retries == 2


def test_transient_failure_retries_then_succeeds() -> None:
    fixture = provider(failures=[TransientRoutingError("timeout")])
    outcome = worker(InMemoryAccessibilityRepository(), fixture).process(
        direct_unit(), worker_run_id="retry-success"
    )
    assert outcome.action == "created"
    assert len([call for call in fixture.calls if call[0] == "route"]) == 2


def test_quality_checks_flag_implausible_routes_but_do_not_leak_payload() -> None:
    assessment = assess_direct_route(route(duration=1))
    assert assessment.decision == "manual_review_required"
    assert "impossible_speed" in assessment.reason_codes
    assert "provider_metadata" not in route().to_dict()


def test_repository_transaction_rolls_back_profile_when_sample_write_fails(
    monkeypatch,
) -> None:
    repository = InMemoryAccessibilityRepository()
    unit = transit_unit()

    def fail(*_):
        raise RuntimeError("fixture sample failure")

    monkeypatch.setattr(repository, "save_samples", fail)
    with pytest.raises(RuntimeError, match="sample failure"):
        worker(repository, provider(samples=transit_samples(unit))).process(
            unit, worker_run_id="rollback"
        )
    assert repository.profiles == []
    assert repository.samples == {}


def test_atomic_run_artifacts_support_resume_and_focused_review(tmp_path: Path) -> None:
    store = AccessibilityRunStore.create(
        tmp_path,
        properties=[PROPERTY],
        hotspots=[HOTSPOT],
        modes=["walking"],
        provider="fixture",
        provider_profile="v1",
        graph_metadata=GRAPH.to_dict(),
        input_fingerprints={"osm_sha256": "a" * 64},
        config_path=tmp_path / "config.toml",
        properties_path=tmp_path / "properties.csv",
    )
    outcome = worker(InMemoryAccessibilityRepository(), provider()).process(
        direct_unit(), worker_run_id=store.run_id
    )
    warning = replace(
        outcome,
        quality=replace(
            outcome.quality,
            decision="manual_review_required",
            reason_codes=("excessive_detour",),
        ),
    )
    store.record(warning, {"provider_calls": 1})
    store.record(warning, {"provider_calls": 1})
    manifest = store.finalize()
    assert manifest["status"] == "completed_with_warnings"
    assert len(store.outcomes()) == 1
    assert store.paths.route_results.read_text(encoding="utf-8").endswith("\n")
    assert "excessive_detour" in store.paths.review_csv.read_text(encoding="utf-8")
    assert json.loads(store.paths.quality_json.read_text(encoding="utf-8"))["review_count"] == 1
    assert not list(store.paths.root.glob("*.tmp"))


def test_atomic_run_text_retries_transient_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = accessibility_runs.os.replace
    route_replace_attempts = 0

    def transient_replace(source: str, destination: str) -> None:
        nonlocal route_replace_attempts
        if Path(destination).name == "route-results.jsonl":
            route_replace_attempts += 1
            if route_replace_attempts == 2:
                raise PermissionError(13, "transient Windows file lock")
        real_replace(source, destination)

    monkeypatch.setattr(accessibility_runs.os, "replace", transient_replace)
    monkeypatch.setattr(accessibility_runs.time, "sleep", lambda _: None)
    store = AccessibilityRunStore.create(
        tmp_path,
        properties=[PROPERTY],
        hotspots=[HOTSPOT],
        modes=["walking"],
        provider="fixture",
        provider_profile="v1",
        graph_metadata=GRAPH.to_dict(),
        input_fingerprints={"osm_sha256": "a" * 64},
        config_path=tmp_path / "config.toml",
        properties_path=tmp_path / "properties.csv",
    )
    outcome = worker(InMemoryAccessibilityRepository(), provider()).process(
        direct_unit(), worker_run_id=store.run_id
    )

    store.record(outcome, {"provider_calls": 1})

    assert route_replace_attempts == 3
    assert len(store.outcomes()) == 1
    assert not list(store.paths.root.glob("*.tmp"))


def test_finalize_recovers_metrics_when_outcome_precedes_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AccessibilityRunStore.create(
        tmp_path,
        properties=[PROPERTY],
        hotspots=[HOTSPOT],
        modes=["walking"],
        provider="fixture",
        provider_profile="v1",
        graph_metadata=GRAPH.to_dict(),
        input_fingerprints={"osm_sha256": "a" * 64},
        config_path=tmp_path / "config.toml",
        properties_path=tmp_path / "properties.csv",
    )
    outcome = worker(InMemoryAccessibilityRepository(), provider()).process(
        direct_unit(), worker_run_id=store.run_id
    )
    real_atomic_write_json = accessibility_runs.atomic_write_json

    def locked_manifest(path: Path, payload: dict[str, object]) -> None:
        if path == store.paths.manifest:
            raise PermissionError(13, "persistent Windows manifest lock")
        real_atomic_write_json(path, payload)

    monkeypatch.setattr(accessibility_runs, "atomic_write_json", locked_manifest)
    with pytest.raises(PermissionError, match="manifest lock"):
        store.record(outcome, {"provider_calls": 1, "database_writes": 1})
    assert store.manifest()["metrics"] == {}

    monkeypatch.setattr(accessibility_runs, "atomic_write_json", real_atomic_write_json)
    manifest = store.finalize(interrupted=True)
    assert manifest["completed_unit_keys"] == [outcome.unit_key]
    assert manifest["success_count"] == 1
    assert manifest["metrics"] == {"provider_calls": 1, "database_writes": 1}
