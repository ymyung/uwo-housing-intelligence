"""Shadow-only Mobility Context V1 domain rules.

This module validates exact OTP bicycle geometry and describes which official
City of London layers may participate in factual route-overlap research.  It
does not expose product state, persist property metrics, or imply safety.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable

from backend.domain import Coordinates, RouteItinerary
from backend.providers import haversine_distance_meters
from backend.route_geometry import decode_polyline


ALGORITHM_VERSION = "mobility-context-v1"
PROJECTED_SRID = 26917
VALIDATION_TOLERANCES_METERS = (5, 8, 10, 15)
DEFAULT_TOLERANCE_METERS = 8
LONDON_BOUNDS = (42.80, 43.20, -81.50, -80.90)

INCLUDED_CITY_DATASETS = (
    "bicycle_routes",
    "recreation_paths_multi_use",
    "thames_valley_parkway",
)
EXCLUDED_CITY_DATASETS = (
    "walking_trails_unpaved",
)
FACILITY_CATEGORIES = (
    "separated",
    "designated",
    "shared",
    "multi_use_path",
    "thames_valley_parkway",
)
INCLUDED_BICYCLE_STATUSES = ("existing",)
INCLUDED_BICYCLE_FACILITY_TYPES = ("separated", "designated", "shared")


class MobilityContextValidationError(ValueError):
    """Exact route or dependency evidence is not safe to analyze."""


@dataclass(frozen=True)
class CityDatasetVersion:
    dataset: str
    run_id: int
    content_sha256: str
    schema_sha256: str
    feature_count: int

    def __post_init__(self) -> None:
        if self.dataset not in INCLUDED_CITY_DATASETS:
            raise ValueError(f"{self.dataset}: dataset is not eligible for Mobility Context V1")
        if self.run_id <= 0 or self.feature_count < 0:
            raise ValueError("City dataset version values are invalid")
        for value in (self.content_sha256, self.schema_sha256):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError("City dataset fingerprints must be lowercase SHA-256 values")

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "run_id": self.run_id,
            "content_sha256": self.content_sha256,
            "schema_sha256": self.schema_sha256,
            "feature_count": self.feature_count,
        }


@dataclass(frozen=True)
class RouteGeometryAssessment:
    coordinates: tuple[Coordinates, ...]
    decoded_length_meters: float
    maximum_jump_meters: float
    origin_offset_meters: float
    destination_offset_meters: float
    itinerary_distance_ratio: float

    def to_dict(self) -> dict[str, object]:
        return {
            "point_count": len(self.coordinates),
            "decoded_length_meters": round(self.decoded_length_meters, 2),
            "maximum_jump_meters": round(self.maximum_jump_meters, 2),
            "origin_offset_meters": round(self.origin_offset_meters, 2),
            "destination_offset_meters": round(self.destination_offset_meters, 2),
            "itinerary_distance_ratio": round(self.itinerary_distance_ratio, 4),
        }


def validate_exact_bicycle_route(
    itinerary: RouteItinerary,
    *,
    origin: Coordinates,
    destination: Coordinates,
    duration_seconds: int | None,
    distance_meters: int | None,
    endpoint_tolerance_meters: float = 500,
    maximum_jump_meters: float = 5_000,
    minimum_distance_ratio: float = 0.70,
    maximum_distance_ratio: float = 1.30,
    bounds: tuple[float, float, float, float] = LONDON_BOUNDS,
) -> RouteGeometryAssessment:
    """Fail closed on malformed, mismatched, or implausible exact bike routes."""

    if duration_seconds is None or duration_seconds <= 0:
        raise MobilityContextValidationError("exact bicycle duration must be positive")
    if distance_meters is None or distance_meters <= 0:
        raise MobilityContextValidationError("exact bicycle distance must be positive")
    if not itinerary.legs or any(leg.mode != "BICYCLE" for leg in itinerary.legs):
        raise MobilityContextValidationError("exact bicycle itinerary contains a non-bicycle leg")

    decoded_legs = tuple(
        decode_polyline(leg.encoded_polyline or "") for leg in itinerary.legs
    )
    coordinates: list[Coordinates] = []
    for leg, points in zip(itinerary.legs, decoded_legs, strict=True):
        if leg.geometry_point_count is not None and leg.geometry_point_count != len(points):
            raise MobilityContextValidationError(
                "encoded point count does not match itinerary metadata"
            )
        if coordinates and coordinates[-1] == points[0]:
            coordinates.extend(points[1:])
        else:
            coordinates.extend(points)
    if len(coordinates) < 2:
        raise MobilityContextValidationError("exact bicycle geometry requires two points")

    minimum_latitude, maximum_latitude, minimum_longitude, maximum_longitude = bounds
    if any(
        not (
            minimum_latitude <= point.latitude <= maximum_latitude
            and minimum_longitude <= point.longitude <= maximum_longitude
        )
        for point in coordinates
    ):
        raise MobilityContextValidationError("exact bicycle geometry leaves London bounds")

    origin_offset = float(haversine_distance_meters(origin, coordinates[0]))
    destination_offset = float(haversine_distance_meters(destination, coordinates[-1]))
    if origin_offset > endpoint_tolerance_meters:
        raise MobilityContextValidationError("exact bicycle geometry does not begin near origin")
    if destination_offset > endpoint_tolerance_meters:
        raise MobilityContextValidationError(
            "exact bicycle geometry does not end near destination"
        )

    decoded_length = 0.0
    maximum_jump = 0.0
    for first, second in zip(coordinates, coordinates[1:]):
        segment = float(haversine_distance_meters(first, second))
        decoded_length += segment
        maximum_jump = max(maximum_jump, segment)
        if segment > maximum_jump_meters:
            raise MobilityContextValidationError(
                "exact bicycle geometry contains an implausible jump"
            )
    distance_ratio = decoded_length / distance_meters
    if not minimum_distance_ratio <= distance_ratio <= maximum_distance_ratio:
        raise MobilityContextValidationError(
            "decoded bicycle geometry disagrees with itinerary distance"
        )
    return RouteGeometryAssessment(
        coordinates=tuple(coordinates),
        decoded_length_meters=decoded_length,
        maximum_jump_meters=maximum_jump,
        origin_offset_meters=origin_offset,
        destination_offset_meters=destination_offset,
        itinerary_distance_ratio=distance_ratio,
    )


def dependency_fingerprint(
    *,
    property_id: int,
    origin: Coordinates,
    destination_id: str,
    destination: Coordinates,
    routing_provider: str,
    provider_profile: str,
    router_version: str,
    network_version: str,
    city_versions: Iterable[CityDatasetVersion],
    tolerance_meters: int = DEFAULT_TOLERANCE_METERS,
    algorithm_version: str = ALGORITHM_VERSION,
) -> str:
    """Return a deterministic compatibility identity for derived context."""

    if property_id <= 0 or not destination_id.strip():
        raise ValueError("property and destination identities are required")
    if tolerance_meters <= 0 or not algorithm_version.strip():
        raise ValueError("positive tolerance and algorithm version are required")
    versions = sorted(city_versions, key=lambda value: value.dataset)
    if tuple(value.dataset for value in versions) != tuple(sorted(INCLUDED_CITY_DATASETS)):
        raise MobilityContextValidationError(
            "all eligible current City dataset versions are required"
        )
    payload: dict[str, Any] = {
        "algorithm_version": algorithm_version,
        "tolerance_meters": tolerance_meters,
        "property_id": property_id,
        "origin": origin.to_dict(),
        "destination_id": destination_id,
        "destination": destination.to_dict(),
        "routing": {
            "provider": routing_provider,
            "provider_profile": provider_profile,
            "router_version": router_version,
            "network_version": network_version,
        },
        "city_versions": [value.to_dict() for value in versions],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def route_share(covered_meters: float | None, route_meters: float | None) -> float | None:
    """Return a bounded route share without fabricating missing values."""

    if covered_meters is None or route_meters is None:
        return None
    if covered_meters < 0 or route_meters <= 0:
        raise MobilityContextValidationError("route coverage measures are invalid")
    ratio = covered_meters / route_meters
    if ratio < -1e-9 or ratio > 1 + 1e-6:
        raise MobilityContextValidationError("route coverage exceeds route length")
    return min(1.0, max(0.0, ratio))


def context_quality_state(
    *,
    location_status: str,
    route_available: bool,
    exact_route_valid: bool,
    city_dependencies_complete: bool,
) -> str:
    """Project fail-closed research states without fabricating measures."""

    if location_status != "available" or not route_available or not exact_route_valid:
        return "unavailable"
    if not city_dependencies_complete:
        return "limited"
    return "ready"
