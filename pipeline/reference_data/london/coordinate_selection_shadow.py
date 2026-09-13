"""Read-only PostgreSQL evidence loader and artifact writer for coordinate-selection-v1."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

from .coordinate_selection import (
    CoordinateSelectionEvidence,
    CoordinateSelectionPolicy,
    Coordinates,
    ShadowCoordinateSelection,
    ShadowDecision,
    evaluate_coordinate_selection,
)
from .normalization import normalize_address


CITY_VISIBILITY_REASONS = {
    "city_reference_disagreement",
    "city_reference_minor_offset",
    "city_reference_offset",
}


@dataclass(frozen=True)
class ShadowCandidateRun:
    records: tuple[dict[str, Any], ...]
    summary: dict[str, Any]
    mutation_safety: dict[str, Any]
    runtime_seconds: float
    query_count: int = 3


def generate_shadow_candidate(
    connection: Any,
    policy: CoordinateSelectionPolicy,
    *,
    evaluated_at: datetime | None = None,
) -> ShadowCandidateRun:
    """Evaluate all current properties using three read-only, set-based queries."""

    started = perf_counter()
    evaluation_time = evaluated_at or datetime.now(timezone.utc)
    before = critical_table_fingerprints(connection)
    rows = _dict_rows(
        connection.execute(
            CANDIDATE_SQL,
            (
                policy.campus.longitude,
                policy.campus.latitude,
                policy.campus.longitude,
                policy.campus.latitude,
            ),
        )
    )
    records = tuple(
        _candidate_record(row, policy, evaluated_at=evaluation_time) for row in rows
    )
    after = critical_table_fingerprints(connection)
    if before != after:
        raise RuntimeError("shadow coordinate evaluation changed protected table state")
    runtime = perf_counter() - started
    mutation_safety = {
        "transaction_contract": "read-only caller transaction",
        "fingerprints_match": True,
        "before": before,
        "after": after,
        "production_property_coordinates_changed": 0,
        "production_observation_coordinates_changed": 0,
        "location_visibility_rows_changed": 0,
        "accessibility_rows_changed": 0,
        "ranking_rows_changed": 0,
        "routes_or_surfaces_rebuilt": 0,
    }
    return ShadowCandidateRun(
        records=records,
        summary=_summary(records, policy, evaluation_time, runtime),
        mutation_safety=mutation_safety,
        runtime_seconds=runtime,
    )


def critical_table_fingerprints(connection: Any) -> dict[str, dict[str, Any]]:
    """Fingerprint protected product state without reading large binary payloads."""

    rows = connection.execute(PROTECTED_STATE_SQL).fetchall()
    return {
        str(row[0]): {"row_count": int(row[1]), "fingerprint": str(row[2])}
        for row in rows
    }


def write_shadow_artifacts(run: ShadowCandidateRun, output_dir: Path) -> dict[str, Path]:
    """Write ignored audit artifacts; never write database or product state."""

    output_dir.mkdir(parents=True, exist_ok=True)
    automatic_movement_threshold = float(
        run.summary["policy_parameters"]["automatic_city_max_movement_meters"]
    )
    candidate_path = output_dir / "coordinate-candidates.csv"
    summary_path = output_dir / "summary.json"
    mutation_path = output_dir / "mutation-safety.json"
    top_path = output_dir / "top-20-movements.csv"
    over_100_path = output_dir / "city-selected-over-100m.csv"

    _write_csv(candidate_path, list(run.records))
    _write_csv(
        top_path,
        sorted(
            (
                record
                for record in run.records
                if record["city_evidence_movement_meters"] is not None
            ),
            key=lambda record: float(record["city_evidence_movement_meters"]),
            reverse=True,
        )[:20],
    )
    _write_csv(
        over_100_path,
        [
            record
            for record in run.records
            if record["decision"] == ShadowDecision.CITY_SELECTED_SHADOW.value
            and float(record["movement_meters"] or 0) > automatic_movement_threshold
        ],
    )
    _write_json(summary_path, run.summary)
    _write_json(mutation_path, run.mutation_safety)
    return {
        "candidates": candidate_path,
        "summary": summary_path,
        "mutation_safety": mutation_path,
        "top_movements": top_path,
        "over_100_meters": over_100_path,
    }


def _candidate_record(
    row: dict[str, Any],
    policy: CoordinateSelectionPolicy,
    *,
    evaluated_at: datetime,
) -> dict[str, Any]:
    evidence = _evidence(row)
    decision = evaluate_coordinate_selection(
        evidence,
        policy,
        evaluated_at=evaluated_at,
    )
    projected_status = _projected_location_status(row, decision)
    current_status = str(row.get("location_status") or "unavailable")
    current_flags = _visibility_flags(current_status)
    projected_flags = _visibility_flags(projected_status)
    campus_change = (
        abs(
            float(row["city_campus_distance_meters"])
            - float(row["current_campus_distance_meters"])
        )
        if decision.decision is ShadowDecision.CITY_SELECTED_SHADOW
        and row.get("city_campus_distance_meters") is not None
        and row.get("current_campus_distance_meters") is not None
        else 0.0
        if decision.decision is not ShadowDecision.NO_USABLE_COORDINATE_SHADOW
        else None
    )
    selected = decision.selected_coordinate_shadow
    return {
        "property_id": evidence.property_id,
        "display_address": row.get("display_address"),
        "policy_version": decision.policy_version,
        "decision": decision.decision.value,
        "research_disposition": decision.research_disposition.value,
        "selection_reason": decision.selection_reason,
        "selection_reason_codes": list(decision.selection_reason_codes),
        "current_source": evidence.current_source,
        "current_status": evidence.current_status,
        "current_latitude": (
            evidence.current_coordinate.latitude if evidence.current_coordinate else None
        ),
        "current_longitude": (
            evidence.current_coordinate.longitude if evidence.current_coordinate else None
        ),
        "selected_source_shadow": decision.selected_source_shadow,
        "shadow_latitude": selected.latitude if selected else None,
        "shadow_longitude": selected.longitude if selected else None,
        "movement_meters": _rounded(decision.movement_meters),
        "city_evidence_movement_meters": _rounded(
            evidence.current_city_movement_meters
        ),
        "city_geoapify_distance_meters": _rounded(
            evidence.city_geoapify_distance_meters
        ),
        "city_dataset_run_id": evidence.city_dataset_run_id,
        "city_dataset_fingerprint": evidence.city_dataset_fingerprint,
        "city_dataset_is_current": evidence.city_dataset_is_current,
        "city_dataset_validation_status": evidence.city_dataset_validation_status,
        "city_dataset_import_status": evidence.city_dataset_import_status,
        "municipal_address_id": evidence.municipal_address_id,
        "municipal_address_status": evidence.municipal_address_status,
        "city_latitude": (
            evidence.city_coordinate.latitude if evidence.city_coordinate else None
        ),
        "city_longitude": (
            evidence.city_coordinate.longitude if evidence.city_coordinate else None
        ),
        "city_geometry_valid": evidence.city_geometry_valid,
        "city_geometry_srid": evidence.city_geometry_srid,
        "city_match_method": evidence.city_match_method,
        "city_match_confidence": evidence.city_match_confidence,
        "city_review_required": evidence.city_review_required,
        "building_id": evidence.building_id,
        "parcel_id": evidence.parcel_id,
        "building_match_method": evidence.building_match_method,
        "parcel_match_method": evidence.parcel_match_method,
        "building_containing_count": evidence.building_containing_count,
        "parcel_containing_count": evidence.parcel_containing_count,
        "city_inside_building": evidence.city_inside_building,
        "city_inside_parcel": evidence.city_inside_parcel,
        "geoapify_building_distance_meters": _rounded(
            evidence.geoapify_building_distance_meters
        ),
        "geoapify_parcel_distance_meters": _rounded(
            evidence.geoapify_parcel_distance_meters
        ),
        "geoapify_inside_parcel": evidence.geoapify_inside_parcel,
        "parcel_area_square_meters": _rounded(
            evidence.parcel_area_square_meters
        ),
        "parcel_building_count": evidence.parcel_building_count,
        "parcel_municipal_address_count": (
            evidence.parcel_municipal_address_count
        ),
        "building_validation": decision.building_validation,
        "parcel_validation": decision.parcel_validation,
        "geoapify_result_id": evidence.geoapify_result_id,
        "geoapify_provider": evidence.geoapify_provider,
        "geoapify_status": evidence.geoapify_status,
        "geoapify_latitude": (
            evidence.geoapify_coordinate.latitude
            if evidence.geoapify_coordinate
            else None
        ),
        "geoapify_longitude": (
            evidence.geoapify_coordinate.longitude
            if evidence.geoapify_coordinate
            else None
        ),
        "geoapify_confidence": evidence.geoapify_confidence,
        "unit_match_status": decision.unit_match_status,
        "shared_civic_point_status": decision.shared_civic_point_status,
        "shared_municipal_property_count": evidence.shared_municipal_property_count,
        "evaluated_at": decision.evaluated_at.isoformat(),
        "current_location_status": current_status,
        "projected_location_status": projected_status,
        "projected_map_visible_change": projected_flags[0] != current_flags[0],
        "projected_route_available_change": projected_flags[1] != current_flags[1],
        "active_listing_count": int(row.get("active_listing_count") or 0),
        "latest_observation_count": int(row.get("latest_observation_count") or 0),
        "campus_distance_observation_count": int(
            row.get("campus_distance_observation_count") or 0
        ),
        "accessibility_profile_count": int(
            row.get("accessibility_profile_count") or 0
        ),
        "walking_profile_count": int(row.get("walking_profile_count") or 0),
        "cycling_profile_count": int(row.get("cycling_profile_count") or 0),
        "transit_profile_count": int(row.get("transit_profile_count") or 0),
        "cached_exact_route_count": int(row.get("cached_exact_route_count") or 0),
        "walking_surface_count": int(row.get("walking_surface_count") or 0),
        "current_ranking_row_count": int(row.get("current_ranking_row_count") or 0),
        "mobility_context_persisted_artifact_count": 0,
        "current_campus_distance_meters": _rounded(
            row.get("current_campus_distance_meters")
        ),
        "shadow_campus_distance_meters": _rounded(
            row.get("city_campus_distance_meters")
            if decision.decision is ShadowDecision.CITY_SELECTED_SHADOW
            else row.get("current_campus_distance_meters")
        ),
        "campus_distance_absolute_change_meters": _rounded(campus_change),
    }


def _evidence(row: dict[str, Any]) -> CoordinateSelectionEvidence:
    return CoordinateSelectionEvidence(
        property_id=int(row["property_id"]),
        current_source=_text(row.get("current_source")),
        current_status=_text(row.get("current_status")),
        current_coordinate=_coordinates(
            row.get("current_latitude"), row.get("current_longitude")
        ),
        current_city_movement_meters=_number(row.get("current_city_movement_meters")),
        geoapify_result_id=_integer(row.get("geoapify_result_id")),
        geoapify_provider=_text(row.get("geoapify_provider")),
        geoapify_status=_text(row.get("geoapify_status")),
        geoapify_coordinate=_coordinates(
            row.get("geoapify_latitude"), row.get("geoapify_longitude")
        ),
        geoapify_confidence=_number(row.get("geoapify_confidence")),
        city_dataset_run_id=_integer(row.get("city_dataset_run_id")),
        city_dataset_fingerprint=_text(row.get("city_dataset_fingerprint")),
        city_dataset_is_current=bool(row.get("city_dataset_is_current")),
        city_dataset_validation_status=_text(
            row.get("city_dataset_validation_status")
        ),
        city_dataset_import_status=_text(row.get("city_dataset_import_status")),
        municipal_address_id=_integer(row.get("municipal_address_id")),
        municipal_address_status=_text(row.get("municipal_address_status")),
        city_coordinate=_coordinates(
            row.get("city_latitude"), row.get("city_longitude")
        ),
        city_geometry_valid=bool(row.get("city_geometry_valid")),
        city_geometry_srid=_integer(row.get("city_geometry_srid")),
        city_match_method=_text(row.get("city_match_method")),
        city_match_confidence=_number(row.get("city_match_confidence")),
        city_review_required=bool(row.get("city_review_required")),
        city_match_reason_codes=tuple(row.get("city_match_reason_codes") or ()),
        canonical_unit_identifiers=_canonical_units(row),
        city_normalized_unit=_text(row.get("city_normalized_unit")),
        shared_municipal_property_count=int(
            row.get("shared_municipal_property_count") or 0
        ),
        building_id=_integer(row.get("building_id")),
        building_match_method=_text(row.get("building_match_method")),
        building_containing_count=int(row.get("building_containing_count") or 0),
        city_inside_building=_optional_bool(row.get("city_inside_building")),
        geoapify_building_distance_meters=_number(
            row.get("geoapify_building_distance_meters")
        ),
        parcel_id=_integer(row.get("parcel_id")),
        parcel_match_method=_text(row.get("parcel_match_method")),
        parcel_containing_count=int(row.get("parcel_containing_count") or 0),
        city_inside_parcel=_optional_bool(row.get("city_inside_parcel")),
        parcel_area_square_meters=_number(row.get("parcel_area_square_meters")),
        parcel_building_count=int(row.get("parcel_building_count") or 0),
        parcel_municipal_address_count=int(
            row.get("parcel_municipal_address_count") or 0
        ),
        geoapify_inside_parcel=_optional_bool(row.get("geoapify_inside_parcel")),
        geoapify_parcel_distance_meters=_number(
            row.get("geoapify_parcel_distance_meters")
        ),
        city_geoapify_distance_meters=_number(
            row.get("city_geoapify_distance_meters")
        ),
    )


def _canonical_units(row: dict[str, Any]) -> tuple[str, ...]:
    values: set[str] = set()
    stored = _text(row.get("unit_identifier"))
    if stored:
        values.add(stored.split(":", 1)[-1].strip().casefold())
    for address in [row.get("display_address"), *(row.get("observation_addresses") or ())]:
        normalized = normalize_address(_text(address))
        if normalized.unit:
            values.add(normalized.unit.strip().casefold())
    return tuple(sorted(value for value in values if value))


def _projected_location_status(
    row: dict[str, Any], decision: ShadowCoordinateSelection
) -> str:
    current = str(row.get("location_status") or "unavailable")
    if decision.decision is not ShadowDecision.CITY_SELECTED_SHADOW:
        return current
    remaining = set(row.get("location_reason_codes") or ()) - CITY_VISIBILITY_REASONS
    if not remaining:
        return "available"
    if remaining == {"canonical_map_not_ready"}:
        return "limited"
    return current


def _visibility_flags(status: str) -> tuple[bool, bool]:
    return {
        "available": (True, True),
        "limited": (True, False),
        "unavailable": (False, False),
    }.get(status, (False, False))


def _summary(
    records: tuple[dict[str, Any], ...],
    policy: CoordinateSelectionPolicy,
    evaluated_at: datetime,
    runtime_seconds: float,
) -> dict[str, Any]:
    selected = [
        record
        for record in records
        if record["decision"] == ShadowDecision.CITY_SELECTED_SHADOW.value
    ]
    movements = [float(record["movement_meters"]) for record in selected]
    campus_changes = [
        float(record["campus_distance_absolute_change_meters"])
        for record in selected
        if record["campus_distance_absolute_change_meters"] is not None
    ]
    all_transitions: dict[str, int] = {}
    candidate_transitions: dict[str, int] = {}
    for record in records:
        key = f'{record["current_location_status"]}_to_{record["projected_location_status"]}'
        all_transitions[key] = all_transitions.get(key, 0) + 1
        if record["decision"] == ShadowDecision.CITY_SELECTED_SHADOW.value:
            candidate_transitions[key] = candidate_transitions.get(key, 0) + 1
    return {
        "policy_version": policy.version,
        "evaluated_at": evaluated_at.isoformat(),
        "mode": "shadow_only_read_only",
        "policy_parameters": {
            "expected_city_srid": policy.expected_city_srid,
            "minimum_city_match_confidence": policy.minimum_city_match_confidence,
            "near_equivalent_meters": policy.near_equivalent_meters,
            "material_geometry_displacement_meters": (
                policy.material_geometry_displacement_meters
            ),
            "automatic_city_max_movement_meters": (
                policy.automatic_city_max_movement_meters
            ),
            "large_parcel_area_square_meters": (
                policy.large_parcel_area_square_meters
            ),
            "geoapify_near_parcel_meters": policy.geoapify_near_parcel_meters,
            "simple_parcel_max_municipal_address_count": (
                policy.simple_parcel_max_municipal_address_count
            ),
            "accepted_city_address_statuses": sorted(
                policy.accepted_city_address_statuses
            ),
            "campus_id": policy.campus_id,
            "campus": policy.campus.to_dict(),
        },
        "total_canonical_properties": len(records),
        "decision_counts": _counts(record["decision"] for record in records),
        "research_disposition_counts": _counts(
            record["research_disposition"] for record in records
        ),
        "movement_meters": {
            **_distribution(movements),
            "buckets": _movement_buckets(movements),
            "over_50": sum(value > 50 for value in movements),
            "over_100": sum(value > 100 for value in movements),
            "over_250": sum(value > 250 for value in movements),
        },
        "blocked_cases": {
            "unit_specific": sum(
                record["unit_match_status"] == "unresolved" for record in records
            ),
            "shared_civic_point": sum(
                record["shared_civic_point_status"] == "shared"
                for record in records
            ),
            "city_review_required": sum(
                bool(record["city_review_required"])
                and record["city_match_method"]
                in {"EXACT_CIVIC_MATCH", "EXACT_UNIT_MATCH"}
                for record in records
            ),
            "invalid_or_unresolved_geometry": sum(
                record["city_match_method"]
                in {"EXACT_CIVIC_MATCH", "EXACT_UNIT_MATCH"}
                and (
                    record["parcel_validation"] != "unique_contains"
                    or record["building_validation"]
                    in {"ambiguous", "contradiction"}
                )
                for record in records
            ),
            "refined_policy_gates": _policy_gate_counts(records),
        },
        "projected_location_visibility": {
            "simulation_only": True,
            "transitions": candidate_transitions,
            "all_property_transitions": all_transitions,
            "map_visible_changes": sum(
                bool(record["projected_map_visible_change"]) for record in records
            ),
            "route_available_changes": sum(
                bool(record["projected_route_available_change"])
                for record in records
            ),
        },
        "dependency_impact": {
            **_dependency_impact(selected),
        },
        "review_required_dependency_impact": _dependency_impact(
            [
                record
                for record in records
                if record["decision"] == ShadowDecision.CONFLICT_REVIEW_SHADOW.value
            ]
        ),
        "campus_distance_impact_meters": {
            **_distribution(campus_changes),
            "over_50": sum(value > 50 for value in campus_changes),
            "over_100": sum(value > 100 for value in campus_changes),
            "over_250": sum(value > 250 for value in campus_changes),
        },
        "performance": {
            "query_count": 3,
            "query_shape": "protected-state fingerprint + one set-based candidate query + protected-state fingerprint",
            "runtime_seconds": round(runtime_seconds, 4),
        },
        "production_changes": {
            "property_coordinates": 0,
            "observation_coordinates": 0,
            "location_visibility": 0,
            "accessibility": 0,
            "ranking": 0,
            "routes_or_surfaces_rebuilt": 0,
        },
    }


def _distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    ordered = sorted(float(value) for value in values)
    return {
        "count": len(ordered),
        "minimum": _rounded(min(ordered)) if ordered else None,
        "p25": _rounded(_percentile(ordered, 0.25)),
        "median": _rounded(_percentile(ordered, 0.50)),
        "p75": _rounded(_percentile(ordered, 0.75)),
        "p90": _rounded(_percentile(ordered, 0.90)),
        "p95": _rounded(_percentile(ordered, 0.95)),
        "maximum": _rounded(max(ordered)) if ordered else None,
    }


def _dependency_impact(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    cohort = list(records)
    return {
        "properties": len(cohort),
        "active_listings": sum(int(row["active_listing_count"]) for row in cohort),
        "latest_observations": sum(
            int(row["latest_observation_count"]) for row in cohort
        ),
        "campus_distance_observations": sum(
            int(row["campus_distance_observation_count"]) for row in cohort
        ),
        "accessibility_profiles": sum(
            int(row["accessibility_profile_count"]) for row in cohort
        ),
        "walking_profiles": sum(int(row["walking_profile_count"]) for row in cohort),
        "cycling_profiles": sum(int(row["cycling_profile_count"]) for row in cohort),
        "transit_profiles": sum(int(row["transit_profile_count"]) for row in cohort),
        "cached_exact_routes": sum(
            int(row["cached_exact_route_count"]) for row in cohort
        ),
        "walking_surfaces": sum(int(row["walking_surface_count"]) for row in cohort),
        "current_ranking_rows": sum(
            int(row["current_ranking_row_count"]) for row in cohort
        ),
        "persisted_mobility_context_artifacts": 0,
    }


def _policy_gate_counts(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    gate_codes = {
        "movement_threshold": "automatic_city_movement_threshold_exceeded",
        "large_parcel_without_unique_building": (
            "large_parcel_without_unique_building"
        ),
        "multi_building_without_unique_building": (
            "multi_building_parcel_without_unique_building"
        ),
        "geoapify_inside_parcel": "geoapify_inside_matched_parcel_conflict",
        "geoapify_near_parcel": "geoapify_near_matched_parcel_conflict",
        "address_complexity_without_unique_building": (
            "multi_address_parcel_without_unique_building"
        ),
    }
    cohort = list(records)
    counts = {
        name: sum(code in row["selection_reason_codes"] for row in cohort)
        for name, code in gate_codes.items()
    }
    all_codes = set(gate_codes.values())
    counts["unique_properties"] = sum(
        bool(all_codes.intersection(row["selection_reason_codes"])) for row in cohort
    )
    return counts


def _percentile(ordered: list[float], fraction: float) -> float | None:
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _movement_buckets(values: Iterable[float]) -> dict[str, int]:
    buckets = {
        "0_5": 0,
        "5_10": 0,
        "10_20": 0,
        "20_50": 0,
        "50_100": 0,
        "100_250": 0,
        "over_250": 0,
    }
    for value in values:
        key = (
            "0_5"
            if value <= 5
            else "5_10"
            if value <= 10
            else "10_20"
            if value <= 20
            else "20_50"
            if value <= 50
            else "50_100"
            if value <= 100
            else "100_250"
            if value <= 250
            else "over_250"
        )
        buckets[key] += 1
    return buckets


def _counts(values: Iterable[object]) -> dict[str, int]:
    output: dict[str, int] = {}
    for value in values:
        key = str(value)
        output[key] = output.get(key, 0) + 1
    return dict(sorted(output.items()))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, separators=(",", ":"))
                    if isinstance(value, list | dict)
                    else value
                    for key, value in row.items()
                }
            )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _dict_rows(cursor: Any) -> list[dict[str, Any]]:
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _coordinates(latitude: object, longitude: object) -> Coordinates | None:
    lat = _number(latitude)
    lon = _number(longitude)
    return Coordinates(lat, lon) if lat is not None and lon is not None else None


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_bool(value: object) -> bool | None:
    return bool(value) if value is not None else None


def _rounded(value: object) -> float | None:
    number = _number(value)
    return round(number, 2) if number is not None else None


def policy_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


CANDIDATE_SQL = r"""
with active_listings as (
    select * from public.housing_listings
    where status in ('active', 'possibly_removed', 'relisted')
),
active_properties as (
    select distinct property_id
    from active_listings
    where property_id is not null
),
latest_observations as (
    select distinct on (observation.listing_id)
           observation.listing_id, observation.address,
           observation.distance_to_western_km
    from public.housing_listing_observations observation
    join active_listings listing on listing.id=observation.listing_id
    order by observation.listing_id, observation.observed_at desc,
             observation.id desc
),
listing_dependencies as (
    select listing.property_id,
           count(*) as active_listing_count,
           count(observation.listing_id) as latest_observation_count,
           count(observation.listing_id) filter (
               where observation.distance_to_western_km is not null
           ) as campus_distance_observation_count,
           jsonb_agg(distinct observation.address) filter (
               where observation.address is not null
           ) as observation_addresses
    from active_listings listing
    left join latest_observations observation on observation.listing_id=listing.id
    group by listing.property_id
),
current_matches as (
    select * from reference_data.property_reference_matches where is_current
),
shared_addresses as (
    select match.municipal_address_id,
           count(distinct match.property_id) as property_count
    from current_matches match
    join active_properties active on active.property_id=match.property_id
    where match.municipal_address_id is not null
    group by match.municipal_address_id
),
address_run as (
    select * from reference_data.dataset_runs
    where dataset_name='municipal_addresses' and is_current
),
building_run as (
    select * from reference_data.dataset_runs
    where dataset_name='building_footprints' and is_current
),
parcel_run as (
    select * from reference_data.dataset_runs
    where dataset_name='parcels' and is_current
),
building_counts as (
    select match.property_id, count(building.id) as containing_count
    from current_matches match
    left join reference_data.municipal_addresses address
      on address.id=match.municipal_address_id
    left join building_run run on true
    left join reference_data.building_footprints building
      on building.dataset_run_id=run.id
     and address.geometry is not null
     and st_covers(building.geometry,address.geometry)
    group by match.property_id
),
parcel_counts as (
    select match.property_id, count(parcel.id) as containing_count
    from current_matches match
    left join reference_data.municipal_addresses address
      on address.id=match.municipal_address_id
    left join parcel_run run on true
    left join reference_data.parcels parcel
      on parcel.dataset_run_id=run.id
     and address.geometry is not null
     and st_covers(parcel.geometry,address.geometry)
    group by match.property_id
),
accessibility_dependencies as (
    select profile.origin_property_id,
           count(*) as profile_count,
           count(*) filter (where profile.travel_mode='walking') as walking_count,
           count(*) filter (where profile.travel_mode='cycling') as cycling_count,
           count(*) filter (where profile.travel_mode='transit') as transit_count,
           count(*) filter (
               where profile.result_type in (
                   'exact_route','cached_exact_property','cached_exact_origin'
               )
           ) as exact_route_count
    from public.housing_accessibility_profiles profile
    where not profile.is_stale
      and (profile.expires_at is null or profile.expires_at > now())
    group by profile.origin_property_id
),
surface_dependencies as (
    select property_id, count(*) as surface_count
    from public.housing_walk_time_surfaces
    group by property_id
),
ranking_dependencies as (
    select listing.property_id, count(*) as ranking_count
    from public.housing_listing_scores score
    join active_listings listing on listing.id=score.listing_id
    where score.is_current
    group by listing.property_id
),
joined as (
    select property.id as property_id, property.display_address,
           property.unit_identifier,
           property.geocode_provider as current_source,
           property.geocode_status as current_status,
           property.latitude as current_latitude,
           property.longitude as current_longitude,
           result.id as geoapify_result_id,
           result.provider as geoapify_provider,
           result.status as geoapify_status,
           result.latitude as geoapify_latitude,
           result.longitude as geoapify_longitude,
           result.confidence as geoapify_confidence,
           match.address_match_method as city_match_method,
           match.address_match_confidence as city_match_confidence,
           coalesce(match.review_required,false) as city_review_required,
           coalesce(match.reason_codes,'[]'::jsonb) as city_match_reason_codes,
           match.municipal_address_id, match.building_footprint_id as building_id,
           match.parcel_id,
           match.building_match_method, match.parcel_match_method,
           address.status as municipal_address_status,
           address.normalized_unit as city_normalized_unit,
           address.geometry as city_geometry,
           city_run.id as city_dataset_run_id,
           city_run.content_sha256 as city_dataset_fingerprint,
           coalesce(
             city_run.is_current and address.dataset_run_id=city_run.id,
             false
           ) as city_dataset_is_current,
           city_run.validation_status as city_dataset_validation_status,
           city_run.import_status as city_dataset_import_status,
           address.dataset_run_id=city_run.id as city_address_uses_current_dataset,
           building.geometry as building_geometry,
           parcel.geometry as parcel_geometry,
           coalesce(shared.property_count,0) as shared_municipal_property_count,
           coalesce(building_count.containing_count,0) as building_containing_count,
           coalesce(parcel_count.containing_count,0) as parcel_containing_count,
           parcel_context.parcel_area_square_meters,
           coalesce(parcel_context.building_count,0) as parcel_building_count,
           coalesce(parcel_context.municipal_address_count,0)
             as parcel_municipal_address_count,
           visibility.location_status,
           visibility.map_visible,
           visibility.route_available,
           coalesce(visibility.reason_codes,'[]'::jsonb) as location_reason_codes,
           coalesce(dependency.active_listing_count,0) as active_listing_count,
           coalesce(dependency.latest_observation_count,0) as latest_observation_count,
           coalesce(dependency.campus_distance_observation_count,0)
             as campus_distance_observation_count,
           coalesce(dependency.observation_addresses,'[]'::jsonb)
             as observation_addresses,
           coalesce(accessibility.profile_count,0) as accessibility_profile_count,
           coalesce(accessibility.walking_count,0) as walking_profile_count,
           coalesce(accessibility.cycling_count,0) as cycling_profile_count,
           coalesce(accessibility.transit_count,0) as transit_profile_count,
           coalesce(accessibility.exact_route_count,0) as cached_exact_route_count,
           coalesce(surface.surface_count,0) as walking_surface_count,
           coalesce(ranking.ranking_count,0) as current_ranking_row_count,
           case when property.longitude is not null and property.latitude is not null
             then st_transform(st_setsrid(st_makepoint(
               property.longitude,property.latitude),4326),26917) end as current_point,
           case when result.longitude is not null and result.latitude is not null
             then st_transform(st_setsrid(st_makepoint(
               result.longitude,result.latitude),4326),26917) end as geoapify_point
    from public.housing_properties property
    join active_properties active on active.property_id=property.id
    left join public.housing_geocode_results result
      on result.id=property.geocode_result_id
    left join current_matches match on match.property_id=property.id
    left join reference_data.municipal_addresses address
      on address.id=match.municipal_address_id
    left join address_run city_run on true
    left join building_run current_building_run on true
    left join reference_data.building_footprints building
      on building.id=match.building_footprint_id
     and building.dataset_run_id=current_building_run.id
    left join parcel_run current_parcel_run on true
    left join reference_data.parcels parcel
      on parcel.id=match.parcel_id
     and parcel.dataset_run_id=current_parcel_run.id
    left join lateral (
      select st_area(parcel.geometry) as parcel_area_square_meters,
             (
               select count(*)
               from reference_data.building_footprints value
               where value.dataset_run_id=current_building_run.id
                 and st_intersects(parcel.geometry,value.geometry)
             ) as building_count,
             (
               select count(*)
               from reference_data.municipal_addresses value
               where value.dataset_run_id=city_run.id
                 and st_covers(parcel.geometry,value.geometry)
             ) as municipal_address_count
    ) parcel_context on parcel.geometry is not null
    left join shared_addresses shared
      on shared.municipal_address_id=match.municipal_address_id
    left join building_counts building_count
      on building_count.property_id=property.id
    left join parcel_counts parcel_count on parcel_count.property_id=property.id
    left join public.housing_property_location_visibility visibility
      on visibility.property_id=property.id and visibility.is_current
    left join listing_dependencies dependency on dependency.property_id=property.id
    left join accessibility_dependencies accessibility
      on accessibility.origin_property_id=property.id
    left join surface_dependencies surface on surface.property_id=property.id
    left join ranking_dependencies ranking on ranking.property_id=property.id
),
spatial as (
    select joined.*,
           case when city_geometry is not null
                     and st_srid(city_geometry)=26917
                     and st_isvalid(city_geometry)
                     and not st_isempty(city_geometry)
                then st_y(st_transform(city_geometry,4326)) end as city_latitude,
           case when city_geometry is not null
                     and st_srid(city_geometry)=26917
                     and st_isvalid(city_geometry)
                     and not st_isempty(city_geometry)
                then st_x(st_transform(city_geometry,4326)) end as city_longitude,
           coalesce(city_geometry is not null
                    and st_geometrytype(city_geometry)='ST_Point'
                    and st_srid(city_geometry)=26917
                    and st_isvalid(city_geometry)
                    and not st_isempty(city_geometry),false) as city_geometry_valid,
           st_srid(city_geometry) as city_geometry_srid,
           case when city_geometry is not null and current_point is not null
                then st_distance(city_geometry,current_point) end
                as current_city_movement_meters,
           case when city_geometry is not null and geoapify_point is not null
                then st_distance(city_geometry,geoapify_point) end
                as city_geoapify_distance_meters,
           case when building_geometry is not null and city_geometry is not null
                then st_covers(building_geometry,city_geometry) end
                as city_inside_building,
           case when building_geometry is not null and geoapify_point is not null
                then st_distance(building_geometry,geoapify_point) end
                as geoapify_building_distance_meters,
           case when parcel_geometry is not null and city_geometry is not null
                then st_covers(parcel_geometry,city_geometry) end
                as city_inside_parcel,
           case when parcel_geometry is not null and geoapify_point is not null
                 then st_distance(parcel_geometry,geoapify_point) end
                 as geoapify_parcel_distance_meters,
           case when parcel_geometry is not null and geoapify_point is not null
                then st_covers(parcel_geometry,geoapify_point) end
                as geoapify_inside_parcel,
           case when current_longitude is not null and current_latitude is not null
                then st_distancesphere(
                  st_setsrid(st_makepoint(current_longitude,current_latitude),4326),
                  st_setsrid(st_makepoint(%s,%s),4326)
                ) end as current_campus_distance_meters
    from joined
)
select spatial.*,
       case when city_longitude is not null and city_latitude is not null
            then st_distancesphere(
              st_setsrid(st_makepoint(city_longitude,city_latitude),4326),
              st_setsrid(st_makepoint(%s,%s),4326)
            ) end as city_campus_distance_meters
