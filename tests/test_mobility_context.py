from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.domain import Coordinates, RouteItinerary, RouteLeg
from backend.mobility_context import (
    ALGORITHM_VERSION,
    EXCLUDED_CITY_DATASETS,
    INCLUDED_CITY_DATASETS,
    CityDatasetVersion,
    MobilityContextValidationError,
    context_quality_state,
    dependency_fingerprint,
    route_share,
    validate_exact_bicycle_route,
)
from backend.providers import haversine_distance_meters


ORIGIN = Coordinates(43.0000, -81.2500)
DESTINATION = Coordinates(43.0010, -81.2500)


def _encode(points: list[Coordinates]) -> str:
    output: list[str] = []
    previous = (0, 0)
    for point in points:
        current = (round(point.latitude * 100_000), round(point.longitude * 100_000))
        for value, old in zip(current, previous, strict=True):
            delta = value - old
            encoded = ~(delta << 1) if delta < 0 else delta << 1
            while encoded >= 0x20:
                output.append(chr((0x20 | (encoded & 0x1F)) + 63))
                encoded >>= 5
            output.append(chr(encoded + 63))
        previous = current
    return "".join(output)


def _itinerary(
    points: list[Coordinates] | None = None,
    *,
    mode: str = "BICYCLE",
    point_count: int | None = None,
) -> RouteItinerary:
    selected = points or [ORIGIN, DESTINATION]
    return RouteItinerary(
        legs=(
            RouteLeg(
                mode=mode,
                duration_seconds=60,
                distance_meters=round(haversine_distance_meters(selected[0], selected[-1])),
                encoded_polyline=_encode(selected),
                geometry_point_count=point_count or len(selected),
            ),
        )
    )


def _versions() -> tuple[CityDatasetVersion, ...]:
    return tuple(
        CityDatasetVersion(name, index, f"{index:x}" * 64, f"{index + 3:x}" * 64, 10)
        for index, name in enumerate(INCLUDED_CITY_DATASETS, 1)
    )


def test_exact_bicycle_geometry_is_validated_against_route_contract() -> None:
    distance = round(haversine_distance_meters(ORIGIN, DESTINATION))
    result = validate_exact_bicycle_route(
        _itinerary(),
        origin=ORIGIN,
        destination=DESTINATION,
        duration_seconds=60,
        distance_meters=distance,
    )
    assert len(result.coordinates) == 2
    assert result.origin_offset_meters == 0
    assert result.destination_offset_meters == 0
    assert 0.99 <= result.itinerary_distance_ratio <= 1.01


@pytest.mark.parametrize(
    ("itinerary", "origin", "destination", "duration", "distance", "message"),
    [
        (_itinerary(mode="WALK"), ORIGIN, DESTINATION, 60, 111, "non-bicycle"),
        (_itinerary(), Coordinates(42.90, -81.25), DESTINATION, 60, 111, "begin near"),
        (_itinerary(), ORIGIN, Coordinates(43.10, -81.25), 60, 111, "end near"),
        (_itinerary([ORIGIN, Coordinates(43.30, -81.25)]), ORIGIN, Coordinates(43.30, -81.25), 60, 33_000, "London bounds"),
        (_itinerary(), ORIGIN, DESTINATION, 0, 111, "duration"),
        (_itinerary(), ORIGIN, DESTINATION, 60, 1_000, "disagrees"),
    ],
)
def test_exact_bicycle_geometry_fails_closed(
    itinerary: RouteItinerary,
    origin: Coordinates,
    destination: Coordinates,
    duration: int,
    distance: int,
    message: str,
) -> None:
    with pytest.raises(MobilityContextValidationError, match=message):
        validate_exact_bicycle_route(
            itinerary,
            origin=origin,
            destination=destination,
            duration_seconds=duration,
            distance_meters=distance,
        )


def test_dependency_fingerprint_is_order_independent_and_version_sensitive() -> None:
    inputs = {
        "property_id": 4,
        "origin": ORIGIN,
        "destination_id": "western-main-campus",
        "destination": DESTINATION,
        "routing_provider": "opentripplanner",
        "provider_profile": "otp-local-transport-v2",
        "router_version": "otp-2.6.0",
        "network_version": "network-a",
    }
    first = dependency_fingerprint(**inputs, city_versions=_versions())
    assert first == dependency_fingerprint(
        **inputs, city_versions=tuple(reversed(_versions()))
    )
    assert first != dependency_fingerprint(
        **(inputs | {"network_version": "network-b"}), city_versions=_versions()
    )
    assert first != dependency_fingerprint(
        **inputs,
        city_versions=_versions(),
        algorithm_version=ALGORITHM_VERSION + "-changed",
    )
    changed_city = list(_versions())
    version = changed_city[0]
    changed_city[0] = CityDatasetVersion(
        version.dataset, version.run_id + 10, "f" * 64, version.schema_sha256, 10
    )
    assert first != dependency_fingerprint(**inputs, city_versions=changed_city)


def test_dependency_fingerprint_requires_every_current_eligible_city_source() -> None:
    with pytest.raises(MobilityContextValidationError, match="all eligible"):
        dependency_fingerprint(
            property_id=4,
            origin=ORIGIN,
            destination_id="western-main-campus",
            destination=DESTINATION,
            routing_provider="opentripplanner",
            provider_profile="v2",
            router_version="otp-2.6.0",
            network_version="network-a",
            city_versions=_versions()[:-1],
        )


def test_route_share_handles_zero_full_partial_and_missing_without_fabrication() -> None:
    assert route_share(0, 100) == 0
    assert route_share(25, 100) == 0.25
    assert route_share(100.00001, 100) == 1
    assert route_share(None, 100) is None
    assert route_share(0, None) is None
    with pytest.raises(MobilityContextValidationError):
        route_share(101, 100)


def test_city_contract_includes_only_defensible_cycling_and_multi_use_layers() -> None:
    assert INCLUDED_CITY_DATASETS == (
        "bicycle_routes",
        "recreation_paths_multi_use",
        "thames_valley_parkway",
    )
    assert EXCLUDED_CITY_DATASETS == ("walking_trails_unpaved",)


def test_context_quality_states_fail_closed_for_unsafe_or_missing_evidence() -> None:
    assert context_quality_state(
        location_status="available",
        route_available=True,
        exact_route_valid=True,
        city_dependencies_complete=True,
    ) == "ready"
    assert context_quality_state(
        location_status="available",
        route_available=True,
        exact_route_valid=True,
        city_dependencies_complete=False,
    ) == "limited"
    assert context_quality_state(
        location_status="limited",
        route_available=False,
        exact_route_valid=True,
        city_dependencies_complete=True,
    ) == "unavailable"
