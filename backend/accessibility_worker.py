"""Bounded, cache-first accessibility routing worker orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from backend.accessibility import (
    TRANSIT_PROFILE_TTL,
    WALK_CYCLE_PROFILE_TTL,
    aggregate_transit_profile,
)
from backend.accessibility_inputs import ReviewedProperty, VerifiedHotspot
from backend.accessibility_periods import DEFAULT_TRANSIT_PERIODS, TransitPeriodDefinition
from backend.accessibility_quality import (
    QualityAssessment,
    assess_cached_profile,
    assess_direct_route,
    assess_transit_profile,
    merge_routing_reason_codes,
)
from backend.accessibility_repository import AccessibilityRepository
from backend.domain import (
    AccessibilityOrigin,
    AccessibilityProfile,
    AccessibilityRequest,
    AccessibilityResultType,
    Location,
    OriginType,
    RouteRequest,
    RouteResult,
    TravelMode,
    TravelStatus,
    TravelTimeSample,
)
from backend.routing_provider import (
    NoRouteError,
    RoutingGraphMetadata,
    RoutingProviderError,
    RoutingValidationError,
    TransientRoutingError,
)


class WorkerRoutingProvider(Protocol):
    provider_name: str

    def preflight(self, expected: RoutingGraphMetadata | None = None) -> RoutingGraphMetadata: ...

    def get_route(self, request: RouteRequest) -> RouteResult: ...

    def get_samples(
        self,
        origin: Location,
        destination: Location,
        departures: list[datetime],
    ) -> list[TravelTimeSample]: ...


CACHEABLE_UNAVAILABLE_ROUTING_REASONS = frozenset({"walking_better_than_transit"})
DETERMINISTIC_UNAVAILABLE_ROUTING_REASONS = frozenset(
    {"no_route", "walking_better_than_transit"}
)


def _is_available_sample(sample: TravelTimeSample) -> bool:
    return sample.duration_seconds is not None and sample.status in {
        TravelStatus.AVAILABLE,
        TravelStatus.ESTIMATED,
    }


def _is_cacheable_unavailable_sample(sample: TravelTimeSample) -> bool:
    reason_codes = {diagnostic.code for diagnostic in sample.routing_diagnostics}
    return (
        sample.duration_seconds is None
        and sample.status is TravelStatus.UNAVAILABLE
        and bool(reason_codes)
        and reason_codes <= CACHEABLE_UNAVAILABLE_ROUTING_REASONS
    )


def _is_reusable_sample(sample: TravelTimeSample) -> bool:
    return _is_available_sample(sample) or _is_cacheable_unavailable_sample(sample)


def _is_deterministic_unavailable_sample(sample: TravelTimeSample) -> bool:
    reason_codes = {diagnostic.code for diagnostic in sample.routing_diagnostics}
    return (
        sample.duration_seconds is None
        and sample.status is TravelStatus.UNAVAILABLE
        and bool(reason_codes)
        and reason_codes <= DETERMINISTIC_UNAVAILABLE_ROUTING_REASONS
    )


def _has_complete_mixed_deterministic_evidence(
    samples: list[TravelTimeSample], expected_departures: int
) -> bool:
    return (
        len(samples) == expected_departures
        and any(_is_available_sample(sample) for sample in samples)
        and all(
            _is_available_sample(sample) or _is_deterministic_unavailable_sample(sample)
            for sample in samples
        )
    )


@dataclass
class WorkerMetrics:
    cache_hits: int = 0
    sample_cache_hits: int = 0
    provider_calls: int = 0
    provider_retries: int = 0
    profiles_created: int = 0
    profiles_refreshed: int = 0
    profiles_skipped: int = 0
    profiles_failed: int = 0
    database_writes: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class WorkUnit:
    property: ReviewedProperty
    hotspot: VerifiedHotspot
    mode: TravelMode
    period: TransitPeriodDefinition | None = None

    @property
    def key(self) -> str:
        period = self.period.id.value if self.period else "all_day"
        return f"{self.property.property_id}:{self.hotspot.hotspot.id}:{self.mode.value}:{period}"


@dataclass(frozen=True)
class WorkerOutcome:
    unit_key: str
    property_id: int
    address: str
    hotspot_id: str
    hotspot_name: str
    mode: str
    time_period: str | None
    action: str
    profile: AccessibilityProfile | None
    samples: tuple[TravelTimeSample, ...]
    quality: QualityAssessment
    error_type: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_key": self.unit_key,
            "property_id": self.property_id,
            "address": self.address,
            "hotspot_id": self.hotspot_id,
            "hotspot_name": self.hotspot_name,
            "mode": self.mode,
            "time_period": self.time_period,
            "action": self.action,
            "profile": self.profile.to_dict() if self.profile else None,
            "samples": [sample.to_dict() for sample in self.samples],
            "quality": self.quality.to_dict(),
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


def build_work_units(
    properties: list[ReviewedProperty],
    hotspots: list[VerifiedHotspot],
    modes: tuple[TravelMode, ...],
) -> list[WorkUnit]:
    output: list[WorkUnit] = []
    for property_ in properties:
        for hotspot in hotspots:
            for mode in modes:
                if mode is TravelMode.TRANSIT:
                    output.extend(
                        WorkUnit(property_, hotspot, mode, period)
                        for period in DEFAULT_TRANSIT_PERIODS
                    )
                else:
                    output.append(WorkUnit(property_, hotspot, mode))
    return output


def period_departures(
    period: TransitPeriodDefinition,
    reference_service_week: datetime,
    *,
    timezone_name: str = "America/Toronto",
) -> list[datetime]:
    if reference_service_week.weekday() != 0:
        raise ValueError("reference_service_week must start on Monday")
    day_offset = {"weekday": 0, "saturday": 5, "sunday": 6}[period.day_type.value]
    service_date = (reference_service_week + timedelta(days=day_offset)).date()
    zone = ZoneInfo(timezone_name)
    return [datetime.combine(service_date, value, tzinfo=zone) for value in period.departures]


def _current(profiles: list[AccessibilityProfile], at: datetime) -> AccessibilityProfile | None:
    return max(
        (profile for profile in profiles if profile.is_current(at)),
        key=lambda profile: (profile.calculated_at, profile.profile_id or 0),
        default=None,
    )


def _latest(profiles: list[AccessibilityProfile]) -> AccessibilityProfile | None:
    return max(
        profiles,
        key=lambda profile: (profile.calculated_at, profile.profile_id or 0),
        default=None,
    )


class AccessibilityWorker:
    def __init__(
        self,
        repository: AccessibilityRepository,
        provider: WorkerRoutingProvider,
        *,
        provider_profile: str,
        graph_metadata: RoutingGraphMetadata,
        reference_service_week: datetime,
        minimum_transit_samples: int = 2,
        max_retries: int = 2,
        retry_delay_seconds: float = 0.25,
        persist: bool = True,
        now: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if reference_service_week.tzinfo is None:
            reference_service_week = reference_service_week.replace(tzinfo=timezone.utc)
        self.repository = repository
        self.provider = provider
        self.provider_profile = provider_profile
        self.graph_metadata = graph_metadata
        self.reference_service_week = reference_service_week
        self.minimum_transit_samples = minimum_transit_samples
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.persist = persist
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleeper
        self.metrics = WorkerMetrics()

    def _request(self, unit: WorkUnit, at: datetime) -> AccessibilityRequest:
        return AccessibilityRequest(
            origin=AccessibilityOrigin(
                unit.property.coordinates,
                property_id=unit.property.property_id,
                origin_type=OriginType.PROPERTY,
            ),
            hotspot=unit.hotspot.hotspot,
            travel_mode=unit.mode,
            time_period=unit.period.id if unit.period else None,
            provider=self.provider.provider_name,
            provider_profile=self.provider_profile,
            requested_at=at,
            schedule_version=(
                self.graph_metadata.schedule_version
                if unit.mode is TravelMode.TRANSIT
                else None
            ),
            network_version=self.graph_metadata.network_version,
            hotspot_fingerprint=unit.hotspot.fingerprint,
            origin_fingerprint=unit.property.fingerprint,
        )

    def _provider_call(
        self, callback: Callable[[], Any], *, logical_call_count: int = 1
    ) -> Any:
        attempts = 0
        while True:
            self.metrics.provider_calls += logical_call_count
            try:
                return callback()
            except TransientRoutingError:
                if attempts >= self.max_retries:
                    raise
                self.metrics.provider_retries += 1
                self._sleep(self.retry_delay_seconds * (2**attempts))
                attempts += 1

    def estimate(self, units: list[WorkUnit]) -> dict[str, int]:
        metrics = WorkerMetrics()
        provider_calls = 0
        sample_writes = 0
        now = self._now()
        for unit in units:
            request = self._request(unit, now)
            candidates = self.repository.find_exact_property(request)
            if _current(candidates, now):
                metrics.cache_hits += 1
                metrics.profiles_skipped += 1
                continue
            if unit.mode is TravelMode.TRANSIT and unit.period:
                departures = period_departures(
                    unit.period, self.reference_service_week
                )
                previous = _latest(candidates)
                cached = (
                    self.repository.load_samples(previous.profile_id)
                    if previous and previous.profile_id is not None
                    else []
                )
                departure_set = {
                    sample.departure_at
                    for sample in cached
                    if _is_reusable_sample(sample)
                }
                hits = sum(value in departure_set for value in departures)
                metrics.sample_cache_hits += hits
                provider_calls += len(departures) - hits
                sample_writes += len(departures)
            else:
                provider_calls += 1
            metrics.profiles_created += 1
        return {
            **metrics.to_dict(),
            "expected_provider_calls": provider_calls,
            "expected_profile_writes": metrics.profiles_created,
            "expected_sample_writes": sample_writes,
            "expected_database_writes": metrics.profiles_created if self.persist else 0,
        }

    def _persist(
        self,
        profile: AccessibilityProfile,
        samples: list[TravelTimeSample],
        previous_current: AccessibilityProfile | None,
    ) -> AccessibilityProfile:
        if not self.persist:
            return profile
        if previous_current and previous_current.profile_id is not None:
            stored = self.repository.save_replacement_with_samples(
                previous_current.profile_id, profile, samples
            )
            self.metrics.profiles_refreshed += 1
        else:
            stored = self.repository.save_profile_with_samples(profile, samples)
            self.metrics.profiles_created += 1
        self.metrics.database_writes += 1
        return stored

    @staticmethod
    def _base_outcome(
        unit: WorkUnit,
        *,
        action: str,
        profile: AccessibilityProfile | None,
        samples: list[TravelTimeSample],
        quality: QualityAssessment,
        error: Exception | None = None,
    ) -> WorkerOutcome:
        return WorkerOutcome(
            unit_key=unit.key,
            property_id=unit.property.property_id,
            address=unit.property.normalized_address,
            hotspot_id=unit.hotspot.hotspot.id,
            hotspot_name=unit.hotspot.hotspot.name,
            mode=unit.mode.value,
            time_period=unit.period.id.value if unit.period else None,
            action=action,
            profile=profile,
            samples=tuple(samples),
            quality=quality,
            error_type=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
        )

    def process(self, unit: WorkUnit, *, worker_run_id: str) -> WorkerOutcome:
        now = self._now()
        request = self._request(unit, now)
        candidates = self.repository.find_exact_property(request)
        current = _current(candidates, now)
        if current:
            self.metrics.cache_hits += 1
            self.metrics.profiles_skipped += 1
            quality = assess_cached_profile(current)
            return self._base_outcome(
                unit,
                action="cache_hit",
                profile=current,
                samples=[],
                quality=quality,
            )
        try:
            if unit.mode is TravelMode.TRANSIT:
                return self._process_transit(
                    unit, request, candidates, now, worker_run_id
                )
            return self._process_direct(unit, request, candidates, now, worker_run_id)
        except (NoRouteError, RoutingValidationError, TransientRoutingError) as exc:
            self.metrics.profiles_failed += 1
            diagnostic_codes = tuple(
                diagnostic.code for diagnostic in getattr(exc, "diagnostics", ())
            )
            reason_codes = diagnostic_codes or (
                (
                    "unexpected_no_route"
                    if isinstance(exc, NoRouteError)
                    else "version_mismatch"
                    if "version" in str(exc).lower()
                    else "provider_error"
                ),
            )
            status = "no_route" if isinstance(exc, NoRouteError) else "provider_error"
            quality = QualityAssessment(status, "failed", reason_codes)
            return self._base_outcome(
                unit,
                action="failed",
                profile=None,
                samples=[],
                quality=quality,
                error=exc,
            )

    def _process_direct(
        self,
        unit: WorkUnit,
        request: AccessibilityRequest,
        candidates: list[AccessibilityProfile],
        now: datetime,
        worker_run_id: str,
    ) -> WorkerOutcome:
        destination = unit.hotspot.hotspot.coordinates
        if destination is None:
            raise RoutingValidationError("Verified hotspot is missing coordinates")
        route = self._provider_call(
            lambda: self.provider.get_route(
                RouteRequest(unit.property.coordinates, destination, unit.mode)
            )
        )
        if route.status is not TravelStatus.AVAILABLE:
            raise NoRouteError("Direct route is unavailable")
        quality = assess_direct_route(route)
        profile = AccessibilityProfile(
            profile_id=None,
            origin=request.origin,
            hotspot_id=request.hotspot.id,
            travel_mode=unit.mode,
            day_type=None,
            time_period=None,
            representative_duration_seconds=route.duration_seconds,
            minimum_duration_seconds=route.duration_seconds,
            maximum_duration_seconds=route.duration_seconds,
            distance_meters=route.distance_meters,
            walking_duration_seconds=(
                route.duration_seconds if unit.mode is TravelMode.WALKING else None
            ),
            transfer_count=None,
            nearest_stop_id=None,
            stop_to_destination_seconds=None,
            provider=self.provider.provider_name,
            provider_profile=self.provider_profile,
            result_type=AccessibilityResultType.EXACT_ROUTE,
            confidence=route.confidence or 1.0,
            sample_count=1,
            requested_sample_count=1,
            calculated_at=now,
            network_version=self.graph_metadata.network_version,
            expires_at=now + WALK_CYCLE_PROFILE_TTL,
            hotspot_fingerprint=request.hotspot_fingerprint,
            origin_fingerprint=request.origin_fingerprint,
            quality_status=quality.quality_status,
            quality_reason_codes=quality.reason_codes,
            worker_run_id=worker_run_id,
            provider_metadata={
                "router_version": self.graph_metadata.router_version,
                "graph_built_at": self.graph_metadata.graph_built_at.isoformat(),
                "route_status": route.status.value,
                "provider_mode": route.provider_mode,
                "origin_snap_distance_meters": route.origin_snap_distance_meters,
                "destination_snap_distance_meters": route.destination_snap_distance_meters,
            },
            route_itinerary=route.itinerary,
        )
        prior = _latest(candidates)
        stored = self._persist(
            profile, [], prior if prior and not prior.is_stale else None
        )
        return self._base_outcome(
            unit,
            action="refreshed" if prior and not prior.is_stale else "created",
            profile=stored,
            samples=[],
            quality=quality,
        )

    def _process_transit(
        self,
        unit: WorkUnit,
        request: AccessibilityRequest,
        candidates: list[AccessibilityProfile],
        now: datetime,
        worker_run_id: str,
    ) -> WorkerOutcome:
        if unit.period is None or unit.hotspot.hotspot.coordinates is None:
            raise RoutingValidationError("Transit work unit is incomplete")
        departures = period_departures(unit.period, self.reference_service_week)
        previous = _latest(candidates)
        cached = (
            self.repository.load_samples(previous.profile_id)
            if previous and previous.profile_id is not None
            else []
        )
        cached_by_departure = {
            sample.departure_at: sample
            for sample in cached
            if sample.departure_at in departures
            and _is_reusable_sample(sample)
        }
        self.metrics.sample_cache_hits += len(cached_by_departure)
        missing = [value for value in departures if value not in cached_by_departure]
        fresh: list[TravelTimeSample] = []
        if missing:
            origin = Location(
                str(unit.property.property_id),
                unit.property.normalized_address,
                unit.property.coordinates,
                unit.property.normalized_address,
            )
            fresh = self._provider_call(
                lambda: self.provider.get_samples(
                    origin, unit.hotspot.hotspot, missing
                ),
                logical_call_count=len(missing),
            )
        returned = {sample.departure_at: sample for sample in fresh}
        samples = [
            replace(
                cached_by_departure.get(departure) or returned[departure],
                provider=self.provider.provider_name,
                schedule_version=self.graph_metadata.schedule_version,
                network_version=self.graph_metadata.network_version,
                worker_run_id=worker_run_id,
            )
            for departure in departures
            if departure in cached_by_departure or departure in returned
        ]
        profile = aggregate_transit_profile(
            origin=request.origin,
            hotspot_id=request.hotspot.id,
            time_period=unit.period.id,
            samples=samples,
            provider=self.provider.provider_name,
            provider_profile=self.provider_profile,
            calculated_at=now,
            schedule_version=self.graph_metadata.schedule_version,
            network_version=self.graph_metadata.network_version,
            expected_sample_count=len(departures),
        )
        profile = replace(
            profile,
            requested_sample_count=len(departures),
            hotspot_fingerprint=request.hotspot_fingerprint,
            origin_fingerprint=request.origin_fingerprint,
            worker_run_id=worker_run_id,
            provider_metadata={
                "router_version": self.graph_metadata.router_version,
                "graph_built_at": self.graph_metadata.graph_built_at.isoformat(),
            },
        )
        deterministic_alternative = (
            len(samples) == len(departures)
            and all(_is_cacheable_unavailable_sample(sample) for sample in samples)
        )
        mixed_deterministic_evidence = _has_complete_mixed_deterministic_evidence(
            samples, len(departures)
        )
        quality = assess_transit_profile(
            profile,
            minimum_valid_samples=self.minimum_transit_samples,
            deterministic_alternative=deterministic_alternative,
        )
        routing_reason_codes = {
            diagnostic.code
            for sample in samples
            for diagnostic in sample.routing_diagnostics
        }
        quality = merge_routing_reason_codes(quality, routing_reason_codes)
        origin_stop_ids = sorted(
            sample.origin_stop_id for sample in samples if sample.origin_stop_id
        )
        current_eligible = (
            profile.sample_count >= self.minimum_transit_samples
            or deterministic_alternative
            or mixed_deterministic_evidence
        )
        profile = replace(
            profile,
            quality_status=quality.quality_status,
            quality_reason_codes=quality.reason_codes,
            provider_metadata={
                **(profile.provider_metadata or {}),
                "routing_reason_codes": sorted(routing_reason_codes),
            },
            nearest_stop_id=origin_stop_ids[0] if origin_stop_ids else None,
            is_stale=not current_eligible,
            stale_reason=None if current_eligible else "insufficient_samples",
            expires_at=(now + TRANSIT_PROFILE_TTL) if current_eligible else now,
        )
        prior = _latest(candidates)
        stored = self._persist(
            profile, samples, prior if prior and not prior.is_stale else None
        )
        action = (
            "refreshed"
            if current_eligible and prior and not prior.is_stale
            else "created"
            if current_eligible
            else "partial_saved"
        )
        return self._base_outcome(
            unit,
            action=action,
            profile=stored,
            samples=samples,
            quality=quality,
        )
