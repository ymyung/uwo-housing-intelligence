"""Small provider protocols and deterministic offline implementations."""

from __future__ import annotations

from datetime import datetime, timezone
from math import asin, cos, radians, sin, sqrt
from collections.abc import Callable
from typing import Protocol

from backend.domain import (
    Coordinates,
    GeocodeResult,
    Location,
    ProviderMetadata,
    RouteRequest,
    RouteResult,
    TravelMode,
    TravelStatus,
    TravelTimeResult,
    TravelTimeSample,
)

EARTH_RADIUS_METERS = 6_371_000.0


class GeocodingProvider(Protocol):
    def geocode(self, address: str) -> GeocodeResult: ...


class RoutingProvider(Protocol):
    def get_route(self, request: RouteRequest) -> RouteResult: ...


class TravelTimeProvider(Protocol):
    def get_travel_times(
        self,
        origin: Location,
        destinations: list[Location],
        modes: list[TravelMode],
    ) -> list[TravelTimeResult]: ...


class TransitProfileProvider(Protocol):
    def get_samples(
        self,
        origin: Location,
        destination: Location,
        departures: list[datetime],
    ) -> list[TravelTimeSample]: ...


def haversine_distance_meters(origin: Coordinates, destination: Coordinates) -> int:
    """Return stable, rounded great-circle distance between two coordinates."""

    lat1 = radians(origin.latitude)
    lat2 = radians(destination.latitude)
    delta_lat = radians(destination.latitude - origin.latitude)
    delta_lon = radians(destination.longitude - origin.longitude)
    value = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    angular_distance = 2 * asin(sqrt(value))
    return round(EARTH_RADIUS_METERS * angular_distance)


class StraightLineEstimateProvider:
    """Offline distance and duration estimates, never a live route result."""

    def __init__(
        self,
        *,
        walking_speed_kmh: float = 4.8,
        cycling_speed_kmh: float = 15.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if walking_speed_kmh <= 0 or cycling_speed_kmh <= 0:
            raise ValueError("travel speeds must be positive")
        self.walking_speed_kmh = walking_speed_kmh
        self.cycling_speed_kmh = cycling_speed_kmh
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider="straight_line_estimate",
            calculation_type="straight_line_estimate",
            calculated_at=self._now(),
        )

    def get_route(self, request: RouteRequest) -> RouteResult:
        distance = haversine_distance_meters(request.origin, request.destination)
        speed = {
            TravelMode.WALKING: self.walking_speed_kmh,
            TravelMode.CYCLING: self.cycling_speed_kmh,
        }.get(request.mode)
        if speed is None:
            return RouteResult(
                origin=request.origin,
                destination=request.destination,
                mode=request.mode,
                distance_meters=distance,
                duration_seconds=None,
                status=TravelStatus.PENDING_PROVIDER,
                metadata=self._metadata(),
                is_estimate=True,
                confidence=None,
                geometry=None,
            )
        duration = round(distance / (speed * 1000 / 3600))
        return RouteResult(
            origin=request.origin,
            destination=request.destination,
            mode=request.mode,
            distance_meters=distance,
            duration_seconds=duration,
            status=TravelStatus.ESTIMATED,
            metadata=self._metadata(),
            is_estimate=True,
            confidence=0.65,
            geometry=[request.origin, request.destination],
        )

    def get_travel_times(
        self,
        origin: Location,
        destinations: list[Location],
        modes: list[TravelMode],
    ) -> list[TravelTimeResult]:
        results: list[TravelTimeResult] = []
        for destination in destinations:
            for mode in modes:
                if origin.coordinates is None:
                    status = TravelStatus.INVALID_ORIGIN
                    distance = duration = None
                    confidence = None
                elif destination.coordinates is None:
                    status = TravelStatus.INVALID_DESTINATION
                    distance = duration = None
                    confidence = None
                else:
                    route = self.get_route(
                        RouteRequest(origin.coordinates, destination.coordinates, mode)
                    )
                    status = route.status
                    distance = route.distance_meters
                    duration = route.duration_seconds
                    confidence = route.confidence
                results.append(
                    TravelTimeResult(
                        origin=origin,
                        destination=destination,
                        mode=mode,
                        distance_meters=distance,
                        duration_seconds=duration,
                        status=status,
                        metadata=self._metadata(),
                        is_estimate=status is TravelStatus.ESTIMATED,
                        confidence=confidence,
                    )
                )
        return results


class UnavailableTransitProvider:
    """Explicitly represents the absence of a configured live transit provider."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get_travel_times(
        self,
        origin: Location,
        destinations: list[Location],
        modes: list[TravelMode],
    ) -> list[TravelTimeResult]:
        metadata = ProviderMetadata(
            provider="unavailable_transit",
            calculation_type="provider_unavailable",
            calculated_at=self._now(),
        )
        return [
            TravelTimeResult(
                origin=origin,
                destination=destination,
                mode=mode,
                distance_meters=None,
                duration_seconds=None,
                status=(
                    TravelStatus.INVALID_ORIGIN
                    if origin.coordinates is None
                    else TravelStatus.INVALID_DESTINATION
                    if destination.coordinates is None
                    else TravelStatus.PENDING_PROVIDER
                ),
                metadata=metadata,
                is_estimate=False,
            )
            for destination in destinations
            for mode in modes
        ]


class FixtureRoutingProvider:
    """Deterministic injectable provider for tests and local demos."""

    def __init__(self, results: dict[TravelMode, tuple[int, int]]) -> None:
        self.results = results

    def get_route(self, request: RouteRequest) -> RouteResult:
        if request.mode not in self.results:
            status = TravelStatus.UNAVAILABLE
            distance = duration = None
        else:
            status = TravelStatus.AVAILABLE
            distance, duration = self.results[request.mode]
        return RouteResult(
            origin=request.origin,
            destination=request.destination,
            mode=request.mode,
            distance_meters=distance,
            duration_seconds=duration,
            status=status,
            metadata=ProviderMetadata(
                provider="fixture",
                calculation_type="fixture",
                calculated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            is_estimate=False,
            confidence=1.0 if status is TravelStatus.AVAILABLE else None,
        )


class FixtureTransitProfileProvider:
    """Returns caller-supplied samples without performing external I/O."""

    def __init__(self, samples: dict[datetime, TravelTimeSample]) -> None:
        self.samples = dict(samples)

    def get_samples(
        self,
        origin: Location,
        destination: Location,
        departures: list[datetime],
    ) -> list[TravelTimeSample]:
        del origin, destination
        return [self.samples[departure] for departure in departures if departure in self.samples]
