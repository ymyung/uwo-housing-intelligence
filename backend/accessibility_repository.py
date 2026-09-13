"""Persistent and in-memory stores for reusable location accessibility profiles."""

from __future__ import annotations

import hashlib
import json
import math
import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Protocol

from backend.domain import (
    AccessibilityOrigin,
    AccessibilityProfile,
    AccessibilityRequest,
    AccessibilityResultType,
    Coordinates,
    DayType,
    OriginType,
    RouteItinerary,
    RoutingDiagnostic,
    TimePeriod,
    TravelMode,
    TravelStatus,
    TravelTimeSample,
)
from backend.providers import haversine_distance_meters


def _compatible(profile: AccessibilityProfile, request: AccessibilityRequest) -> bool:
    if (
        profile.hotspot_id != request.hotspot.id
        or profile.travel_mode is not request.travel_mode
        or profile.provider != request.provider
        or profile.provider_profile != request.provider_profile
        or profile.network_version != request.network_version
        or (
            request.hotspot_fingerprint is not None
            and profile.hotspot_fingerprint != request.hotspot_fingerprint
        )
        or (
            request.origin_fingerprint is not None
            and profile.origin_fingerprint != request.origin_fingerprint
        )
    ):
        return False
    if request.travel_mode is TravelMode.TRANSIT:
        return (
            profile.time_period is request.time_period
            and profile.schedule_version == request.schedule_version
        )
    return True


class AccessibilityRepository(Protocol):
    def find_exact_property(self, request: AccessibilityRequest) -> list[AccessibilityProfile]: ...

    def find_exact_origin(self, request: AccessibilityRequest) -> list[AccessibilityProfile]: ...

    def find_nearby_profiles(
        self, request: AccessibilityRequest, radius_meters: int
    ) -> list[AccessibilityProfile]: ...

    def find_same_stop_profiles(self, request: AccessibilityRequest) -> list[AccessibilityProfile]: ...

    def save_profile(self, profile: AccessibilityProfile) -> AccessibilityProfile: ...

    def save_samples(self, profile_id: int, samples: list[TravelTimeSample]) -> None: ...

    def load_samples(self, profile_id: int) -> list[TravelTimeSample]: ...

    def load_samples_for_profiles(
        self, profile_ids: list[int]
    ) -> dict[int, list[TravelTimeSample]]: ...

    def save_profile_with_samples(
        self,
        profile: AccessibilityProfile,
        samples: list[TravelTimeSample],
        *,
        replace_profile_id: int | None = None,
    ) -> AccessibilityProfile: ...

    def save_replacement_with_samples(
        self,
        prior_profile_id: int,
        profile: AccessibilityProfile,
        samples: list[TravelTimeSample],
    ) -> AccessibilityProfile: ...

    def save_reuse_decision(self, decision: dict[str, Any]) -> None: ...

    def invalidate_stale(
        self,
        at: datetime,
        *,
        schedule_version: str | None = None,
        network_version: str | None = None,
    ) -> int: ...

    def profiles_due_for_refresh(
        self, before: datetime, limit: int = 100
    ) -> list[AccessibilityProfile]: ...

    def find_current_property_profiles(
        self,
        property_ids: list[int],
        hotspot_id: str,
        *,
        at: datetime,
        provider: str,
        provider_profile: str,
        schedule_version: str | None,
        network_version: str | None,
    ) -> list[AccessibilityProfile]: ...


