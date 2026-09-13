"""Deterministic quality checks for normalized accessibility results."""

from __future__ import annotations

from dataclasses import dataclass

from backend.domain import AccessibilityProfile, Coordinates, RouteResult, TravelMode
from backend.providers import haversine_distance_meters


@dataclass(frozen=True)
class QualityAssessment:
    quality_status: str
    decision: str
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "quality_status": self.quality_status,
            "decision": self.decision,
            "reason_codes": list(self.reason_codes),
        }


def _decision(reasons: set[str], *, failed: bool = False) -> str:
    if failed:
        return "failed"
    review = {
        "duration_outlier",
        "distance_outlier",
        "impossible_speed",
        "origin_snap_too_far",
        "destination_snap_too_far",
        "insufficient_samples",
        "unexpected_no_route",
        "version_mismatch",
    }
    if reasons & review:
        return "manual_review_required"
    return "accepted_with_warning" if reasons else "accepted"


def assess_direct_route(route: RouteResult) -> QualityAssessment:
    reasons: set[str] = set()
    duration = route.duration_seconds
    distance = route.distance_meters
    if duration is None or duration <= 0 or duration > 4 * 60 * 60:
        reasons.add("duration_outlier")
    if distance is None or distance <= 0 or distance > 100_000:
        reasons.add("distance_outlier")
    if duration and distance:
        speed_mps = distance / duration
        limits = {
            TravelMode.WALKING: (0.4, 2.7),
            TravelMode.CYCLING: (1.5, 13.0),
        }.get(route.mode)
        if limits and not limits[0] <= speed_mps <= limits[1]:
            reasons.add("impossible_speed")
        straight = haversine_distance_meters(route.origin, route.destination)
        if straight > 0 and distance / straight > 3.5:
            reasons.add("excessive_detour")
    if (
        route.origin_snap_distance_meters is not None
        and route.origin_snap_distance_meters > 200
    ):
        reasons.add("origin_snap_too_far")
    if (
        route.destination_snap_distance_meters is not None
        and route.destination_snap_distance_meters > 200
    ):
        reasons.add("destination_snap_too_far")
    expected = "walk" if route.mode is TravelMode.WALKING else "bicycle"
    if route.provider_mode and route.provider_mode.lower() != expected:
        reasons.add("provider_mode_mismatch")
    return QualityAssessment(
        "complete" if duration and distance else "no_route",
        _decision(reasons, failed=not duration or not distance),
        tuple(sorted(reasons)),
    )


def assess_transit_profile(
    profile: AccessibilityProfile,
    *,
    minimum_valid_samples: int,
    deterministic_alternative: bool = False,
) -> QualityAssessment:
    reasons: set[str] = set()
    if profile.sample_count < minimum_valid_samples and not deterministic_alternative:
        reasons.add("insufficient_samples")
    if profile.representative_duration_seconds is None:
        if not deterministic_alternative:
            reasons.add("unexpected_no_route")
    elif not 60 <= profile.representative_duration_seconds <= 6 * 60 * 60:
        reasons.add("duration_outlier")
    if profile.distance_meters is not None and not 0 < profile.distance_meters <= 150_000:
        reasons.add("distance_outlier")
    if profile.transfer_count is not None and profile.transfer_count > 3:
        reasons.add("transfer_count_outlier")
    if (
        profile.walking_duration_seconds is not None
        and profile.representative_duration_seconds
        and profile.walking_duration_seconds / profile.representative_duration_seconds > 0.7
    ):
        reasons.add("high_walking_share")
    if profile.sample_count == 0:
        status = "no_route"
    elif profile.sample_count < minimum_valid_samples:
        status = "insufficient_samples"
    elif profile.sample_count < profile.requested_sample_count:
        status = "partial"
    else:
        status = "complete"
    return QualityAssessment(status, _decision(reasons), tuple(sorted(reasons)))


def assess_cached_profile(profile: AccessibilityProfile) -> QualityAssessment:
    """Rehydrate a current profile's stored quality decision without rerouting."""

    reasons = set(profile.quality_reason_codes)
    return QualityAssessment(
        profile.quality_status or "complete",
        _decision(reasons),
        tuple(sorted(reasons)),
    )


def merge_routing_reason_codes(
    assessment: QualityAssessment,
    reason_codes: set[str],
) -> QualityAssessment:
    """Add normalized provider reasons without masking a known no-route cause."""

    reasons = set(assessment.reason_codes)
    if reason_codes:
        reasons.discard("unexpected_no_route")
        reasons.update(reason_codes)
    decision = assessment.decision
    if reason_codes and decision == "accepted":
        decision = "accepted_with_warning"
    return QualityAssessment(
        assessment.quality_status,
        decision,
        tuple(sorted(reasons)),
    )


def coordinate_snap_distance(
    requested: Coordinates, returned: Coordinates | None
) -> int | None:
    return haversine_distance_meters(requested, returned) if returned else None
