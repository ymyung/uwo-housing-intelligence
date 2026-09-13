"""Run bounded, local-only Mobility Context V1 validation.

The command reads canonical property origins, existing exact OTP bicycle
profiles, and current City reference tables.  An optional bounded subset may
be re-routed against the local OTP instance.  Results are diagnostic artifacts,
never product or ranking state.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.accessibility_inputs import load_routing_bundle, load_worker_config
from backend.domain import Coordinates, RouteItinerary, RouteRequest, TravelMode
from backend.hotspots import DEFAULT_HOTSPOTS, find_hotspot
from backend.mobility_context import (
    ALGORITHM_VERSION,
    DEFAULT_TOLERANCE_METERS,
    EXCLUDED_CITY_DATASETS,
    INCLUDED_CITY_DATASETS,
    VALIDATION_TOLERANCES_METERS,
    MobilityContextValidationError,
    dependency_fingerprint,
    validate_exact_bicycle_route,
)
from backend.mobility_context_store import (
    PostgresMobilityContextAnalyzer,
    current_city_versions,
    database_url_from_environment,
)
from backend.routing_provider import (
    NoRouteError,
    OpenTripPlannerProvider,
    RoutingProviderError,
)


@dataclass(frozen=True)
class Candidate:
    property_id: int
    origin: Coordinates
    profile_id: int | None
    duration_seconds: int | None
    distance_meters: int | None
    calculated_at: datetime | None
    itinerary: RouteItinerary | None


def _candidate_rows(
    connection: Any,
    *,
    provider: str,
    provider_profile: str,
    network_version: str,
    destination_id: str,
) -> list[Candidate]:
    rows = connection.execute(
        """select p.id,p.latitude,p.longitude,
             profile.id,profile.representative_duration_seconds,
             profile.distance_meters,profile.calculated_at,profile.route_itinerary
           from public.housing_properties p
           join public.housing_property_location_visibility visibility
             on visibility.property_id=p.id and visibility.is_current
           left join lateral (
             select value.id,value.representative_duration_seconds,
               value.distance_meters,value.calculated_at,value.route_itinerary
             from public.housing_accessibility_profiles value
             where value.origin_property_id=p.id
               and value.hotspot_id=%s and value.travel_mode='cycling'
               and value.provider=%s and value.provider_profile=%s
               and value.network_version=%s and not value.is_stale
               and (value.expires_at is null or value.expires_at > now())
             order by value.calculated_at desc,value.id desc limit 1
           ) profile on true
           where visibility.location_status='available'
             and visibility.route_available
             and p.latitude is not null and p.longitude is not null
           order by p.id""",
        (destination_id, provider, provider_profile, network_version),
    ).fetchall()
    output: list[Candidate] = []
    for row in rows:
        itinerary = row[7]
        output.append(
            Candidate(
                property_id=int(row[0]),
                origin=Coordinates(float(row[1]), float(row[2])),
                profile_id=int(row[3]) if row[3] is not None else None,
                duration_seconds=int(row[4]) if row[4] is not None else None,
                distance_meters=int(row[5]) if row[5] is not None else None,
                calculated_at=row[6],
                itinerary=(
                    RouteItinerary.from_dict(itinerary)
                    if isinstance(itinerary, dict)
                    else None
                ),
            )
        )
    return output


def _geographic_sample(candidates: list[Candidate], size: int) -> list[Candidate]:
    """Select deterministically across a 5x5 London-area grid."""

    if size >= len(candidates):
        return list(candidates)
    latitudes = [candidate.origin.latitude for candidate in candidates]
    longitudes = [candidate.origin.longitude for candidate in candidates]
    latitude_range = max(latitudes) - min(latitudes) or 1
    longitude_range = max(longitudes) - min(longitudes) or 1
    buckets: dict[tuple[int, int], list[Candidate]] = {}
    for candidate in candidates:
        latitude_bin = min(
            4,
            int((candidate.origin.latitude - min(latitudes)) / latitude_range * 5),
        )
        longitude_bin = min(
            4,
            int((candidate.origin.longitude - min(longitudes)) / longitude_range * 5),
        )
        buckets.setdefault((latitude_bin, longitude_bin), []).append(candidate)
    for values in buckets.values():
        values.sort(key=lambda candidate: candidate.property_id)
    selected: list[Candidate] = []
    depth = 0
    while len(selected) < size:
        added = False
        for key in sorted(buckets):
            values = buckets[key]
            if depth < len(values):
                selected.append(values[depth])
                added = True
                if len(selected) == size:
                    break
        if not added:
            break
        depth += 1
    return selected


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    prepared = [float(value) for value in values]
    return {
        "count": len(prepared),
        "minimum": round(min(prepared), 2) if prepared else None,
        "p25": _rounded(_percentile(prepared, 0.25)),
        "median": _rounded(_percentile(prepared, 0.50)),
        "p75": _rounded(_percentile(prepared, 0.75)),
        "p90": _rounded(_percentile(prepared, 0.90)),
        "maximum": round(max(prepared), 2) if prepared else None,
    }


def _rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _route_record(
    candidate: Candidate,
    *,
    route_source: str,
    duration_seconds: int,
    distance_meters: int,
    calculated_at: datetime,
    itinerary: RouteItinerary,
    destination: Coordinates,
    destination_id: str,
    provider: str,
    provider_profile: str,
    router_version: str,
    network_version: str,
    city_versions: tuple[Any, ...],
    analyzer: PostgresMobilityContextAnalyzer,
) -> dict[str, object]:
    assessment = validate_exact_bicycle_route(
        itinerary,
        origin=candidate.origin,
        destination=destination,
        duration_seconds=duration_seconds,
        distance_meters=distance_meters,
    )
    contexts = {
        str(tolerance): analyzer.analyze(
            assessment.coordinates,
            origin=candidate.origin,
            tolerance_meters=tolerance,
        ).to_dict()
        for tolerance in VALIDATION_TOLERANCES_METERS
    }
    return {
        "property_id": candidate.property_id,
        "status": "ready",
        "route_source": route_source,
        "profile_id": candidate.profile_id if route_source == "current_cache" else None,
        "origin": candidate.origin.to_dict(),
        "destination_id": destination_id,
        "destination": destination.to_dict(),
        "mode": "cycling",
        "duration_seconds": duration_seconds,
        "distance_meters": distance_meters,
        "route_generated_at": calculated_at.isoformat(),
        "routing_provider": provider,
        "provider_profile": provider_profile,
        "router_version": router_version,
        "network_version": network_version,
        "algorithm_version": ALGORITHM_VERSION,
        "selected_tolerance_meters": DEFAULT_TOLERANCE_METERS,
        "dependency_fingerprint": dependency_fingerprint(
            property_id=candidate.property_id,
            origin=candidate.origin,
            destination_id=destination_id,
            destination=destination,
            routing_provider=provider,
            provider_profile=provider_profile,
            router_version=router_version,
            network_version=network_version,
            city_versions=city_versions,
        ),
        "geometry": assessment.to_dict(),
        "tolerances": contexts,
        "warnings": [],
        "_coordinates": assessment.coordinates,
    }


def _error_record(candidate: Candidate, source: str, error: str) -> dict[str, object]:
    return {
        "property_id": candidate.property_id,
        "status": "unavailable",
        "route_source": source,
        "origin": candidate.origin.to_dict(),
        "mode": "cycling",
        "failure": error,
    }


def _summary(
    records: list[dict[str, object]],
    *,
    route_eligible_count: int,
    current_cached_count: int,
    live_attempts: int,
    live_seconds: list[float],
    mask_build_milliseconds: float,
) -> dict[str, object]:
    ready = [record for record in records if record["status"] == "ready"]
    unavailable = [record for record in records if record["status"] != "ready"]
    selected_key = str(DEFAULT_TOLERANCE_METERS)
    selected_contexts = [record["tolerances"][selected_key] for record in ready]
    tolerance_summary: dict[str, object] = {}
    for tolerance in VALIDATION_TOLERANCES_METERS:
        contexts = [record["tolerances"][str(tolerance)] for record in ready]
        tolerance_summary[str(tolerance)] = {
            "covered_meters": _distribution(
                context["covered_meters"] for context in contexts
            ),
            "route_share_percent": _distribution(
                float(context["route_share"]) * 100 for context in contexts
            ),
        }
    facility_totals = {
        category: round(
            sum(
                float(context["facility_meters"].get(category, 0))
                for context in selected_contexts
            ),
            2,
        )
        for category in (
            "separated",
            "designated",
            "shared",
            "multi_use_path",
            "thames_valley_parkway",
        )
    }
    sorted_overlap = sorted(
        ready,
        key=lambda record: float(record["tolerances"][selected_key]["route_share"]),
    )
    tolerance_sensitive = sorted(
        ready,
        key=lambda record: abs(
            float(record["tolerances"]["15"]["route_share"])
            - float(record["tolerances"]["5"]["route_share"])
        ),
        reverse=True,
    )[:10]
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "route_eligible_property_count": route_eligible_count,
        "current_compatible_cached_route_count": current_cached_count,
        "sample_count": len(records),
        "ready_count": len(ready),
        "unavailable_count": len(unavailable),
        "invalid_geometry_count": sum(
            "geometry" in str(record.get("failure", "")) for record in unavailable
        ),
        "no_route_count": sum(
            record.get("failure") == "no_route" for record in unavailable
        ),
        "live_route_attempts": live_attempts,
        "live_route_seconds": _distribution(live_seconds),
        "mask_build_milliseconds": round(mask_build_milliseconds, 2),
        "duration_minutes": _distribution(
            float(record["duration_seconds"]) / 60 for record in ready
        ),
        "route_distance_km": _distribution(
            float(record["distance_meters"]) / 1000 for record in ready
        ),
        "nearest_bicycle_route_meters": _distribution(
            float(context["nearest_bicycle_route_meters"])
            for context in selected_contexts
            if context["nearest_bicycle_route_meters"] is not None
        ),
        "nearest_path_meters": _distribution(
            float(context["nearest_path_meters"])
            for context in selected_contexts
            if context["nearest_path_meters"] is not None
        ),
        "selected_tolerance_meters": DEFAULT_TOLERANCE_METERS,
        "selected_overlap_meters": _distribution(
            float(context["covered_meters"]) for context in selected_contexts
        ),
        "selected_overlap_percent": _distribution(
            float(context["route_share"]) * 100 for context in selected_contexts
        ),
        "selected_overlap_query_milliseconds": _distribution(
            float(context["elapsed_milliseconds"]) for context in selected_contexts
        ),
        "tolerance_summary": tolerance_summary,
        "facility_breakdown_non_additive_meters": facility_totals,
        "lowest_overlap_property_ids": [
            record["property_id"] for record in sorted_overlap[:5]
        ],
        "highest_overlap_property_ids": [
            record["property_id"] for record in sorted_overlap[-5:]
        ],
        "most_tolerance_sensitive_property_ids": [
            record["property_id"] for record in tolerance_sensitive
        ],
        "failure_counts": {
            reason: sum(record.get("failure") == reason for record in unavailable)
            for reason in sorted(
                {str(record.get("failure")) for record in unavailable}
            )
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def _svg_lines(geometry: dict[str, object]) -> list[list[list[float]]]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "LineString" and isinstance(coordinates, list):
        return [coordinates]
    if geometry_type == "MultiLineString" and isinstance(coordinates, list):
        return coordinates
    return []


def _diagnostic_svg(collection: dict[str, object]) -> str:
    features = collection.get("features")
    if not isinstance(features, list):
        raise ValueError("diagnostic GeoJSON has no features")
    all_lines = [
        (feature, line)
        for feature in features
        if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)
        for line in _svg_lines(feature["geometry"])
    ]
    points = [point for _, line in all_lines for point in line]
    if not points:
        raise ValueError("diagnostic GeoJSON has no line coordinates")
    minimum_x = min(float(point[0]) for point in points)
    maximum_x = max(float(point[0]) for point in points)
    minimum_y = min(float(point[1]) for point in points)
    maximum_y = max(float(point[1]) for point in points)
    x_range = maximum_x - minimum_x or 1
    y_range = maximum_y - minimum_y or 1

    def projected(line: list[list[float]]) -> str:
        return " ".join(
            f"{20 + (float(point[0]) - minimum_x) / x_range * 1160:.1f},"
            f"{780 - (float(point[1]) - minimum_y) / y_range * 760:.1f}"
            for point in line
        )

    colors = {
        "separated": "#0072b2",
        "designated": "#009e73",
        "shared": "#e69f00",
        "multi_use_path": "#56b4e9",
        "thames_valley_parkway": "#cc79a7",
    }
    city = []
    route = []
    for feature, line in all_lines:
        properties = feature.get("properties") or {}
        if properties.get("kind") == "otp_bicycle_route":
            route.append(
                f'<polyline points="{projected(line)}" fill="none" stroke="#111" stroke-width="4"/>'
            )
        else:
            category = str(properties.get("facility_category") or "unknown")
            city.append(
                f'<polyline points="{projected(line)}" fill="none" stroke="{colors.get(category, "#777")}" stroke-width="2" opacity="0.8"/>'
            )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800" '
        'viewBox="0 0 1200 800"><rect width="1200" height="800" fill="white"/>'
        + "".join(city)
        + "".join(route)
        + "</svg>\n"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=80)
    parser.add_argument("--live-route-limit", type=int, default=0)
    parser.add_argument("--diagnostic-count", type=int, default=6)
    parser.add_argument("--diagnostic-property-id", type=int, action="append", default=[])
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "mobility-context-validation",
    )
    args = parser.parse_args(argv)
    if not 30 <= args.sample_size <= 100:
        parser.error("--sample-size must be between 30 and 100")
    if not 0 <= args.live_route_limit <= min(50, args.sample_size):
        parser.error("--live-route-limit must be between 0 and 50 and no larger than sample")
    if not 0 <= args.diagnostic_count <= 10:
        parser.error("--diagnostic-count must be between 0 and 10")
    if len(set(args.diagnostic_property_id)) != len(args.diagnostic_property_id):
        parser.error("--diagnostic-property-id values must be unique")
    if len(args.diagnostic_property_id) > 10:
        parser.error("at most 10 explicit diagnostic properties are supported")

    config = load_worker_config(
        PROJECT_ROOT / "config" / "accessibility-worker.toml", PROJECT_ROOT
    )
    bundle = load_routing_bundle(config)
    hotspot = find_hotspot(DEFAULT_HOTSPOTS, "western-main-campus")
    if hotspot is None or hotspot.coordinates is None:
        raise RuntimeError("verified western-main-campus destination is unavailable")
    provider = OpenTripPlannerProvider(
        base_url=config.otp_base_url,
        router_id=config.otp_router_id,
        timeout_seconds=config.otp_request_timeout_seconds,
        metadata=bundle.metadata,
    )
    if args.live_route_limit:
        provider.preflight(bundle.metadata)

    import psycopg

    run_started = datetime.now(timezone.utc)
    with psycopg.connect(database_url_from_environment()) as connection:
        city_versions = current_city_versions(connection)
        candidates = _candidate_rows(
            connection,
            provider=provider.provider_name,
            provider_profile=config.provider_profile,
            network_version=bundle.metadata.network_version,
            destination_id=hotspot.id,
        )
        selected = _geographic_sample(candidates, args.sample_size)
        current_cached_count = sum(candidate.itinerary is not None for candidate in candidates)
        analyzer = PostgresMobilityContextAnalyzer(connection)
        mask_build_ms = analyzer.prepare_masks(VALIDATION_TOLERANCES_METERS)
        records: list[dict[str, object]] = []
        live_seconds: list[float] = []
        for index, candidate in enumerate(selected):
            if index < args.live_route_limit:
                start = perf_counter()
                try:
                    route = provider.get_route(
                        RouteRequest(
                            origin=candidate.origin,
                            destination=hotspot.coordinates,
                            mode=TravelMode.CYCLING,
                        )
                    )
                    live_seconds.append(perf_counter() - start)
                    if route.itinerary is None:
                        raise MobilityContextValidationError(
                            "live exact route has no itinerary"
                        )
                    record = _route_record(
                        candidate,
                        route_source="live_local_otp",
                        duration_seconds=route.duration_seconds or 0,
                        distance_meters=route.distance_meters or 0,
                        calculated_at=route.metadata.calculated_at,
                        itinerary=route.itinerary,
                        destination=hotspot.coordinates,
                        destination_id=hotspot.id,
                        provider=provider.provider_name,
                        provider_profile=config.provider_profile,
                        router_version=bundle.metadata.router_version,
                        network_version=bundle.metadata.network_version,
                        city_versions=city_versions,
                        analyzer=analyzer,
                    )
                except NoRouteError:
                    live_seconds.append(perf_counter() - start)
                    record = _error_record(candidate, "live_local_otp", "no_route")
                except (RoutingProviderError, MobilityContextValidationError) as error:
                    live_seconds.append(perf_counter() - start)
                    record = _error_record(
                        candidate,
                        "live_local_otp",
                        f"{type(error).__name__}: {error}",
                    )
            elif candidate.itinerary is None or candidate.calculated_at is None:
                record = _error_record(
                    candidate, "current_cache", "missing_compatible_cached_route"
                )
            else:
                try:
                    record = _route_record(
                        candidate,
                        route_source="current_cache",
                        duration_seconds=candidate.duration_seconds or 0,
                        distance_meters=candidate.distance_meters or 0,
                        calculated_at=candidate.calculated_at,
                        itinerary=candidate.itinerary,
                        destination=hotspot.coordinates,
                        destination_id=hotspot.id,
                        provider=provider.provider_name,
                        provider_profile=config.provider_profile,
                        router_version=bundle.metadata.router_version,
                        network_version=bundle.metadata.network_version,
                        city_versions=city_versions,
                        analyzer=analyzer,
                    )
                except MobilityContextValidationError as error:
                    record = _error_record(
                        candidate,
                        "current_cache",
                        f"{type(error).__name__}: {error}",
                    )
            records.append(record)

        ready = [record for record in records if record["status"] == "ready"]
        output = args.output_root / run_started.strftime("%Y%m%dT%H%M%SZ")
        output.mkdir(parents=True, exist_ok=False)
        diagnostics = []
        diagnostic_records = (
            [
                record
                for record in ready
                if record["property_id"] in set(args.diagnostic_property_id)
            ]
            if args.diagnostic_property_id
            else ready[: args.diagnostic_count]
        )
        if args.diagnostic_property_id and len(diagnostic_records) != len(
            args.diagnostic_property_id
        ):
            found = {int(record["property_id"]) for record in diagnostic_records}
            missing = sorted(set(args.diagnostic_property_id) - found)
            raise RuntimeError(
                f"diagnostic properties are not ready in this sample: {missing}"
            )
        for record in diagnostic_records:
            coordinates = record.pop("_coordinates")
            collection = analyzer.diagnostic_features(coordinates)
            property_id = int(record["property_id"])
            geojson_path = output / f"property-{property_id}-overlay.geojson"
            svg_path = output / f"property-{property_id}-overlay.svg"
            _write_json(geojson_path, collection)
            svg_path.write_text(_diagnostic_svg(collection), encoding="utf-8")
            diagnostics.append(
                {
                    "property_id": property_id,
                    "geojson": geojson_path.name,
                    "svg": svg_path.name,
                }
            )
        for record in ready:
            record.pop("_coordinates", None)

        summary = _summary(
            records,
            route_eligible_count=len(candidates),
            current_cached_count=current_cached_count,
            live_attempts=args.live_route_limit,
            live_seconds=live_seconds,
            mask_build_milliseconds=mask_build_ms,
        )
        summary["batch_elapsed_seconds"] = round(
            (datetime.now(timezone.utc) - run_started).total_seconds(), 2
        )
        manifest = {
            "run_started_at": run_started.isoformat(),
            "purpose": "shadow-only Mobility Context V1 validation",
            "sample_size": args.sample_size,
            "selection": "deterministic round-robin across 5x5 coordinate bins",
            "location_contract": "current available + route_available properties only",
            "destination": hotspot.to_dict(),
            "routing": bundle.metadata.to_dict()
            | {"provider": provider.provider_name, "provider_profile": config.provider_profile},
            "city_versions": [version.to_dict() for version in city_versions],
            "included_city_datasets": list(INCLUDED_CITY_DATASETS),
            "excluded_city_datasets": list(EXCLUDED_CITY_DATASETS),
            "algorithm_version": ALGORITHM_VERSION,
            "tested_tolerances_meters": list(VALIDATION_TOLERANCES_METERS),
            "selected_tolerance_meters": DEFAULT_TOLERANCE_METERS,
            "diagnostics": diagnostics,
            "contains_raw_otp_payloads": False,
            "persists_property_metrics": False,
        }
        _write_json(output / "manifest.json", manifest)
        _write_json(output / "summary.json", summary)
        (output / "routes.jsonl").write_text(
            "".join(json.dumps(record, default=str) + "\n" for record in records),
            encoding="utf-8",
        )
    print(json.dumps({"output": str(output), "summary": summary}, indent=2))


if __name__ == "__main__":
    main()