def profile_cache_identity(profile: AccessibilityProfile) -> str:
    payload = {
        "origin": profile.origin.identity_key,
        "origin_coordinates": (
            [
                round(profile.origin.coordinates.latitude, 6),
                round(profile.origin.coordinates.longitude, 6),
            ]
            if profile.origin.coordinates
            else None
        ),
        "entrance": profile.origin.entrance_fingerprint,
        "hotspot": profile.hotspot_id,
        "mode": profile.travel_mode.value,
        "day_type": profile.day_type.value if profile.day_type else None,
        "time_period": profile.time_period.value if profile.time_period else None,
        "provider": profile.provider,
        "provider_profile": profile.provider_profile,
        "schedule_version": profile.schedule_version,
        "network_version": profile.network_version,
    }
    if profile.hotspot_fingerprint is not None:
        payload["hotspot_fingerprint"] = profile.hotspot_fingerprint
    if profile.origin_fingerprint is not None:
        payload["origin_fingerprint"] = profile.origin_fingerprint
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class InMemoryAccessibilityRepository:
    def __init__(self, profiles: list[AccessibilityProfile] | None = None) -> None:
        self.profiles: list[AccessibilityProfile] = []
        self.samples: dict[int, list[TravelTimeSample]] = {}
        self.reuse_history: list[dict[str, Any]] = []
        self._next_id = 1
        for profile in profiles or []:
            self.save_profile(profile)

    def _matching(self, request: AccessibilityRequest) -> list[AccessibilityProfile]:
        return [profile for profile in self.profiles if _compatible(profile, request)]

    def find_exact_property(self, request: AccessibilityRequest) -> list[AccessibilityProfile]:
        if request.origin.property_id is None:
            return []
        return [
            profile
            for profile in self._matching(request)
            if profile.origin.property_id == request.origin.property_id
        ]

    def find_exact_origin(self, request: AccessibilityRequest) -> list[AccessibilityProfile]:
        coordinates = request.origin.coordinates
        if coordinates is None:
            return []
        output = []
        for profile in self._matching(request):
            candidate = profile.origin.coordinates
            same_entrance = bool(
                request.origin.entrance_fingerprint
                and request.origin.entrance_fingerprint
                == profile.origin.entrance_fingerprint
            )
            if candidate:
                distance = haversine_distance_meters(coordinates, candidate)
                if distance <= 3 or (same_entrance and distance <= 3):
                    output.append(profile)
        return output

    def find_nearby_profiles(
        self, request: AccessibilityRequest, radius_meters: int
    ) -> list[AccessibilityProfile]:
        coordinates = request.origin.coordinates
        if coordinates is None:
            return []
        return [
            profile
            for profile in self._matching(request)
            if profile.origin.coordinates
            and haversine_distance_meters(coordinates, profile.origin.coordinates)
            <= radius_meters
        ]

    def find_same_stop_profiles(self, request: AccessibilityRequest) -> list[AccessibilityProfile]:
        stop_id = request.origin.nearest_stop_id
        if not stop_id:
            return []
        return [
            profile
            for profile in self._matching(request)
            if profile.nearest_stop_id == stop_id
            and profile.stop_to_destination_seconds is not None
        ]

    def save_profile(self, profile: AccessibilityProfile) -> AccessibilityProfile:
        identity = profile_cache_identity(profile)
        for index, existing in enumerate(self.profiles):
            if not existing.is_stale and profile_cache_identity(existing) == identity:
                stored = replace(profile, profile_id=existing.profile_id)
                self.profiles[index] = stored
                return stored
        stored = replace(profile, profile_id=profile.profile_id or self._next_id)
        self._next_id = max(self._next_id, (stored.profile_id or 0) + 1)
        self.profiles.append(stored)
        return stored

    def save_samples(self, profile_id: int, samples: list[TravelTimeSample]) -> None:
        by_departure = {
            sample.departure_at: sample for sample in self.samples.get(profile_id, [])
        }
        by_departure.update({sample.departure_at: sample for sample in samples})
        self.samples[profile_id] = [by_departure[key] for key in sorted(by_departure)]

    def load_samples(self, profile_id: int) -> list[TravelTimeSample]:
        return list(self.samples.get(profile_id, []))

    def load_samples_for_profiles(
        self, profile_ids: list[int]
    ) -> dict[int, list[TravelTimeSample]]:
        return {
            profile_id: list(self.samples.get(profile_id, []))
            for profile_id in profile_ids
        }

    def save_profile_with_samples(
        self,
        profile: AccessibilityProfile,
        samples: list[TravelTimeSample],
        *,
        replace_profile_id: int | None = None,
    ) -> AccessibilityProfile:
        profiles_before = list(self.profiles)
        samples_before = {key: list(value) for key, value in self.samples.items()}
        next_id_before = self._next_id
        try:
            if replace_profile_id is not None:
                self.profiles = [
                    candidate
                    for candidate in self.profiles
                    if candidate.profile_id != replace_profile_id
                ]
                profile = replace(profile, profile_id=replace_profile_id)
            stored = self.save_profile(profile)
            if stored.profile_id is None:
                raise RuntimeError("Stored profile has no identity")
            self.save_samples(stored.profile_id, samples)
            return stored
        except Exception:
            self.profiles = profiles_before
            self.samples = samples_before
            self._next_id = next_id_before
            raise

    def save_replacement_with_samples(
        self,
        prior_profile_id: int,
        profile: AccessibilityProfile,
        samples: list[TravelTimeSample],
    ) -> AccessibilityProfile:
        profiles_before = list(self.profiles)
        samples_before = {key: list(value) for key, value in self.samples.items()}
        next_id_before = self._next_id
        try:
            found = False
            for index, candidate in enumerate(self.profiles):
                if candidate.profile_id == prior_profile_id:
                    self.profiles[index] = replace(
                        candidate, is_stale=True, stale_reason="worker_replaced"
                    )
                    found = True
                    break
            if not found:
                raise ValueError(f"Unknown accessibility profile {prior_profile_id}")
            return self.save_profile_with_samples(profile, samples)
        except Exception:
            self.profiles = profiles_before
            self.samples = samples_before
            self._next_id = next_id_before
            raise

    def save_reuse_decision(self, decision: dict[str, Any]) -> None:
        self.reuse_history.append(dict(decision))

    def invalidate_stale(
        self,
        at: datetime,
        *,
        schedule_version: str | None = None,
        network_version: str | None = None,
    ) -> int:
        count = 0
        for index, profile in enumerate(self.profiles):
            version_stale = (
                schedule_version is not None
                and profile.travel_mode is TravelMode.TRANSIT
                and profile.schedule_version != schedule_version
            ) or (
                network_version is not None
                and profile.network_version != network_version
            )
            expired = profile.expires_at is not None and profile.expires_at <= at
            if not profile.is_stale and (version_stale or expired):
                reason = "version_changed" if version_stale else "expired"
                self.profiles[index] = replace(
                    profile, is_stale=True, stale_reason=reason
                )
                count += 1
        return count

    def profiles_due_for_refresh(
        self, before: datetime, limit: int = 100
    ) -> list[AccessibilityProfile]:
        due = [
            profile
            for profile in self.profiles
            if profile.is_stale
            or (profile.expires_at is not None and profile.expires_at <= before)
        ]
        due.sort(key=lambda profile: (profile.expires_at or profile.calculated_at, profile.profile_id or 0))
        return due[:limit]

    def find_current_property_profiles(
        self,
        property_ids: list[int],
        hotspot_id: str,
        *,
        at: datetime,
        provider: str,
        provider_profile: str,
        schedule_version: str | None,
        network_version: str | None,
    ) -> list[AccessibilityProfile]:
        selected = set(property_ids)
        return [
            profile
            for profile in self.profiles
            if profile.origin.property_id in selected
            and profile.hotspot_id == hotspot_id
            and profile.provider == provider
            and profile.provider_profile == provider_profile
            and profile.network_version == network_version
            and (
                profile.travel_mode is not TravelMode.TRANSIT
                or profile.schedule_version == schedule_version
            )
            and profile.is_current(at)
        ]


