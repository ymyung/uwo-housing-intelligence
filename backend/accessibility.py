"""Deterministic accessibility aggregation and reuse-resolution service."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from math import ceil, floor
from statistics import median
from typing import Protocol

from backend.accessibility_periods import PERIOD_BY_ID
from backend.accessibility_repository import AccessibilityRepository
from backend.domain import (
    AccessibilityOrigin,
    AccessibilityProfile,
    AccessibilityRequest,
    AccessibilityResultType,
    AccessibilityReuseDecision,
    DayType,
    Location,
    OriginType,
    RouteRequest,
    TimePeriod,
    TravelMode,
    TravelStatus,
    TravelTimeSample,
)
from backend.origin_zones import CoordinateGridOriginZoneResolver, OriginZoneResolver
from backend.providers import StraightLineEstimateProvider, haversine_distance_meters

WALK_CYCLE_PROFILE_TTL = timedelta(days=180)
TRANSIT_PROFILE_TTL = timedelta(days=30)


class BarrierPolicy(Protocol):
    def allows_reuse(
        self, origin: AccessibilityOrigin, candidate: AccessibilityOrigin
    ) -> bool: ...


class NoKnownBarrierPolicy:
    """Allows reuse when no barrier dataset is configured; never claims a check."""

    def allows_reuse(
        self, origin: AccessibilityOrigin, candidate: AccessibilityOrigin
    ) -> bool:
        del origin, candidate
        return True


def _median_int(values: list[int]) -> int | None:
    return floor(median(values) + 0.5) if values else None


def aggregate_transit_profile(
    *,
    origin: AccessibilityOrigin,
    hotspot_id: str,
    time_period: TimePeriod,
    samples: list[TravelTimeSample],
    provider: str,
    provider_profile: str,
    calculated_at: datetime,
    schedule_version: str,
    network_version: str | None,
    expected_sample_count: int | None = None,
) -> AccessibilityProfile:
    """Aggregate valid samples with a deterministic median and observed range."""

    valid = [
        sample
        for sample in samples
        if sample.duration_seconds is not None
        and sample.status in {TravelStatus.AVAILABLE, TravelStatus.ESTIMATED}
    ]
    durations = [sample.duration_seconds for sample in valid if sample.duration_seconds is not None]
    duration_median = median(durations) if durations else None
    representative = (
        min(
            valid,
            key=lambda sample: (
                abs((sample.duration_seconds or 0) - duration_median),
                sample.departure_at,
            ),
        )
        if duration_median is not None
        else None
    )
    expected = expected_sample_count if expected_sample_count is not None else len(samples)
    completeness = len(valid) / expected if expected > 0 else 0.0
    period = PERIOD_BY_ID[time_period]
    return AccessibilityProfile(
        profile_id=None,
        origin=origin,
        hotspot_id=hotspot_id,
        travel_mode=TravelMode.TRANSIT,
        day_type=period.day_type,
        time_period=time_period,
        representative_duration_seconds=(
            representative.duration_seconds if representative else None
        ),
        minimum_duration_seconds=min(durations) if durations else None,
        maximum_duration_seconds=max(durations) if durations else None,
        distance_meters=representative.distance_meters if representative else None,
        walking_duration_seconds=(
            representative.walking_duration_seconds if representative else None
        ),
        transfer_count=representative.transfer_count if representative else None,
        nearest_stop_id=origin.nearest_stop_id,
        stop_to_destination_seconds=(
            representative.stop_to_destination_seconds if representative else None
        ),
        provider=provider,
        provider_profile=provider_profile,
        result_type=AccessibilityResultType.EXACT_ROUTE,
        confidence=round(min(1.0, completeness * 0.95), 3),
        sample_count=len(valid),
        calculated_at=calculated_at,
        schedule_version=schedule_version,
        network_version=network_version,
        expires_at=calculated_at + TRANSIT_PROFILE_TTL,
        representative_sample_departure_at=(
            representative.departure_at if representative else None
        ),
    )


def _freshest_current(
    profiles: list[AccessibilityProfile], at: datetime
) -> AccessibilityProfile | None:
    current = [profile for profile in profiles if profile.is_current(at)]
    return max(current, key=lambda profile: (profile.calculated_at, profile.profile_id or 0), default=None)


def _freshest_stale(
    profiles: list[AccessibilityProfile], at: datetime
) -> AccessibilityProfile | None:
    stale = [profile for profile in profiles if not profile.is_current(at)]
    return max(
        stale,
        key=lambda profile: (profile.calculated_at, profile.profile_id or 0),
        default=None,
    )


class AccessibilityResolver:
    def __init__(
        self,
        repository: AccessibilityRepository,
        *,
        estimate_provider: StraightLineEstimateProvider | None = None,
        zone_resolver: OriginZoneResolver | None = None,
        barrier_policy: BarrierPolicy | None = None,
        nearby_radius_meters: int = 125,
        property_origin_tolerance_meters: int = 20,
        connector_detour_factor: float = 1.35,
        walking_speed_kmh: float = 4.8,
        cycling_speed_kmh: float = 15.0,
    ) -> None:
        if nearby_radius_meters <= 3:
            raise ValueError("nearby_radius_meters must exceed exact-origin tolerance")
        if connector_detour_factor < 1:
            raise ValueError("connector_detour_factor must be at least one")
        self.repository = repository
        self.estimate_provider = estimate_provider or StraightLineEstimateProvider(
            walking_speed_kmh=walking_speed_kmh,
            cycling_speed_kmh=cycling_speed_kmh,
        )
        self.zone_resolver = zone_resolver or CoordinateGridOriginZoneResolver()
        self.barrier_policy = barrier_policy or NoKnownBarrierPolicy()
        self.nearby_radius_meters = nearby_radius_meters
        self.property_origin_tolerance_meters = property_origin_tolerance_meters
        self.connector_detour_factor = connector_detour_factor
        self.speeds = {
            TravelMode.WALKING: walking_speed_kmh,
            TravelMode.CYCLING: cycling_speed_kmh,
        }

    def _audit(
        self,
        request: AccessibilityRequest,
        decision: AccessibilityReuseDecision,
        *,
        listing_id: str | None,
        connector_seconds: int | None = None,
    ) -> None:
        self.repository.save_reuse_decision(
            {
                "requested_listing_id": listing_id,
                "requested_property_id": request.origin.property_id,
                "hotspot_id": request.hotspot.id,
                "travel_mode": request.travel_mode.value,
                "time_period": request.time_period.value if request.time_period else None,
                "result_type": decision.result_type.value,
                "source_profile_id": decision.result.source_profile_id,
                "resolved_profile_id": decision.result.profile_id,
                "estimation_distance_meters": decision.result.estimation_distance_meters,
                "connector_duration_seconds": connector_seconds,
                "confidence": decision.result.confidence,
                "reuse_reason": decision.reuse_reason,
                "request_metadata": {
                    "provider": request.provider,
                    "provider_profile": request.provider_profile,
                    "schedule_version": request.schedule_version,
                    "network_version": request.network_version,
                },
            }
        )

    def _decision(
        self,
        profile: AccessibilityProfile,
        result_type: AccessibilityResultType,
        reason: str,
        *,
        estimate: bool,
    ) -> AccessibilityReuseDecision:
        return AccessibilityReuseDecision(
            result=profile,
            result_type=result_type,
            reuse_reason=reason,
            is_estimate=estimate,
            freshness="current" if not profile.is_stale else "stale",
        )

    def resolve(
        self,
        request: AccessibilityRequest,
        *,
        listing_id: str | None = None,
        record_history: bool = True,
    ) -> AccessibilityReuseDecision:
        if request.travel_mode is TravelMode.TRANSIT and request.time_period is None:
            raise ValueError("time_period is required for transit accessibility")

        origin = request.origin
        if origin.coordinates and origin.origin_zone_id is None:
            origin = replace(
                origin, origin_zone_id=self.zone_resolver.zone_for(origin.coordinates)
            )
            request = replace(request, origin=origin)

        property_candidates = self.repository.find_exact_property(request)
        if origin.coordinates:
            property_candidates = [
                profile
                for profile in property_candidates
                if profile.origin.coordinates
                and haversine_distance_meters(
                    origin.coordinates, profile.origin.coordinates
                )
                <= self.property_origin_tolerance_meters
            ]
        exact_property = _freshest_current(property_candidates, request.requested_at)
        if exact_property:
            decision = self._decision(
                exact_property,
                AccessibilityResultType.CACHED_EXACT_PROPERTY,
                "Current profile matched the persistent property identity.",
                estimate=False,
            )
            if record_history:
                self._audit(request, decision, listing_id=listing_id)
            return decision

        exact_origin_candidates = self.repository.find_exact_origin(request)
        exact_origin = _freshest_current(exact_origin_candidates, request.requested_at)
        if exact_origin:
            decision = self._decision(
                exact_origin,
                AccessibilityResultType.CACHED_EXACT_ORIGIN,
                "Current profile matched the same entrance or strict coordinate origin.",
                estimate=False,
            )
            if record_history:
                self._audit(request, decision, listing_id=listing_id)
            return decision

        if request.travel_mode is TravelMode.TRANSIT:
            same_stop = _freshest_current(
                self.repository.find_same_stop_profiles(request), request.requested_at
            )
            if same_stop and origin.walking_to_stop_seconds is not None:
                total = origin.walking_to_stop_seconds + (
                    same_stop.stop_to_destination_seconds or 0
                )
                previous_walk = (
                    same_stop.origin.walking_to_stop_seconds
                    if same_stop.origin.walking_to_stop_seconds is not None
                    else same_stop.walking_duration_seconds or 0
                )
                adjustment = origin.walking_to_stop_seconds - previous_walk
                reused = replace(
                    same_stop,
                    profile_id=None,
                    origin=origin,
                    representative_duration_seconds=total,
                    minimum_duration_seconds=(
                        max(0, same_stop.minimum_duration_seconds + adjustment)
                        if same_stop.minimum_duration_seconds is not None
                        else None
                    ),
                    maximum_duration_seconds=(
                        max(0, same_stop.maximum_duration_seconds + adjustment)
                        if same_stop.maximum_duration_seconds is not None
                        else None
                    ),
                    walking_duration_seconds=origin.walking_to_stop_seconds,
                    result_type=AccessibilityResultType.SAME_STOP_REUSE,
                    confidence=round((same_stop.confidence or 0.7) * 0.9, 3),
                    source_profile_id=same_stop.profile_id,
                    estimation_method="same_stop_plus_origin_walk",
                )
                decision = self._decision(
                    reused,
                    AccessibilityResultType.SAME_STOP_REUSE,
                    "Reused a compatible stop-to-destination profile and "
                    "replaced only the walking connection to the same stop.",
                    estimate=True,
                )
                if record_history:
                    self._audit(request, decision, listing_id=listing_id)
                return decision

        if request.travel_mode in {TravelMode.WALKING, TravelMode.CYCLING} and origin.coordinates:
            candidates = [
                profile
                for profile in self.repository.find_nearby_profiles(
                    request, self.nearby_radius_meters
                )
                if profile.is_current(request.requested_at)
                and profile.origin.coordinates
                and self.barrier_policy.allows_reuse(origin, profile.origin)
            ]
            if candidates:
                source = min(
                    candidates,
                    key=lambda profile: haversine_distance_meters(
                        origin.coordinates, profile.origin.coordinates
                    ),
                )
                connector_distance = haversine_distance_meters(
                    origin.coordinates, source.origin.coordinates
                )
                if connector_distance > 3:
                    adjusted_distance = ceil(
                        connector_distance * self.connector_detour_factor
                    )
                    speed_mps = self.speeds[request.travel_mode] * 1000 / 3600
                    connector_seconds = ceil(adjusted_distance / speed_mps)
                    confidence_factor = 0.85 * (
                        1 - 0.25 * connector_distance / self.nearby_radius_meters
                    )
                    nearby = replace(
                        source,
                        profile_id=None,
                        origin=origin,
                        representative_duration_seconds=(
                            source.representative_duration_seconds + connector_seconds
                            if source.representative_duration_seconds is not None
                            else None
                        ),
                        minimum_duration_seconds=(
                            source.minimum_duration_seconds + connector_seconds
                            if source.minimum_duration_seconds is not None
                            else None
                        ),
                        maximum_duration_seconds=(
                            source.maximum_duration_seconds + connector_seconds
                            if source.maximum_duration_seconds is not None
                            else None
                        ),
                        distance_meters=(
                            source.distance_meters + adjusted_distance
                            if source.distance_meters is not None
                            else None
                        ),
                        result_type=AccessibilityResultType.NEARBY_ORIGIN_ESTIMATE,
                        confidence=round((source.confidence or 0.7) * confidence_factor, 3),
                        source_profile_id=source.profile_id,
                        estimation_distance_meters=connector_distance,
                        estimation_method=(
                            f"straight_line_connector_x{self.connector_detour_factor:g}_"
                            f"at_{self.speeds[request.travel_mode]:g}_kmh"
                        ),
                    )
                    decision = self._decision(
                        nearby,
                        AccessibilityResultType.NEARBY_ORIGIN_ESTIMATE,
                        "Estimated conservatively from a current nearby-origin profile plus a speed-based connector.",
                        estimate=True,
                    )
                    if record_history:
                        self._audit(
                            request,
                            decision,
                            listing_id=listing_id,
                            connector_seconds=connector_seconds,
                        )
                    return decision

        stale_exact = _freshest_stale(
            property_candidates + exact_origin_candidates,
            request.requested_at,
        )
        if stale_exact:
            stale = replace(
                stale_exact,
                result_type=AccessibilityResultType.STALE,
                is_stale=True,
                stale_reason=stale_exact.stale_reason or "expired",
            )
            decision = self._decision(
                stale,
                AccessibilityResultType.STALE,
                "An exact profile exists, but it is expired or stale and must not be treated as current.",
                estimate=False,
            )
            if record_history:
                self._audit(request, decision, listing_id=listing_id)
            return decision

        decision = self._fallback(request)
        if record_history:
            self._audit(request, decision, listing_id=listing_id)
        return decision

    def _fallback(self, request: AccessibilityRequest) -> AccessibilityReuseDecision:
        origin = request.origin
        destination = request.hotspot.coordinates
        if (
            request.travel_mode in {TravelMode.WALKING, TravelMode.CYCLING}
            and origin.coordinates
            and destination
        ):
            route = self.estimate_provider.get_route(
                RouteRequest(origin.coordinates, destination, request.travel_mode)
            )
            profile = AccessibilityProfile(
                profile_id=None,
                origin=origin,
                hotspot_id=request.hotspot.id,
                travel_mode=request.travel_mode,
                day_type=None,
                time_period=None,
                representative_duration_seconds=route.duration_seconds,
                minimum_duration_seconds=None,
                maximum_duration_seconds=None,
                distance_meters=route.distance_meters,
                walking_duration_seconds=(
                    route.duration_seconds
                    if request.travel_mode is TravelMode.WALKING
                    else None
                ),
                transfer_count=None,
                nearest_stop_id=None,
                stop_to_destination_seconds=None,
                provider=route.metadata.provider,
                provider_profile="v1",
                result_type=AccessibilityResultType.STRAIGHT_LINE_FALLBACK,
                confidence=route.confidence,
                sample_count=0,
                calculated_at=request.requested_at,
                network_version=None,
                expires_at=request.requested_at + WALK_CYCLE_PROFILE_TTL,
                estimation_method="haversine_speed_estimate",
            )
            return self._decision(
                profile,
                AccessibilityResultType.STRAIGHT_LINE_FALLBACK,
                "No compatible profile was available; used a straight-line speed estimate.",
                estimate=True,
            )

        result_type = (
            AccessibilityResultType.PENDING_PROVIDER
            if origin.coordinates and destination
            else AccessibilityResultType.UNAVAILABLE
        )
        profile = AccessibilityProfile(
            profile_id=None,
            origin=origin,
            hotspot_id=request.hotspot.id,
            travel_mode=request.travel_mode,
            day_type=(
                PERIOD_BY_ID[request.time_period].day_type
                if request.time_period
                else None
            ),
            time_period=request.time_period,
            representative_duration_seconds=None,
            minimum_duration_seconds=None,
            maximum_duration_seconds=None,
            distance_meters=None,
            walking_duration_seconds=None,
            transfer_count=None,
            nearest_stop_id=origin.nearest_stop_id,
            stop_to_destination_seconds=None,
            provider=request.provider,
            provider_profile=request.provider_profile,
            result_type=result_type,
            confidence=None,
            sample_count=0,
            calculated_at=request.requested_at,
            schedule_version=request.schedule_version,
            network_version=request.network_version,
        )
        return self._decision(
            profile,
            result_type,
            (
                "No safe transit profile exists; a live provider is required."
                if request.travel_mode is TravelMode.TRANSIT
                else "Origin or destination coordinates are unavailable."
            ),
            estimate=False,
        )