from spatial
order by property_id
"""


PROTECTED_STATE_SQL = r"""
select name,row_count,fingerprint from (
  select 'housing_properties_coordinates' name,count(*) row_count,
         md5(coalesce(string_agg(concat_ws('|',id,
           coalesce(latitude::text,'null'),coalesce(longitude::text,'null'),
           coalesce(geocode_provider,'null'),coalesce(geocode_status,'null'),
           coalesce(geocode_result_id::text,'null')),',' order by id),'')) fingerprint
  from public.housing_properties
  union all
  select 'housing_listing_observations_coordinates',count(*),
         md5(coalesce(string_agg(concat_ws('|',id,
           coalesce(latitude::text,'null'),coalesce(longitude::text,'null'),
           coalesce(distance_to_western_km::text,'null'),map_ready::text),
           ',' order by id),''))
  from public.housing_listing_observations
  union all
  select 'housing_property_location_visibility',count(*),
         md5(coalesce(string_agg(concat_ws('|',id,property_id,policy_version,
           location_status,map_visible,route_available,is_current,
           coalesce(superseded_at::text,'null')),',' order by id),''))
  from public.housing_property_location_visibility
  union all
  select 'housing_accessibility_profiles',count(*),
         md5(coalesce(string_agg(concat_ws('|',id,cache_identity,is_stale,
           coalesce(stale_at::text,'null'),coalesce(updated_at::text,'null')),
           ',' order by id),''))
  from public.housing_accessibility_profiles
  union all
  select 'housing_accessibility_samples',count(*),
         md5(coalesce(string_agg(concat_ws('|',id,profile_id,status,
           coalesce(duration_seconds::text,'null')),
           ',' order by id),''))
  from public.housing_accessibility_samples
  union all
  select 'housing_walk_time_surfaces',count(*),
         md5(coalesce(string_agg(concat_ws('|',id,property_id,cache_identity,status,
           coalesce(computed_at::text,'null')),',' order by id),''))
  from public.housing_walk_time_surfaces
  union all
  select 'housing_ranking_runs',count(*),
         md5(coalesce(string_agg(concat_ws('|',id,run_id,status,
           coalesce(output_fingerprint,'null')),',' order by id),''))
  from public.housing_ranking_runs
  union all
  select 'housing_listing_scores',count(*),
         md5(coalesce(string_agg(concat_ws('|',id,listing_id,ranking_run_id,
           input_fingerprint,is_current,coalesce(superseded_at::text,'null')),
           ',' order by id),''))
  from public.housing_listing_scores
) protected
order by name
"""