def _fixture_datetime(value: Any, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid accessibility fixture {field_name}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Accessibility fixture {field_name} must include a timezone")
    return parsed


def _fixture_profile(value: dict[str, Any]) -> AccessibilityProfile:
    origin_value = value.get("origin")
    if not isinstance(origin_value, dict):
        raise ValueError("Accessibility fixture profile origin must be an object")
    latitude = origin_value.get("latitude")
    longitude = origin_value.get("longitude")
    coordinates = (
        Coordinates(float(latitude), float(longitude))
        if latitude is not None and longitude is not None
        else None
    )
    if (latitude is None) != (longitude is None):
        raise ValueError("Accessibility fixture origin coordinates must be paired")
    expires_at = value.get("expires_at")
    representative_departure = value.get("representative_sample_departure_at")
    route_itinerary = value.get("route_itinerary")
    return AccessibilityProfile(
        profile_id=int(value["profile_id"]),
        origin=AccessibilityOrigin(
            coordinates=coordinates,
            property_id=(
                int(origin_value["property_id"])
                if origin_value.get("property_id") is not None
                else None
            ),
            origin_type=OriginType(origin_value["origin_type"]),
            origin_zone_id=origin_value.get("origin_zone_id"),
            entrance_fingerprint=origin_value.get("entrance_fingerprint"),
            nearest_stop_id=origin_value.get("nearest_stop_id"),
            walking_to_stop_seconds=origin_value.get("walking_to_stop_seconds"),
        ),
        hotspot_id=str(value["hotspot_id"]),
        travel_mode=TravelMode(value["travel_mode"]),
        day_type=DayType(value["day_type"]) if value.get("day_type") else None,
        time_period=(
            TimePeriod(value["time_period"]) if value.get("time_period") else None
        ),
        representative_duration_seconds=value.get("representative_duration_seconds"),
        minimum_duration_seconds=value.get("minimum_duration_seconds"),
        maximum_duration_seconds=value.get("maximum_duration_seconds"),
        distance_meters=value.get("distance_meters"),
        walking_duration_seconds=value.get("walking_duration_seconds"),
        transfer_count=value.get("transfer_count"),
        nearest_stop_id=value.get("nearest_stop_id"),
        stop_to_destination_seconds=value.get("stop_to_destination_seconds"),
        provider=str(value["provider"]),
        provider_profile=str(value["provider_profile"]),
        result_type=AccessibilityResultType(value["result_type"]),
        confidence=value.get("confidence"),
        sample_count=int(value.get("sample_count", 0)),
        calculated_at=_fixture_datetime(value["calculated_at"], field_name="calculated_at"),
        schedule_version=value.get("schedule_version"),
        network_version=value.get("network_version"),
        expires_at=(
            _fixture_datetime(expires_at, field_name="expires_at")
            if expires_at
            else None
        ),
        source_profile_id=value.get("source_profile_id"),
        estimation_distance_meters=value.get("estimation_distance_meters"),
        estimation_method=value.get("estimation_method"),
        is_stale=bool(value.get("is_stale", False)),
        stale_reason=value.get("stale_reason"),
        representative_sample_departure_at=(
            _fixture_datetime(
                representative_departure,
                field_name="representative_sample_departure_at",
            )
            if representative_departure
            else None
        ),
        route_itinerary=(
            RouteItinerary.from_dict(route_itinerary)
            if isinstance(route_itinerary, dict)
            else None
        ),
    )


def _fixture_sample(value: dict[str, Any]) -> TravelTimeSample:
    route_itinerary = value.get("route_itinerary") or value.get("itinerary")
    return TravelTimeSample(
        departure_at=_fixture_datetime(value["departure_at"], field_name="departure_at"),
        duration_seconds=value.get("duration_seconds"),
        walking_duration_seconds=value.get("walking_duration_seconds"),
        transfer_count=value.get("transfer_count"),
        distance_meters=value.get("distance_meters"),
        stop_to_destination_seconds=value.get("stop_to_destination_seconds"),
        status=TravelStatus(value.get("status", "available")),
        provider_sample_id=value.get("provider_sample_id"),
        itinerary=(
            RouteItinerary.from_dict(route_itinerary)
            if isinstance(route_itinerary, dict)
            else None
        ),
    )


class FixtureAccessibilityRepository(InMemoryAccessibilityRepository):
    """Strict, deterministic JSON fixture store that performs no external I/O."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not load accessibility fixture: {path}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError(
                f"Accessibility fixture schema_version must be {self.SCHEMA_VERSION}"
            )
        if payload.get("fixture_only") is not True:
            raise ValueError("Accessibility fixture must declare fixture_only=true")
        profiles_value = payload.get("profiles")
        if not isinstance(profiles_value, list):
            raise ValueError("Accessibility fixture profiles must be an array")
        required_configuration = (
            "provider",
            "provider_profile",
            "schedule_version",
            "network_version",
            "warning",
        )
        if any(not str(payload.get(key) or "").strip() for key in required_configuration):
            raise ValueError("Accessibility fixture configuration is incomplete")
        self.fixture_configuration = {
            key: payload.get(key)
            for key in required_configuration
        }
        self.fixture_path = path
        super().__init__([_fixture_profile(profile) for profile in profiles_value])
        samples_value = payload.get("samples", [])
        history_value = payload.get("reuse_history", [])
        if not isinstance(samples_value, list) or not isinstance(history_value, list):
            raise ValueError("Accessibility fixture samples and reuse_history must be arrays")
        profile_ids = {profile.profile_id for profile in self.profiles}
        for sample_value in samples_value:
            profile_id = int(sample_value["profile_id"])
            if profile_id not in profile_ids:
                raise ValueError(
                    f"Accessibility fixture sample references unknown profile {profile_id}"
                )
            self.save_samples(profile_id, [_fixture_sample(sample_value)])
        for decision in history_value:
            if not isinstance(decision, dict):
                raise ValueError("Accessibility fixture reuse history must contain objects")
            self.save_reuse_decision(decision)


PROFILE_COLUMNS = """
id, origin_type, origin_property_id, origin_zone_id, origin_latitude,
origin_longitude, entrance_fingerprint, hotspot_id, travel_mode, day_type,
time_period, representative_duration_seconds, minimum_duration_seconds,
maximum_duration_seconds, distance_meters, walking_duration_seconds,
transfer_count, nearest_stop_id, stop_to_destination_seconds, provider,
provider_profile, result_type, confidence, sample_count, calculated_at,
schedule_version, network_version, expires_at, source_profile_id,
estimation_distance_meters, estimation_method, is_stale, stale_reason,
origin_walking_to_stop_seconds, provider_metadata,
representative_sample_departure_at, route_itinerary
""".replace("\n", " ")

SAMPLE_COLUMNS = """
departure_at, duration_seconds, walking_duration_seconds, transfer_count,
distance_meters, stop_to_destination_seconds, status, provider_sample_id,
provider_metadata, route_itinerary
""".replace("\n", " ")


def _metadata_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _profile_metadata(profile: AccessibilityProfile) -> dict[str, Any]:
    metadata = dict(profile.provider_metadata or {})
    metadata.update(
        {
            "hotspot_fingerprint": profile.hotspot_fingerprint,
            "origin_fingerprint": profile.origin_fingerprint,
            "requested_sample_count": profile.requested_sample_count,
            "quality_status": profile.quality_status,
            "quality_reason_codes": list(profile.quality_reason_codes),
            "worker_run_id": profile.worker_run_id,
        }
    )
    return {key: value for key, value in metadata.items() if value is not None}


def _sample_metadata(sample: TravelTimeSample) -> dict[str, Any]:
    metadata = dict(sample.provider_metadata or {})
    metadata.update(
        {
            "arrival_at": sample.arrival_at.isoformat() if sample.arrival_at else None,
            "waiting_duration_seconds": sample.waiting_duration_seconds,
            "in_vehicle_duration_seconds": sample.in_vehicle_duration_seconds,
            "origin_stop_id": sample.origin_stop_id,
            "destination_stop_id": sample.destination_stop_id,
            "route_ids": list(sample.route_ids),
            "provider": sample.provider,
            "schedule_version": sample.schedule_version,
            "network_version": sample.network_version,
            "worker_run_id": sample.worker_run_id,
            "routing_diagnostics": [
                diagnostic.to_dict() for diagnostic in sample.routing_diagnostics
            ],
        }
    )
    return {key: value for key, value in metadata.items() if value is not None}


def _optional_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed


def _route_itinerary(value: Any) -> RouteItinerary | None:
    if value is None:
        return None
    payload = _metadata_object(value)
    return RouteItinerary.from_dict(payload) if payload else None


def _profile_from_row(row: tuple[Any, ...]) -> AccessibilityProfile:
    metadata = _metadata_object(row[34])
    return AccessibilityProfile(
        profile_id=row[0],
        origin=AccessibilityOrigin(
            origin_type=OriginType(row[1]),
            property_id=row[2],
            origin_zone_id=row[3],
            coordinates=(
                Coordinates(float(row[4]), float(row[5]))
                if row[4] is not None and row[5] is not None
                else None
            ),
            entrance_fingerprint=row[6],
            nearest_stop_id=row[17],
            walking_to_stop_seconds=row[33],
        ),
        hotspot_id=row[7],
        travel_mode=TravelMode(row[8]),
        day_type=DayType(row[9]) if row[9] else None,
        time_period=TimePeriod(row[10]) if row[10] else None,
        representative_duration_seconds=row[11],
        minimum_duration_seconds=row[12],
        maximum_duration_seconds=row[13],
        distance_meters=row[14],
        walking_duration_seconds=row[15],
        transfer_count=row[16],
        nearest_stop_id=row[17],
        stop_to_destination_seconds=row[18],
        provider=row[19],
        provider_profile=row[20],
        result_type=AccessibilityResultType(row[21]),
        confidence=float(row[22]) if row[22] is not None else None,
        sample_count=row[23],
        calculated_at=row[24],
        schedule_version=row[25],
        network_version=row[26],
        expires_at=row[27],
        source_profile_id=row[28],
        estimation_distance_meters=row[29],
        estimation_method=row[30],
        is_stale=row[31],
        stale_reason=row[32],
        hotspot_fingerprint=metadata.get("hotspot_fingerprint"),
        origin_fingerprint=metadata.get("origin_fingerprint"),
        requested_sample_count=int(metadata.get("requested_sample_count") or 0),
        quality_status=metadata.get("quality_status"),
        quality_reason_codes=tuple(metadata.get("quality_reason_codes") or ()),
        worker_run_id=metadata.get("worker_run_id"),
        provider_metadata=metadata,
        representative_sample_departure_at=row[35],
        route_itinerary=_route_itinerary(row[36]),
    )


def _sample_from_row(row: tuple[Any, ...]) -> TravelTimeSample:
    metadata = _metadata_object(row[8])
    return TravelTimeSample(
        departure_at=row[0],
        duration_seconds=row[1],
        walking_duration_seconds=row[2],
        transfer_count=row[3],
        distance_meters=row[4],
        stop_to_destination_seconds=row[5],
        status=TravelStatus(row[6]),
        provider_sample_id=row[7],
        arrival_at=_optional_datetime(metadata.get("arrival_at")),
        waiting_duration_seconds=metadata.get("waiting_duration_seconds"),
        in_vehicle_duration_seconds=metadata.get("in_vehicle_duration_seconds"),
        origin_stop_id=metadata.get("origin_stop_id"),
        destination_stop_id=metadata.get("destination_stop_id"),
        route_ids=tuple(metadata.get("route_ids") or ()),
        provider=metadata.get("provider"),
        schedule_version=metadata.get("schedule_version"),
        network_version=metadata.get("network_version"),
        worker_run_id=metadata.get("worker_run_id"),
        routing_diagnostics=tuple(
            RoutingDiagnostic(
                code=str(value["code"]),
                description=str(value["description"]),
                input_field=(
                    str(value["input_field"])
                    if value.get("input_field") is not None
                    else None
                ),
            )
            for value in metadata.get("routing_diagnostics") or ()
            if isinstance(value, dict)
            and value.get("code")
            and value.get("description")
        ),
        provider_metadata=metadata,
        itinerary=_route_itinerary(row[9]),
    )


class PostgresAccessibilityRepository:
    """Psycopg adapter; construction and import perform no connection."""

    def __init__(self, database_url: str) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self.database_url = database_url

    @classmethod
    def from_environment(cls) -> "PostgresAccessibilityRepository":
        database_url = (
            os.getenv("DATABASE_URL", "").strip()
            or os.getenv("ACCESSIBILITY_DATABASE_URL", "").strip()
        )
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL or ACCESSIBILITY_DATABASE_URL is not configured"
            )
        return cls(database_url)

    @contextmanager
    def connect(self) -> Iterator[Any]:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install requirements-database.txt for PostgreSQL access") from exc
        with psycopg.connect(self.database_url) as connection:
            yield connection

    def _profiles(self, where: str, parameters: tuple[Any, ...]) -> list[AccessibilityProfile]:
        with self.connect() as connection:
            rows = connection.execute(
                f"select {PROFILE_COLUMNS} from public.housing_accessibility_profiles where {where}",
                parameters,
            ).fetchall()
        return [_profile_from_row(tuple(row)) for row in rows]

    @staticmethod
    def _base_parameters(request: AccessibilityRequest) -> tuple[Any, ...]:
        return (
            request.hotspot.id,
            request.travel_mode.value,
            request.provider,
            request.provider_profile,
            request.network_version,
            request.time_period.value if request.time_period else None,
            request.schedule_version,
            request.hotspot_fingerprint,
            request.hotspot_fingerprint,
            request.origin_fingerprint,
            request.origin_fingerprint,
        )

    @staticmethod
    def _base_where() -> str:
        return """
            hotspot_id = %s and travel_mode = %s and provider = %s
            and provider_profile = %s and network_version is not distinct from %s
            and (
                travel_mode <> 'transit'
                or (
                    time_period is not distinct from %s
                    and schedule_version is not distinct from %s
                )
            )
            and (%s::text is null or provider_metadata ->> 'hotspot_fingerprint' = %s)
            and (%s::text is null or provider_metadata ->> 'origin_fingerprint' = %s)
        """

    def find_exact_property(self, request: AccessibilityRequest) -> list[AccessibilityProfile]:
        if request.origin.property_id is None:
            return []
        return self._profiles(
            self._base_where() + " and origin_property_id = %s",
            self._base_parameters(request) + (request.origin.property_id,),
        )

    def find_exact_origin(self, request: AccessibilityRequest) -> list[AccessibilityProfile]:
        origin = request.origin
        if origin.coordinates is None:
            return []
        return self._profiles(
            self._base_where()
            + " and origin_latitude between %s and %s and origin_longitude between %s and %s",
            self._base_parameters(request)
            + (
                origin.coordinates.latitude - 0.00004,
                origin.coordinates.latitude + 0.00004,
                origin.coordinates.longitude - 0.00005,
                origin.coordinates.longitude + 0.00005,
            ),
        )

    def find_nearby_profiles(
        self, request: AccessibilityRequest, radius_meters: int
    ) -> list[AccessibilityProfile]:
        origin = request.origin.coordinates
        if origin is None:
            return []
        latitude_delta = radius_meters / 111_000
        longitude_scale = max(0.1, math.cos(math.radians(origin.latitude)))
        longitude_delta = radius_meters / (111_000 * longitude_scale)
        candidates = self._profiles(
            self._base_where()
            + " and origin_latitude between %s and %s and origin_longitude between %s and %s",
            self._base_parameters(request)
            + (
                origin.latitude - latitude_delta,
                origin.latitude + latitude_delta,
                origin.longitude - longitude_delta,
                origin.longitude + longitude_delta,
            ),
        )
        return [
            profile
            for profile in candidates
            if profile.origin.coordinates
            and haversine_distance_meters(origin, profile.origin.coordinates) <= radius_meters
        ]

    def find_same_stop_profiles(self, request: AccessibilityRequest) -> list[AccessibilityProfile]:
        if not request.origin.nearest_stop_id:
            return []
        return self._profiles(
            self._base_where()
            + " and nearest_stop_id = %s and stop_to_destination_seconds is not null",
            self._base_parameters(request) + (request.origin.nearest_stop_id,),
        )

    def find_current_property_profiles(
        self,
        property_ids: list[int],
        hotspot_id: str,
        *,
        at: datetime,
        provider: str,
        provider_profile: str,
        schedule_version: str | None,
        network_version: str | None,
    ) -> list[AccessibilityProfile]:
        if not property_ids:
            return []
        return self._profiles(
            """
            origin_property_id = any(%s) and hotspot_id = %s
            and provider = %s and provider_profile = %s
            and network_version is not distinct from %s
            and (
                travel_mode <> 'transit'
                or schedule_version is not distinct from %s
            )
            and not is_stale and (expires_at is null or expires_at > %s)
            """,
            (
                property_ids,
                hotspot_id,
                provider,
                provider_profile,
                network_version,
                schedule_version,
                at,
            ),
        )

    @staticmethod
    def _profile_values(profile: AccessibilityProfile) -> tuple[Any, ...]:
        identity = profile_cache_identity(profile)
        origin = profile.origin
        return (
            identity,
            origin.identity_key,
            origin.origin_type.value,
            origin.property_id,
            origin.origin_zone_id,
            origin.coordinates.latitude if origin.coordinates else None,
            origin.coordinates.longitude if origin.coordinates else None,
            origin.entrance_fingerprint,
            profile.hotspot_id,
            profile.travel_mode.value,
            profile.day_type.value if profile.day_type else None,
            profile.time_period.value if profile.time_period else None,
            profile.representative_duration_seconds,
            profile.minimum_duration_seconds,
            profile.maximum_duration_seconds,
            profile.distance_meters,
            profile.walking_duration_seconds,
            profile.transfer_count,
            profile.nearest_stop_id,
            profile.stop_to_destination_seconds,
            profile.provider,
            profile.provider_profile,
            profile.result_type.value,
            profile.confidence,
            profile.sample_count,
            profile.calculated_at,
            profile.schedule_version,
            profile.network_version,
            profile.expires_at,
            profile.source_profile_id,
            profile.estimation_distance_meters,
            profile.estimation_method,
            profile.is_stale,
            profile.stale_reason,
            origin.walking_to_stop_seconds,
            json.dumps(_profile_metadata(profile), sort_keys=True),
            profile.representative_sample_departure_at,
            (
                json.dumps(profile.route_itinerary.to_dict(), sort_keys=True)
                if profile.route_itinerary
                else None
            ),
        )

    def _save_profile(
        self,
        connection: Any,
        profile: AccessibilityProfile,
        *,
        replace_profile_id: int | None = None,
    ) -> AccessibilityProfile:
        values = self._profile_values(profile)
        if replace_profile_id is not None:
            row = connection.execute(
                """
                update public.housing_accessibility_profiles set
                    cache_identity=%s, origin_key=%s, origin_type=%s,
                    origin_property_id=%s, origin_zone_id=%s,
                    origin_latitude=%s, origin_longitude=%s,
                    entrance_fingerprint=%s, hotspot_id=%s, travel_mode=%s,
                    day_type=%s, time_period=%s,
                    representative_duration_seconds=%s,
                    minimum_duration_seconds=%s, maximum_duration_seconds=%s,
                    distance_meters=%s, walking_duration_seconds=%s,
                    transfer_count=%s, nearest_stop_id=%s,
                    stop_to_destination_seconds=%s, provider=%s,
                    provider_profile=%s, result_type=%s, confidence=%s,
                    sample_count=%s, calculated_at=%s, schedule_version=%s,
                    network_version=%s, expires_at=%s, source_profile_id=%s,
                    estimation_distance_meters=%s, estimation_method=%s,
                    is_stale=%s, stale_reason=%s,
                    origin_walking_to_stop_seconds=%s,
                    provider_metadata=%s::jsonb,
                    representative_sample_departure_at=%s,
                    route_itinerary=%s::jsonb, updated_at=now()
                where id=%s
                returning id
                """,
                values + (replace_profile_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown accessibility profile {replace_profile_id}")
            return replace(profile, profile_id=row[0])

        row = connection.execute(
                """
                insert into public.housing_accessibility_profiles (
                    cache_identity, origin_key, origin_type, origin_property_id,
                    origin_zone_id, origin_latitude, origin_longitude,
                    entrance_fingerprint, hotspot_id, travel_mode, day_type,
                    time_period, representative_duration_seconds,
                    minimum_duration_seconds, maximum_duration_seconds,
                    distance_meters, walking_duration_seconds, transfer_count,
                    nearest_stop_id, stop_to_destination_seconds, provider,
                    provider_profile, result_type, confidence, sample_count,
                    calculated_at, schedule_version, network_version, expires_at,
                    source_profile_id, estimation_distance_meters,
                    estimation_method, is_stale, stale_reason,
                    origin_walking_to_stop_seconds, provider_metadata,
                    representative_sample_departure_at, route_itinerary
                ) values (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s::jsonb,%s,%s::jsonb
                )
                on conflict (cache_identity) where not is_stale do update set
                    representative_duration_seconds = excluded.representative_duration_seconds,
                    minimum_duration_seconds = excluded.minimum_duration_seconds,
                    maximum_duration_seconds = excluded.maximum_duration_seconds,
                    distance_meters = excluded.distance_meters,
                    walking_duration_seconds = excluded.walking_duration_seconds,
                    transfer_count = excluded.transfer_count,
                    nearest_stop_id = excluded.nearest_stop_id,
                    stop_to_destination_seconds = excluded.stop_to_destination_seconds,
                    origin_walking_to_stop_seconds = excluded.origin_walking_to_stop_seconds,
                    confidence = excluded.confidence, sample_count = excluded.sample_count,
                    calculated_at = excluded.calculated_at, expires_at = excluded.expires_at,
                    provider_metadata = excluded.provider_metadata,
                    representative_sample_departure_at = excluded.representative_sample_departure_at,
                    route_itinerary = excluded.route_itinerary,
                    result_type = excluded.result_type,
                    is_stale = excluded.is_stale,
                    stale_reason = excluded.stale_reason,
                    updated_at = now()
                returning id
                """,
                values,
            ).fetchone()
        return replace(profile, profile_id=row[0])

    def save_profile(self, profile: AccessibilityProfile) -> AccessibilityProfile:
        with self.connect() as connection:
            stored = self._save_profile(connection, profile)
        return stored

    @staticmethod
    def _save_samples(
        connection: Any,
        profile_id: int,
        samples: list[TravelTimeSample],
        *,
        replace_existing: bool = False,
    ) -> None:
        if replace_existing:
            connection.execute(
                "delete from public.housing_accessibility_samples where profile_id = %s",
                (profile_id,),
            )
        for sample in samples:
            connection.execute(
                    """
                    insert into public.housing_accessibility_samples (
                        profile_id, departure_at, duration_seconds,
                        walking_duration_seconds, transfer_count, distance_meters,
                        stop_to_destination_seconds, status, provider_sample_id,
                        provider_metadata, route_itinerary
                    ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
                    on conflict (profile_id, departure_at) do update set
                        duration_seconds = excluded.duration_seconds,
                        walking_duration_seconds = excluded.walking_duration_seconds,
                        transfer_count = excluded.transfer_count,
                        distance_meters = excluded.distance_meters,
                        stop_to_destination_seconds = excluded.stop_to_destination_seconds,
                        status = excluded.status,
                        provider_sample_id = excluded.provider_sample_id,
                        provider_metadata = excluded.provider_metadata,
                        route_itinerary = excluded.route_itinerary
                    """,
                    (
                        profile_id,
                        sample.departure_at,
                        sample.duration_seconds,
                        sample.walking_duration_seconds,
                        sample.transfer_count,
                        sample.distance_meters,
                        sample.stop_to_destination_seconds,
                        sample.status.value,
                        sample.provider_sample_id,
                        json.dumps(_sample_metadata(sample), sort_keys=True),
                        (
                            json.dumps(sample.itinerary.to_dict(), sort_keys=True)
                            if sample.itinerary
                            else None
                        ),
                    ),
                )

    def save_samples(self, profile_id: int, samples: list[TravelTimeSample]) -> None:
        with self.connect() as connection:
            self._save_samples(connection, profile_id, samples)

    def load_samples(self, profile_id: int) -> list[TravelTimeSample]:
        with self.connect() as connection:
            rows = connection.execute(
                f"select {SAMPLE_COLUMNS} "
                "from public.housing_accessibility_samples "
                "where profile_id = %s order by departure_at",
                (profile_id,),
            ).fetchall()
        return [_sample_from_row(tuple(row)) for row in rows]

    def load_samples_for_profiles(
        self, profile_ids: list[int]
    ) -> dict[int, list[TravelTimeSample]]:
        if not profile_ids:
            return {}
        with self.connect() as connection:
            rows = connection.execute(
                f"select profile_id, {SAMPLE_COLUMNS} "
                "from public.housing_accessibility_samples "
                "where profile_id = any(%s) order by profile_id, departure_at",
                (profile_ids,),
            ).fetchall()
        output = {profile_id: [] for profile_id in profile_ids}
        for row in rows:
            output[int(row[0])].append(_sample_from_row(tuple(row[1:])))
        return output

    def save_profile_with_samples(
        self,
        profile: AccessibilityProfile,
        samples: list[TravelTimeSample],
        *,
        replace_profile_id: int | None = None,
    ) -> AccessibilityProfile:
        with self.connect() as connection, connection.transaction():
            stored = self._save_profile(
                connection,
                profile,
                replace_profile_id=replace_profile_id,
            )
            if stored.profile_id is None:
                raise RuntimeError("Stored profile has no identity")
            self._save_samples(
                connection,
                stored.profile_id,
                samples,
                replace_existing=replace_profile_id is not None,
            )
        return stored

    def save_replacement_with_samples(
        self,
        prior_profile_id: int,
        profile: AccessibilityProfile,
        samples: list[TravelTimeSample],
    ) -> AccessibilityProfile:
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                update public.housing_accessibility_profiles
                set is_stale = true, stale_at = now(),
                    stale_reason = 'worker_replaced', updated_at = now()
                where id = %s and not is_stale
                returning id
                """,
                (prior_profile_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown current accessibility profile {prior_profile_id}")
            stored = self._save_profile(connection, profile)
            if stored.profile_id is None:
                raise RuntimeError("Stored profile has no identity")
            self._save_samples(connection, stored.profile_id, samples)
        return stored

    def save_reuse_decision(self, decision: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into public.housing_accessibility_reuse_history (
                    requested_source_listing_id, requested_property_id, hotspot_id,
                    travel_mode, time_period, result_type, source_profile_id,
                    resolved_profile_id, estimation_distance_meters,
                    connector_duration_seconds, confidence, reuse_reason,
                    request_metadata
                ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    decision.get("requested_listing_id"),
                    decision.get("requested_property_id"),
                    decision["hotspot_id"],
                    decision["travel_mode"],
                    decision.get("time_period"),
                    decision["result_type"],
                    decision.get("source_profile_id"),
                    decision.get("resolved_profile_id"),
                    decision.get("estimation_distance_meters"),
                    decision.get("connector_duration_seconds"),
                    decision.get("confidence"),
                    decision["reuse_reason"],
                    json.dumps(decision.get("request_metadata") or {}),
                ),
            )

    def invalidate_stale(
        self,
        at: datetime,
        *,
        schedule_version: str | None = None,
        network_version: str | None = None,
    ) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                with invalidated as (
                    update public.housing_accessibility_profiles
                    set is_stale = true, stale_at = %s,
                        stale_reason = case when expires_at <= %s then 'expired'
                                            else 'version_changed' end,
                        updated_at = now()
                    where not is_stale and (
                        expires_at <= %s
                        or (%s::text is not null and travel_mode = 'transit'
                            and schedule_version is distinct from %s::text)
                        or (%s::text is not null
                            and network_version is distinct from %s::text)
                    ) returning 1
                ) select count(*) from invalidated
                """,
                (
                    at,
                    at,
                    at,
                    schedule_version,
                    schedule_version,
                    network_version,
                    network_version,
                ),
            ).fetchone()
        return row[0]

    def profiles_due_for_refresh(
        self, before: datetime, limit: int = 100
    ) -> list[AccessibilityProfile]:
        return self._profiles(
            "is_stale or expires_at <= %s order by expires_at nulls first, id limit %s",
            (before, limit),
        )
