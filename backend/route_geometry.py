"""Validation helpers for compact OTP encoded-polyline route geometry."""

from __future__ import annotations

from math import isfinite

from backend.domain import Coordinates, RouteItinerary
from backend.providers import haversine_distance_meters


class RouteGeometryError(ValueError):
    """Stored or provider route geometry is malformed or implausible."""


def decode_polyline(value: str, *, precision: int = 5) -> tuple[Coordinates, ...]:
    """Decode a Google encoded polyline without retaining a decoded duplicate."""

    if not value:
        raise RouteGeometryError("encoded polyline is empty")
    coordinates: list[Coordinates] = []
    latitude = 0
    longitude = 0
    index = 0
    factor = 10**precision
    while index < len(value):
        deltas: list[int] = []
        for _ in range(2):
            result = 0
            shift = 0
            while True:
                if index >= len(value):
                    raise RouteGeometryError("encoded polyline is truncated")
                byte = ord(value[index]) - 63
                index += 1
                if not 0 <= byte <= 63:
                    raise RouteGeometryError("encoded polyline contains invalid data")
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
                if shift > 60:
                    raise RouteGeometryError("encoded polyline component is too large")
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += deltas[0]
        longitude += deltas[1]
        point = Coordinates(latitude / factor, longitude / factor)
        if not isfinite(point.latitude) or not isfinite(point.longitude):
            raise RouteGeometryError("encoded polyline contains non-finite coordinates")
        coordinates.append(point)
    if len(coordinates) < 2:
        raise RouteGeometryError("route geometry requires at least two coordinates")
    return tuple(coordinates)


def validate_itinerary_geometry(
    itinerary: RouteItinerary,
    *,
    origin: Coordinates | None = None,
    destination: Coordinates | None = None,
    endpoint_tolerance_meters: int = 500,
    maximum_jump_meters: int = 5_000,
) -> dict[str, float | int]:
    """Validate encoded legs and return deterministic structural measures."""

    decoded = [decode_polyline(leg.encoded_polyline or "") for leg in itinerary.legs]
    for leg, points in zip(itinerary.legs, decoded, strict=True):
        if leg.geometry_point_count is not None and leg.geometry_point_count != len(points):
            raise RouteGeometryError("encoded point count does not match itinerary metadata")
    flattened = [point for points in decoded for point in points]
    if origin and haversine_distance_meters(origin, flattened[0]) > endpoint_tolerance_meters:
        raise RouteGeometryError("route geometry does not begin near the requested origin")
    if (
        destination
        and haversine_distance_meters(destination, flattened[-1])
        > endpoint_tolerance_meters
    ):
        raise RouteGeometryError("route geometry does not end near the requested destination")
    maximum_jump = 0.0
    length = 0.0
    for points in decoded:
        for first, second in zip(points, points[1:]):
            segment = haversine_distance_meters(first, second)
            maximum_jump = max(maximum_jump, segment)
            length += segment
            if segment > maximum_jump_meters:
                raise RouteGeometryError("route geometry contains an implausible jump")
    return {
        "leg_count": len(decoded),
        "point_count": len(flattened),
        "decoded_length_meters": round(length, 1),
        "maximum_jump_meters": round(maximum_jump, 1),
    }
