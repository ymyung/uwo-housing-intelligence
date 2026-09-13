"""Public Getting to Western projections over persisted accessibility data."""

from __future__ import annotations

from typing import Any

from backend.accessibility_periods import DEFAULT_TRANSIT_PERIODS
from backend.accessibility_repository import profile_cache_identity
from backend.domain import AccessibilityProfile, Hotspot, TimePeriod, TravelMode, TravelTimeSample
from backend.gtfs_freshness import GtfsFreshnessReport


PRIMARY_TRANSIT_PERIOD = TimePeriod.WEEKDAY_MORNING_COMMUTE
TECHNICAL_REASON_CODES = frozenset(
    {"provider_error", "invalid_request", "version_mismatch", "missing_route_geometry"}
)


def current_profile_index(
    profiles: list[AccessibilityProfile],
) -> dict[tuple[TravelMode, TimePeriod | None], AccessibilityProfile]:
    output: dict[tuple[TravelMode, TimePeriod | None], AccessibilityProfile] = {}
    for profile in profiles:
        key = (profile.travel_mode, profile.time_period)
        existing = output.get(key)
        if existing is None or (profile.calculated_at, profile.profile_id or 0) > (
            existing.calculated_at,
            existing.profile_id or 0,
        ):
            output[key] = profile
    return output


def _status(profile: AccessibilityProfile | None) -> str:
    if profile is None:
        return "unavailable"
    reasons = set(profile.quality_reason_codes)
    if profile.is_stale:
        return "stale"
    if reasons & TECHNICAL_REASON_CODES or profile.quality_status == "provider_error":
        return "technical_failure"
    if profile.representative_duration_seconds is None:
        if "walking_better_than_transit" in reasons:
            return "walking_better_than_transit"
        if "no_route" in reasons or profile.quality_status == "no_route":
            return "no_route"
        return "unavailable"
    if profile.quality_status in {"partial", "insufficient_samples"}:
        return "partial"
    return "available"


def _review_required(profile: AccessibilityProfile | None) -> bool:
    if profile is None:
        return False
    return profile.quality_status == "insufficient_samples" or (
        "insufficient_samples" in profile.quality_reason_codes
        and profile.representative_duration_seconds is not None
    )


def profile_summary(
    profile: AccessibilityProfile | None,
    *,
    gtfs: GtfsFreshnessReport | None = None,
) -> dict[str, Any]:
    status = _status(profile)
    if profile is None:
        return {
            "status": status,
            "duration_seconds": None,
            "duration_minutes": None,
            "distance_meters": None,
            "walking_duration_seconds": None,
            "transfer_count": None,
            "sample_count": 0,
            "requested_sample_count": 0,
            "quality_status": None,
            "reason_codes": [],
            "review_required": False,
            "high_walking_share": False,
            "geometry_available": False,
            "calculated_at": None,
            "expires_at": None,
            "freshness": "unavailable",
            "schedule_version": None,
            "network_version": None,
            "representative_sample_departure_at": None,
        }
    duration = profile.representative_duration_seconds
    transit_schedule_stale = (
        profile.travel_mode is TravelMode.TRANSIT and gtfs is not None and gtfs.expired
    )
    return {
        "status": status,
        "duration_seconds": duration,
        "duration_minutes": round(duration / 60) if duration is not None else None,
        "distance_meters": profile.distance_meters,
        "walking_duration_seconds": profile.walking_duration_seconds,
        "transfer_count": profile.transfer_count,
        "sample_count": profile.sample_count,
        "requested_sample_count": profile.requested_sample_count,
        "quality_status": profile.quality_status,
        "reason_codes": list(profile.quality_reason_codes),
        "review_required": _review_required(profile),
        "high_walking_share": "high_walking_share" in profile.quality_reason_codes,
        "geometry_available": (
            profile.route_itinerary.has_geometry
            if profile.route_itinerary is not None
            else profile.representative_sample_departure_at is not None
        ),
        "calculated_at": profile.calculated_at.isoformat(),
        "expires_at": profile.expires_at.isoformat() if profile.expires_at else None,
        "freshness": (
            "stale_schedule"
            if transit_schedule_stale
            else "stale"
            if profile.is_stale
            else "current"
        ),
        "schedule_version": profile.schedule_version,
        "network_version": profile.network_version,
        "representative_sample_departure_at": (
            profile.representative_sample_departure_at.isoformat()
            if profile.representative_sample_departure_at
            else None
        ),
    }


def schedule_summary(gtfs: GtfsFreshnessReport | None) -> dict[str, Any]:
    if gtfs is None:
        return {
            "kind": "static_schedule",
            "live": False,
            "freshness": "unknown",
        }
    return {
        "kind": "static_schedule",
        "live": False,
        "freshness": "expired" if gtfs.expired else "current",
        "feed_version": gtfs.feed_version,
        "service_start_date": gtfs.service_start_date.isoformat(),
        "service_end_date": gtfs.service_end_date.isoformat(),
        "reference_week_start": gtfs.reference_week_start.isoformat(),
        "reference_week_supported": gtfs.reference_week_supported,
    }


