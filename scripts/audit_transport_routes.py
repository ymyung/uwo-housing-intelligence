"""Audit persisted normalized route geometry without contacting a provider."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.accessibility_inputs import (
    load_routing_bundle,
    load_verified_hotspots,
    load_worker_config,
)
from backend.accessibility_repository import PostgresAccessibilityRepository
from backend.domain import AccessibilityProfile, Coordinates, TravelMode, TravelTimeSample
from backend.providers import haversine_distance_meters
from backend.route_geometry import decode_polyline, validate_itinerary_geometry
from pipeline.run_context import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "accessibility-worker.toml"


def _ids(value: str) -> list[int]:
    try:
        output = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    except ValueError as exc:
        raise argparse.ArgumentTypeError("property IDs must be comma-separated integers") from exc
    if not output:
        raise argparse.ArgumentTypeError("at least one property ID is required")
    return output


def _representative_sample(
    profile: AccessibilityProfile,
    samples: list[TravelTimeSample],
) -> TravelTimeSample | None:
    departure = profile.representative_sample_departure_at
    if departure is None:
        return None
    return next((sample for sample in samples if sample.departure_at == departure), None)


def _audit_itinerary(
    profile: AccessibilityProfile,
    sample: TravelTimeSample | None,
    destination: Coordinates,
) -> dict[str, Any]:
    itinerary = sample.itinerary if sample else profile.route_itinerary
    if itinerary is None:
        raise ValueError("representative route itinerary is missing")
    measures = validate_itinerary_geometry(
        itinerary,
        origin=profile.origin.coordinates,
        destination=destination,
    )
    expected_transfers = max(0, len(itinerary.transit_legs) - 1)
    if itinerary.transfer_count not in {None, expected_transfers}:
        raise ValueError("itinerary transfer count differs from transit legs")
    if profile.transfer_count not in {None, expected_transfers}:
        raise ValueError("profile transfer count differs from representative itinerary")
    if sample:
        comparable = (
            ("duration_seconds", profile.representative_duration_seconds),
            ("walking_duration_seconds", profile.walking_duration_seconds),
            ("transfer_count", profile.transfer_count),
            ("distance_meters", profile.distance_meters),
        )
        for field, expected in comparable:
            if getattr(sample, field) != expected:
                raise ValueError(f"representative sample {field} differs from profile")
    stop_offsets: list[float] = []
    continuity_gaps: list[float] = []
    prior_end: Coordinates | None = None
    for leg in itinerary.legs:
        points = decode_polyline(leg.encoded_polyline or "")
        if prior_end:
            continuity_gaps.append(haversine_distance_meters(prior_end, points[0]))
        prior_end = points[-1]
        if leg.from_stop and leg.from_stop.coordinates:
            stop_offsets.append(
                haversine_distance_meters(leg.from_stop.coordinates, points[0])
            )
        if leg.to_stop and leg.to_stop.coordinates:
            stop_offsets.append(
                haversine_distance_meters(leg.to_stop.coordinates, points[-1])
            )
    if stop_offsets and max(stop_offsets) > 500:
        raise ValueError("transit geometry endpoint is too far from its persisted stop")
    if continuity_gaps and max(continuity_gaps) > 500:
        raise ValueError("route legs are not spatially continuous")
    return {
        **measures,
        "maximum_stop_offset_meters": round(max(stop_offsets), 1) if stop_offsets else None,
        "maximum_leg_gap_meters": round(max(continuity_gaps), 1) if continuity_gaps else 0.0,
        "transit_leg_count": len(itinerary.transit_legs),
        "route_labels": [
            leg.route_short_name or leg.route_long_name
            for leg in itinerary.transit_legs
            if leg.route_short_name or leg.route_long_name
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--property-ids", type=_ids, required=True)
    parser.add_argument("--hotspot", default="western-main-campus")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_worker_config(args.config.resolve(), PROJECT_ROOT)
    bundle = load_routing_bundle(config)
    hotspot = load_verified_hotspots(
        config.hotspot_config_path,
        bounds=config.bounds,
        selected_ids={args.hotspot},
        limit=1,
    )[0]
    database_url = os.getenv(config.database_url_env, "").strip()
    if not database_url:
        raise SystemExit(f"{config.database_url_env} is required")
    repository = PostgresAccessibilityRepository(database_url)
    profiles = repository.find_current_property_profiles(
        args.property_ids,
        args.hotspot,
        at=datetime.now(timezone.utc),
        provider=config.routing_provider,
        provider_profile=config.provider_profile,
        schedule_version=bundle.metadata.schedule_version,
        network_version=bundle.metadata.network_version,
    )
    profile_ids = [profile.profile_id for profile in profiles if profile.profile_id]
    samples_by_profile = repository.load_samples_for_profiles(profile_ids)
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    intentional_absence = 0
    for profile in profiles:
        samples = samples_by_profile.get(profile.profile_id or -1, [])
        representative = _representative_sample(profile, samples)
        if profile.travel_mode is TravelMode.TRANSIT and representative is None:
            intentional_absence += 1
            rows.append(
                {
                    "property_id": profile.origin.property_id,
                    "mode": profile.travel_mode.value,
                    "time_period": profile.time_period.value if profile.time_period else None,
                    "geometry_status": "intentionally_absent",
                    "reason_codes": list(profile.quality_reason_codes),
                }
            )
            continue
        try:
            measures = _audit_itinerary(
                profile,
                representative,
                hotspot.hotspot.coordinates,
            )
            rows.append(
                {
                    "property_id": profile.origin.property_id,
                    "mode": profile.travel_mode.value,
                    "time_period": profile.time_period.value if profile.time_period else None,
                    "geometry_status": "valid",
                    "representative_departure_at": (
                        representative.departure_at.isoformat() if representative else None
                    ),
                    **measures,
                }
            )
        except ValueError as exc:
            failures.append(
                {
                    "property_id": profile.origin.property_id,
                    "mode": profile.travel_mode.value,
                    "time_period": profile.time_period.value if profile.time_period else None,
                    "error": str(exc),
                }
            )
    expected_profiles = len(args.property_ids) * 8
    report = {
        "schema_version": 1,
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "property_count": len(args.property_ids),
        "expected_profile_count": expected_profiles,
        "profile_count": len(profiles),
        "valid_geometry_count": sum(row["geometry_status"] == "valid" for row in rows),
        "intentional_geometry_absence_count": intentional_absence,
        "failure_count": len(failures),
        "routing": {
            "router_version": bundle.metadata.router_version,
            "network_version": bundle.metadata.network_version,
            "schedule_version": bundle.metadata.schedule_version,
            "provider_profile": config.provider_profile,
        },
        "rows": rows,
        "failures": failures,
    }
    if args.output:
        atomic_write_json(args.output.resolve(), report)
    console_report = (
        {key: value for key, value in report.items() if key not in {"rows", "failures"}}
        | {"failure_count": len(failures)}
        if args.summary_only
        else report
    )
    print(json.dumps(console_report, sort_keys=True))
    return 1 if failures or len(profiles) != expected_profiles else 0


if __name__ == "__main__":
    raise SystemExit(main())
