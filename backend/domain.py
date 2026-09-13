"""Provider-neutral location and travel domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isfinite
from typing import Any


class TravelMode(str, Enum):
    WALKING = "walking"
    CYCLING = "cycling"
    TRANSIT = "transit"
    DRIVING = "driving"


class TravelStatus(str, Enum):
    AVAILABLE = "available"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"
    PENDING_PROVIDER = "pending_provider"
    INVALID_ORIGIN = "invalid_origin"
    INVALID_DESTINATION = "invalid_destination"


class AccessibilityResultType(str, Enum):
    EXACT_ROUTE = "exact_route"
    CACHED_EXACT_PROPERTY = "cached_exact_property"
    CACHED_EXACT_ORIGIN = "cached_exact_origin"
    SAME_STOP_REUSE = "same_stop_reuse"
    NEARBY_ORIGIN_ESTIMATE = "nearby_origin_estimate"
    STRAIGHT_LINE_FALLBACK = "straight_line_fallback"
    PENDING_PROVIDER = "pending_provider"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


class OriginType(str, Enum):
    PROPERTY = "property"
    ENTRANCE = "entrance"
    COORDINATE = "coordinate"


class DayType(str, Enum):
    WEEKDAY = "weekday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


class TimePeriod(str, Enum):
    WEEKDAY_MORNING_COMMUTE = "weekday_morning_commute"
    WEEKDAY_MIDDAY = "weekday_midday"
    WEEKDAY_EVENING_COMMUTE = "weekday_evening_commute"
    WEEKDAY_LATE_EVENING = "weekday_late_evening"
    SATURDAY_DAYTIME = "saturday_daytime"
    SUNDAY_DAYTIME = "sunday_daytime"


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not isfinite(self.latitude) or not -90 <= self.latitude <= 90:
            raise ValueError("latitude must be between -90 and 90")
        if not isfinite(self.longitude) or not -180 <= self.longitude <= 180:
            raise ValueError("longitude must be between -180 and 180")

    def to_dict(self) -> dict[str, float]:
        return {"latitude": self.latitude, "longitude": self.longitude}


@dataclass(frozen=True)
class Location:
    id: str
    name: str
    coordinates: Coordinates | None
    address: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "coordinates": self.coordinates.to_dict() if self.coordinates else None,
            "address": self.address,
        }


@dataclass(frozen=True)
class Hotspot(Location):
    category: str = "other"
    is_active: bool = True
    display_order: int = 0
    source: str = "configuration"

    def to_dict(self) -> dict[str, Any]:
        coordinates = self.coordinates
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "latitude": coordinates.latitude if coordinates else None,
            "longitude": coordinates.longitude if coordinates else None,
            "address": self.address,
            "is_active": self.is_active,
            "display_order": self.display_order,
            "source": self.source,
            "coordinate_status": "verified" if coordinates else "pending_verification",
        }


@dataclass(frozen=True)
class ProviderMetadata:
    provider: str
    calculation_type: str
    calculated_at: datetime
    expires_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "calculation_type": self.calculation_type,
            "calculated_at": self.calculated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass(frozen=True)
class RoutingDiagnostic:
    """Provider-neutral, sanitized explanation for an unavailable route."""

    code: str
    description: str
    input_field: str | None = None

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.description.strip():
            raise ValueError("routing diagnostic code and description are required")
        if len(self.code) > 100 or len(self.description) > 500:
            raise ValueError("routing diagnostic exceeds safe storage limits")
        if self.input_field is not None and len(self.input_field) > 100:
            raise ValueError("routing diagnostic input_field exceeds safe storage limits")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "description": self.description,
            "input_field": self.input_field,
        }


def _domain_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed


@dataclass(frozen=True)
class RouteStop:
    """Student-useful stop identity retained independently of an OTP payload."""

    stop_id: str
    name: str
    coordinates: Coordinates | None = None

    def __post_init__(self) -> None:
        if not self.stop_id.strip() or not self.name.strip():
            raise ValueError("route stop id and name are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_id": self.stop_id,
            "name": self.name,
            "coordinates": self.coordinates.to_dict() if self.coordinates else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RouteStop":
        coordinates = value.get("coordinates")
        return cls(
            stop_id=str(value["stop_id"]),
            name=str(value["name"]),
            coordinates=(
                Coordinates(
                    float(coordinates["latitude"]),
                    float(coordinates["longitude"]),
                )
                if isinstance(coordinates, dict)
                and coordinates.get("latitude") is not None
                and coordinates.get("longitude") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class RouteLeg:
    """One normalized, renderable leg of an actual provider itinerary."""

    mode: str
    duration_seconds: int | None
    distance_meters: int | None
    encoded_polyline: str | None = None
    geometry_point_count: int | None = None
    route_id: str | None = None
    route_short_name: str | None = None
    route_long_name: str | None = None
    from_stop: RouteStop | None = None
    to_stop: RouteStop | None = None
    scheduled_departure_at: datetime | None = None
    scheduled_arrival_at: datetime | None = None
    is_real_time: bool = False

    def __post_init__(self) -> None:
        normalized_mode = self.mode.strip().upper()
        if not normalized_mode:
            raise ValueError("route leg mode is required")
        object.__setattr__(self, "mode", normalized_mode)
        for field_name in ("duration_seconds", "distance_meters", "geometry_point_count"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if self.encoded_polyline is not None:
            if not self.encoded_polyline or len(self.encoded_polyline) > 250_000:
                raise ValueError("encoded route geometry is invalid")
            if self.geometry_point_count == 0:
                raise ValueError("route geometry point count cannot be zero")
        for timestamp in (self.scheduled_departure_at, self.scheduled_arrival_at):
            if timestamp is not None and timestamp.tzinfo is None:
                raise ValueError("route schedule timestamps must include a timezone")

    @property
    def has_geometry(self) -> bool:
        return bool(self.encoded_polyline and (self.geometry_point_count or 0) >= 2)

    def to_dict(self, *, include_geometry: bool = True) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "duration_seconds": self.duration_seconds,
            "distance_meters": self.distance_meters,
            "encoded_polyline": self.encoded_polyline if include_geometry else None,
            "geometry_point_count": self.geometry_point_count,
            "route_id": self.route_id,
            "route_short_name": self.route_short_name,
            "route_long_name": self.route_long_name,
            "from_stop": self.from_stop.to_dict() if self.from_stop else None,
            "to_stop": self.to_stop.to_dict() if self.to_stop else None,
            "scheduled_departure_at": (
                self.scheduled_departure_at.isoformat()
                if self.scheduled_departure_at
                else None
            ),
            "scheduled_arrival_at": (
                self.scheduled_arrival_at.isoformat() if self.scheduled_arrival_at else None
            ),
            "is_real_time": self.is_real_time,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RouteLeg":
        from_stop = value.get("from_stop")
        to_stop = value.get("to_stop")
        return cls(
            mode=str(value["mode"]),
            duration_seconds=value.get("duration_seconds"),
            distance_meters=value.get("distance_meters"),
            encoded_polyline=value.get("encoded_polyline"),
            geometry_point_count=value.get("geometry_point_count"),
            route_id=value.get("route_id"),
            route_short_name=value.get("route_short_name"),
            route_long_name=value.get("route_long_name"),
            from_stop=(
                RouteStop.from_dict(from_stop) if isinstance(from_stop, dict) else None
            ),
            to_stop=RouteStop.from_dict(to_stop) if isinstance(to_stop, dict) else None,
            scheduled_departure_at=_domain_datetime(
                value.get("scheduled_departure_at")
            ),
            scheduled_arrival_at=_domain_datetime(value.get("scheduled_arrival_at")),
            is_real_time=bool(value.get("is_real_time", False)),
        )


@dataclass(frozen=True)
class RouteItinerary:
    """Compact normalized itinerary; never contains a raw provider response."""

    legs: tuple[RouteLeg, ...]
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    transfer_count: int | None = None
    is_live: bool = False

    def __post_init__(self) -> None:
        if not self.legs:
            raise ValueError("route itinerary requires at least one leg")
        if self.transfer_count is not None and self.transfer_count < 0:
            raise ValueError("route itinerary transfer_count cannot be negative")
        transit_legs = [leg for leg in self.legs if leg.mode not in {"WALK", "BICYCLE"}]
        expected_transfers = max(0, len(transit_legs) - 1)
        if transit_legs and self.transfer_count != expected_transfers:
            raise ValueError("route itinerary transfer_count does not match transit legs")
        if not transit_legs and self.transfer_count not in {None, 0}:
            raise ValueError("direct route itinerary cannot contain transfers")
        for timestamp in (self.departure_at, self.arrival_at):
            if timestamp is not None and timestamp.tzinfo is None:
                raise ValueError("route itinerary timestamps must include a timezone")

    @property
    def has_geometry(self) -> bool:
        return all(leg.has_geometry for leg in self.legs)

    @property
    def transit_legs(self) -> tuple[RouteLeg, ...]:
        return tuple(leg for leg in self.legs if leg.mode not in {"WALK", "BICYCLE"})

    def to_dict(self, *, include_geometry: bool = True) -> dict[str, Any]:
        return {
            "departure_at": self.departure_at.isoformat() if self.departure_at else None,
            "arrival_at": self.arrival_at.isoformat() if self.arrival_at else None,
            "transfer_count": self.transfer_count,
            "is_live": self.is_live,
            "geometry_available": self.has_geometry,
            "legs": [
                leg.to_dict(include_geometry=include_geometry) for leg in self.legs
            ],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RouteItinerary":
        legs = value.get("legs")
        if not isinstance(legs, list):
            raise ValueError("route itinerary legs must be an array")
        return cls(
            legs=tuple(RouteLeg.from_dict(leg) for leg in legs if isinstance(leg, dict)),
            departure_at=_domain_datetime(value.get("departure_at")),
            arrival_at=_domain_datetime(value.get("arrival_at")),
            transfer_count=value.get("transfer_count"),
            is_live=bool(value.get("is_live", False)),
        )


@dataclass(frozen=True)
class RouteRequest:
    origin: Coordinates
    destination: Coordinates
    mode: TravelMode
    departure_at: datetime | None = None


@dataclass(frozen=True)
class RouteResult:
    origin: Coordinates
    destination: Coordinates
    mode: TravelMode
    distance_meters: int | None
    duration_seconds: int | None
    status: TravelStatus
    metadata: ProviderMetadata
    is_estimate: bool
    confidence: float | None = None
    geometry: list[Coordinates] | None = None
    provider_mode: str | None = None
    origin_snap_distance_meters: int | None = None
    destination_snap_distance_meters: int | None = None
    itinerary: RouteItinerary | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin.to_dict(),
            "destination": self.destination.to_dict(),
            "mode": self.mode.value,
            "distance_meters": self.distance_meters,
            "duration_seconds": self.duration_seconds,
            "status": self.status.value,
            **self.metadata.to_dict(),
            "is_estimate": self.is_estimate,
            "confidence": self.confidence,
            "geometry": (
                [point.to_dict() for point in self.geometry]
                if self.geometry is not None
                else None
            ),
            "provider_mode": self.provider_mode,
            "origin_snap_distance_meters": self.origin_snap_distance_meters,
            "destination_snap_distance_meters": self.destination_snap_distance_meters,
            "itinerary": self.itinerary.to_dict() if self.itinerary else None,
        }


@dataclass(frozen=True)
class TravelTimeResult:
    origin: Location
    destination: Location
    mode: TravelMode
    distance_meters: int | None
    duration_seconds: int | None
    status: TravelStatus
    metadata: ProviderMetadata
    is_estimate: bool
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin.to_dict(),
            "destination": self.destination.to_dict(),
            "mode": self.mode.value,
            "distance_meters": self.distance_meters,
            "duration_seconds": self.duration_seconds,
            "status": self.status.value,
            **self.metadata.to_dict(),
            "is_estimate": self.is_estimate,
            "confidence": self.confidence,
            "distance_type": (
                "straight_line"
                if self.metadata.calculation_type == "straight_line_estimate"
                else self.metadata.calculation_type
            ),
            "duration_type": "estimate" if self.is_estimate else "provider_result",
        }


@dataclass(frozen=True)
class GeocodeResult:
    query: str
    coordinates: Coordinates | None
    formatted_address: str | None
    status: str
    metadata: ProviderMetadata
    confidence: float | None = None
    provider_result_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "coordinates": self.coordinates.to_dict() if self.coordinates else None,
            "formatted_address": self.formatted_address,
            "status": self.status,
            **self.metadata.to_dict(),
            "confidence": self.confidence,
            "provider_result_id": self.provider_result_id,
        }


@dataclass(frozen=True)
class AccessibilityOrigin:
    coordinates: Coordinates | None
    property_id: int | None = None
    origin_type: OriginType = OriginType.COORDINATE
    origin_zone_id: str | None = None
    entrance_fingerprint: str | None = None
    nearest_stop_id: str | None = None
    walking_to_stop_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.walking_to_stop_seconds is not None and self.walking_to_stop_seconds < 0:
            raise ValueError("walking_to_stop_seconds cannot be negative")

    @property
    def identity_key(self) -> str | None:
        if self.property_id is not None:
            return f"property:{self.property_id}"
        if self.entrance_fingerprint:
            return f"entrance:{self.entrance_fingerprint}"
        if self.coordinates:
            return (
                f"coordinate:{self.coordinates.latitude:.6f}:"
                f"{self.coordinates.longitude:.6f}"
            )
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin_type": self.origin_type.value,
            "property_id": self.property_id,
            "origin_zone_id": self.origin_zone_id,
            "latitude": self.coordinates.latitude if self.coordinates else None,
            "longitude": self.coordinates.longitude if self.coordinates else None,
            "entrance_fingerprint": self.entrance_fingerprint,
            "nearest_stop_id": self.nearest_stop_id,
            "walking_to_stop_seconds": self.walking_to_stop_seconds,
        }


@dataclass(frozen=True)
class TravelTimeSample:
    departure_at: datetime
    duration_seconds: int | None
    walking_duration_seconds: int | None = None
    transfer_count: int | None = None
    distance_meters: int | None = None
    stop_to_destination_seconds: int | None = None
    status: TravelStatus = TravelStatus.AVAILABLE
    provider_sample_id: str | None = None
    arrival_at: datetime | None = None
    waiting_duration_seconds: int | None = None
    in_vehicle_duration_seconds: int | None = None
    origin_stop_id: str | None = None
    destination_stop_id: str | None = None
    route_ids: tuple[str, ...] = ()
    provider: str | None = None
    schedule_version: str | None = None
    network_version: str | None = None
    worker_run_id: str | None = None
    routing_diagnostics: tuple[RoutingDiagnostic, ...] = ()
    provider_metadata: dict[str, Any] | None = None
    itinerary: RouteItinerary | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "duration_seconds",
            "walking_duration_seconds",
            "transfer_count",
            "distance_meters",
            "stop_to_destination_seconds",
            "waiting_duration_seconds",
            "in_vehicle_duration_seconds",
        ):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "departure_at": self.departure_at.isoformat(),
            "duration_seconds": self.duration_seconds,
            "walking_duration_seconds": self.walking_duration_seconds,
            "transfer_count": self.transfer_count,
            "distance_meters": self.distance_meters,
            "stop_to_destination_seconds": self.stop_to_destination_seconds,
            "status": self.status.value,
            "provider_sample_id": self.provider_sample_id,
            "arrival_at": self.arrival_at.isoformat() if self.arrival_at else None,
            "waiting_duration_seconds": self.waiting_duration_seconds,
            "in_vehicle_duration_seconds": self.in_vehicle_duration_seconds,
            "origin_stop_id": self.origin_stop_id,
            "destination_stop_id": self.destination_stop_id,
            "route_ids": list(self.route_ids),
            "provider": self.provider,
            "schedule_version": self.schedule_version,
            "network_version": self.network_version,
            "worker_run_id": self.worker_run_id,
            "routing_reason_codes": [
                diagnostic.code for diagnostic in self.routing_diagnostics
            ],
            "routing_diagnostics": [
                diagnostic.to_dict() for diagnostic in self.routing_diagnostics
            ],
            "itinerary": self.itinerary.to_dict() if self.itinerary else None,
        }


@dataclass(frozen=True)
class AccessibilityProfile:
    profile_id: int | None
    origin: AccessibilityOrigin
    hotspot_id: str
    travel_mode: TravelMode
    day_type: DayType | None
    time_period: TimePeriod | None
    representative_duration_seconds: int | None
    minimum_duration_seconds: int | None
    maximum_duration_seconds: int | None
    distance_meters: int | None
    walking_duration_seconds: int | None
    transfer_count: int | None
    nearest_stop_id: str | None
    stop_to_destination_seconds: int | None
    provider: str
    provider_profile: str
    result_type: AccessibilityResultType
    confidence: float | None
    sample_count: int
    calculated_at: datetime
    schedule_version: str | None = None
    network_version: str | None = None
    expires_at: datetime | None = None
    source_profile_id: int | None = None
    estimation_distance_meters: int | None = None
    estimation_method: str | None = None
    is_stale: bool = False
    stale_reason: str | None = None
    hotspot_fingerprint: str | None = None
    origin_fingerprint: str | None = None
    requested_sample_count: int = 0
    quality_status: str | None = None
    quality_reason_codes: tuple[str, ...] = ()
    worker_run_id: str | None = None
    provider_metadata: dict[str, Any] | None = None
    representative_sample_departure_at: datetime | None = None
    route_itinerary: RouteItinerary | None = None

    def __post_init__(self) -> None:
        if not self.hotspot_id.strip() or not self.provider.strip() or not self.provider_profile.strip():
            raise ValueError("hotspot, provider, and provider_profile are required")
        for field_name in (
            "representative_duration_seconds",
            "minimum_duration_seconds",
            "maximum_duration_seconds",
            "distance_meters",
            "walking_duration_seconds",
            "transfer_count",
            "stop_to_destination_seconds",
            "sample_count",
            "requested_sample_count",
            "estimation_distance_meters",
        ):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if (
            self.representative_sample_departure_at is not None
            and self.representative_sample_departure_at.tzinfo is None
        ):
            raise ValueError("representative sample departure must include a timezone")

    def is_current(self, at: datetime) -> bool:
        return not self.is_stale and (self.expires_at is None or self.expires_at > at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            **self.origin.to_dict(),
            "hotspot_id": self.hotspot_id,
            "mode": self.travel_mode.value,
            "day_type": self.day_type.value if self.day_type else None,
            "time_period": self.time_period.value if self.time_period else None,
            "representative_duration_seconds": self.representative_duration_seconds,
            "minimum_duration_seconds": self.minimum_duration_seconds,
            "maximum_duration_seconds": self.maximum_duration_seconds,
            "distance_meters": self.distance_meters,
            "walking_duration_seconds": self.walking_duration_seconds,
            "transfer_count": self.transfer_count,
            "nearest_stop_id": self.nearest_stop_id,
            "stop_to_destination_seconds": self.stop_to_destination_seconds,
            "provider": self.provider,
            "provider_profile": self.provider_profile,
            "result_type": self.result_type.value,
            "confidence": self.confidence,
            "sample_count": self.sample_count,
            "calculated_at": self.calculated_at.isoformat(),
            "schedule_version": self.schedule_version,
            "network_version": self.network_version,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "source_profile_id": self.source_profile_id,
            "estimation_distance_meters": self.estimation_distance_meters,
            "estimation_method": self.estimation_method,
            "is_stale": self.is_stale,
            "stale_reason": self.stale_reason,
            "requested_sample_count": self.requested_sample_count,
            "quality_status": self.quality_status,
            "quality_reason_codes": list(self.quality_reason_codes),
            "worker_run_id": self.worker_run_id,
            "representative_sample_departure_at": (
                self.representative_sample_departure_at.isoformat()
                if self.representative_sample_departure_at
                else None
            ),
            "route_itinerary": (
                self.route_itinerary.to_dict() if self.route_itinerary else None
            ),
        }


@dataclass(frozen=True)
class AccessibilityRequest:
    origin: AccessibilityOrigin
    hotspot: Hotspot
    travel_mode: TravelMode
    time_period: TimePeriod | None
    provider: str
    provider_profile: str
    requested_at: datetime
    schedule_version: str | None = None
    network_version: str | None = None
    hotspot_fingerprint: str | None = None
    origin_fingerprint: str | None = None


@dataclass(frozen=True)
class AccessibilityReuseDecision:
    result: AccessibilityProfile
    result_type: AccessibilityResultType
    reuse_reason: str
    is_estimate: bool
    freshness: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.result.to_dict(),
            "result_type": self.result_type.value,
            "reuse_explanation": self.reuse_reason,
            "is_estimate": self.is_estimate,
            "freshness": self.freshness,
        }