def compact_transportation_summary(
    profiles: list[AccessibilityProfile],
    *,
    hotspot: Hotspot,
    gtfs: GtfsFreshnessReport | None = None,
) -> dict[str, Any]:
    indexed = current_profile_index(profiles)
    walking_summary = profile_summary(
        indexed.get((TravelMode.WALKING, None)), gtfs=gtfs
    )
    cycling_summary = profile_summary(
        indexed.get((TravelMode.CYCLING, None)), gtfs=gtfs
    )
    transit_summary = profile_summary(
        indexed.get((TravelMode.TRANSIT, PRIMARY_TRANSIT_PERIOD)), gtfs=gtfs
    )
    walking = {
        key: walking_summary[key]
        for key in ("status", "duration_minutes", "distance_meters", "freshness")
    }
    cycling = {
        key: cycling_summary[key]
        for key in ("status", "duration_minutes", "distance_meters", "freshness")
    }
    transit = {
        key: transit_summary[key]
        for key in (
            "status",
            "duration_minutes",
            "walking_duration_seconds",
            "transfer_count",
            "review_required",
            "high_walking_share",
            "freshness",
        )
    }
    transit["primary_period"] = PRIMARY_TRANSIT_PERIOD.value
    schedule = schedule_summary(gtfs)
    return {
        "destination_id": hotspot.id,
        "destination_name": hotspot.name,
        "walking": walking,
        "cycling": cycling,
        "transit": transit,
        "availability": (
            "available"
            if any(value["status"] != "unavailable" for value in (walking, cycling, transit))
            else "unavailable"
        ),
        "schedule": {
            key: schedule[key] for key in ("kind", "live", "freshness")
        },
    }


def transportation_overview(
    profiles: list[AccessibilityProfile],
    *,
    hotspot: Hotspot,
    gtfs: GtfsFreshnessReport | None = None,
) -> dict[str, Any]:
    indexed = current_profile_index(profiles)
    periods = []
    for definition in DEFAULT_TRANSIT_PERIODS:
        summary = profile_summary(
            indexed.get((TravelMode.TRANSIT, definition.id)), gtfs=gtfs
        )
        periods.append(
            {
                "time_period": definition.id.value,
                "label": definition.label,
                **summary,
            }
        )
    return {
        "destination": hotspot.to_dict(),
        "walking": profile_summary(
            indexed.get((TravelMode.WALKING, None)), gtfs=gtfs
        ),
        "cycling": profile_summary(
            indexed.get((TravelMode.CYCLING, None)), gtfs=gtfs
        ),
        "transit": {
            "primary_period": PRIMARY_TRANSIT_PERIOD.value,
            "periods": periods,
        },
        "schedule": schedule_summary(gtfs),
    }


def representative_sample(
    profile: AccessibilityProfile,
    samples: list[TravelTimeSample],
) -> TravelTimeSample | None:
    if profile.representative_sample_departure_at is not None:
        for sample in samples:
            if sample.departure_at == profile.representative_sample_departure_at:
                return sample
    valid = [
        sample
        for sample in samples
        if sample.duration_seconds is not None and sample.itinerary is not None
    ]
    if not valid or profile.representative_duration_seconds is None:
        return None
    return min(
        valid,
        key=lambda sample: (
            abs((sample.duration_seconds or 0) - profile.representative_duration_seconds),
            sample.departure_at,
        ),
    )


def route_detail(
    profile: AccessibilityProfile | None,
    *,
    samples: list[TravelTimeSample] | None = None,
    gtfs: GtfsFreshnessReport | None = None,
    include_geometry: bool = True,
) -> dict[str, Any]:
    summary = profile_summary(profile, gtfs=gtfs)
    if profile is None:
        return {**summary, "routing_fingerprint": None, "itinerary": None}
    sample = (
        representative_sample(profile, samples or [])
        if profile.travel_mode is TravelMode.TRANSIT
        else None
    )
    itinerary = sample.itinerary if sample else profile.route_itinerary
    sample_details = (
        {
            "departure_at": sample.departure_at.isoformat(),
            "arrival_at": sample.arrival_at.isoformat() if sample.arrival_at else None,
            "waiting_duration_seconds": sample.waiting_duration_seconds,
            "in_vehicle_duration_seconds": sample.in_vehicle_duration_seconds,
        }
        if sample
        else None
    )
    return {
        **summary,
        "mode": profile.travel_mode.value,
        "time_period": profile.time_period.value if profile.time_period else None,
        "routing_fingerprint": profile_cache_identity(profile),
        "representative_sample": sample_details,
        "itinerary": (
            itinerary.to_dict(include_geometry=include_geometry) if itinerary else None
        ),
    }
