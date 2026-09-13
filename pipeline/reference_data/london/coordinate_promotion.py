"""Dry-run planning and disposable validation for coordinate-promotion-v1.

The planner consumes the byte-frozen coordinate-selection-v1 artifact.  It does
not re-run the selector.  The write path is deliberately guarded for disposable
test databases and is not used by the dry-run generator.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import tomllib
from typing import Any, Iterable

from .coordinate_selection_shadow import CANDIDATE_SQL, critical_table_fingerprints


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROMOTION_VERSION = "coordinate-promotion-v1"
CITY_DECISION = "CITY_SELECTED_SHADOW"
ACTIVE_LISTING_STATUSES = ("active", "possibly_removed", "relisted")
ZERO_SHA256 = "0" * 64


@dataclass(frozen=True)
class PromotionConfig:
    version: str
    coordinate_selection_policy_version: str
    coordinate_selection_policy_fingerprint: str
    candidate_run_id: str
    candidate_fingerprint: str
    expected_candidate_rows: int
    expected_city_selected: int
    expected_conflict_review: int
    expected_geoapify_retained: int
    expected_no_usable_coordinate: int
    maximum_automatic_movement_meters: float
    campus_id: str
    campus_latitude: float
    campus_longitude: float


@dataclass(frozen=True)
class FrozenCandidate:
    records: tuple[dict[str, str], ...]
    selected: tuple[dict[str, str], ...]
    decision_counts: dict[str, int]
    candidate_path: Path
    candidate_fingerprint: str
    policy_fingerprint: str


@dataclass(frozen=True)
class StaleCandidate:
    property_id: int
    evidence_fingerprint: str
    current_evidence_fingerprint: str | None
    changed_evidence: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class PromotionRow:
    property_id: int
    display_address: str
    previous_latitude: float
    previous_longitude: float
    previous_source: str
    previous_status: str
    previous_confidence: float | None
    previous_geocode_result_id: int | None
    new_latitude: float
    new_longitude: float
    new_distance_to_western_km: float
    movement_meters: float
    city_dataset_run_id: int
    city_dataset_fingerprint: str
    municipal_address_id: int
    building_id: int | None
    parcel_id: int | None
    city_match_method: str
    city_match_confidence: float
    selection_reason: str
    candidate_evidence_fingerprint: str
    active_listing_count: int
    latest_observation_count: int
    accessibility_profile_count: int
    walking_profile_count: int
    cycling_profile_count: int
    transit_profile_count: int
    cached_exact_route_count: int
    walking_surface_count: int
    current_ranking_row_count: int

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class PromotionPlan:
    config: PromotionConfig
    frozen: FrozenCandidate
    eligible: tuple[PromotionRow, ...]
    stale: tuple[StaleCandidate, ...]
    dependency_impact: dict[str, int]
    movement_audit: dict[str, Any]
    protected_before: dict[str, dict[str, Any]]
    protected_after: dict[str, dict[str, Any]]
    planned_at: datetime


@dataclass(frozen=True)
class BeforeStateSnapshot:
    state: dict[str, Any]
    state_fingerprint: str
    artifact_fingerprint: str | None = None


def load_promotion_config(path: Path | None = None) -> PromotionConfig:
    config_path = path or PROJECT_ROOT / "config" / "coordinate-promotion-v1.toml"
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    promotion = raw["promotion"]
    campus = raw["campus"]
    config = PromotionConfig(
        version=str(promotion["version"]),
        coordinate_selection_policy_version=str(
            promotion["coordinate_selection_policy_version"]
        ),
        coordinate_selection_policy_fingerprint=str(
            promotion["coordinate_selection_policy_fingerprint"]
        ),
        candidate_run_id=str(promotion["candidate_run_id"]),
        candidate_fingerprint=str(promotion["candidate_fingerprint"]),
        expected_candidate_rows=int(promotion["expected_candidate_rows"]),
        expected_city_selected=int(promotion["expected_city_selected"]),
        expected_conflict_review=int(promotion["expected_conflict_review"]),
        expected_geoapify_retained=int(promotion["expected_geoapify_retained"]),
        expected_no_usable_coordinate=int(
            promotion["expected_no_usable_coordinate"]
        ),
        maximum_automatic_movement_meters=float(
            promotion["maximum_automatic_movement_meters"]
        ),
        campus_id=str(campus["id"]),
        campus_latitude=float(campus["latitude"]),
        campus_longitude=float(campus["longitude"]),
    )
    if config.version != PROMOTION_VERSION:
        raise ValueError(f"unsupported promotion version: {config.version}")
    for fingerprint in (
        config.coordinate_selection_policy_fingerprint,
        config.candidate_fingerprint,
    ):
        if len(fingerprint) != 64:
            raise ValueError("promotion fingerprints must be SHA-256 values")
    if config.maximum_automatic_movement_meters <= 0:
        raise ValueError("automatic movement threshold must be positive")
    return config


def load_frozen_candidate(
    config: PromotionConfig, *, project_root: Path = PROJECT_ROOT
) -> FrozenCandidate:
    """Load and strictly validate the committed policy and ignored candidate bytes."""

    policy_path = project_root / "config" / "coordinate-selection-v1.toml"
    policy_fingerprint = _sha256_bytes(policy_path.read_bytes())
    if policy_fingerprint != config.coordinate_selection_policy_fingerprint:
        raise RuntimeError("frozen coordinate-selection policy fingerprint mismatch")

    run_dir = (
        project_root
        / "data"
        / "london-reference-validation"
        / "coordinate-selection-v1"
        / config.candidate_run_id
    )
    candidate_path = run_dir / "coordinate-candidates.csv"
    candidate_fingerprint = _sha256_bytes(candidate_path.read_bytes())
    if candidate_fingerprint != config.candidate_fingerprint:
        raise RuntimeError("frozen coordinate candidate fingerprint mismatch")

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    with candidate_path.open(encoding="utf-8-sig", newline="") as source:
        records = tuple(csv.DictReader(source))
    counts: dict[str, int] = {}
    for record in records:
        counts[record["decision"]] = counts.get(record["decision"], 0) + 1
    expected = {
        "CITY_SELECTED_SHADOW": config.expected_city_selected,
        "CONFLICT_REVIEW_SHADOW": config.expected_conflict_review,
        "GEOAPIFY_RETAINED_SHADOW": config.expected_geoapify_retained,
        "NO_USABLE_COORDINATE_SHADOW": config.expected_no_usable_coordinate,
    }
    if len(records) != config.expected_candidate_rows or counts != expected:
        raise RuntimeError("frozen coordinate candidate counts mismatch")
    if summary.get("policy_version") != config.coordinate_selection_policy_version:
        raise RuntimeError("frozen candidate policy version mismatch")
    summary_counts = summary.get("decision_counts")
    if summary_counts != expected:
        raise RuntimeError("frozen candidate summary decision counts mismatch")

    selected = tuple(record for record in records if record["decision"] == CITY_DECISION)
    ids = [int(record["property_id"]) for record in selected]
    if len(ids) != len(set(ids)):
        raise RuntimeError("frozen promotion candidate contains duplicate properties")
    over_limit = [
        record
        for record in selected
        if float(record["movement_meters"])
        > config.maximum_automatic_movement_meters
    ]
    if over_limit:
        raise RuntimeError("frozen automatic cohort contains movement over 100 m")
    return FrozenCandidate(
        records=records,
        selected=selected,
        decision_counts=counts,
        candidate_path=candidate_path,
        candidate_fingerprint=candidate_fingerprint,
        policy_fingerprint=policy_fingerprint,
    )


def build_promotion_plan(
    connection: Any,
    config: PromotionConfig,
    *,
    project_root: Path = PROJECT_ROOT,
    planned_at: datetime | None = None,
) -> PromotionPlan:
    """Revalidate frozen evidence and measure impact without selector recalculation."""

    frozen = load_frozen_candidate(config, project_root=project_root)
    before = critical_table_fingerprints(connection)
    rows = _dict_rows(
        connection.execute(
            CANDIDATE_SQL,
            (
                config.campus_longitude,
                config.campus_latitude,
                config.campus_longitude,
                config.campus_latitude,
            ),
        )
    )
    current = {int(row["property_id"]): row for row in rows}
    eligible: list[PromotionRow] = []
    stale: list[StaleCandidate] = []
    for candidate in frozen.selected:
        property_id = int(candidate["property_id"])
        expected = candidate_evidence(candidate)
        actual_row = current.get(property_id)
        actual = current_evidence(actual_row) if actual_row is not None else None
        changes = _evidence_changes(expected, actual)
        if changes:
            stale.append(
                StaleCandidate(
                    property_id=property_id,
                    evidence_fingerprint=_json_sha256(expected),
                    current_evidence_fingerprint=(
                        _json_sha256(actual) if actual is not None else None
                    ),
                    changed_evidence=changes,
                )
            )
            continue
        assert actual_row is not None
        eligible.append(_promotion_row(candidate, actual_row, config))
    after = critical_table_fingerprints(connection)
    if before != after:
        raise RuntimeError("promotion planning changed protected database state")
    movement = movement_audit(row.movement_meters for row in eligible)
    if movement["buckets"][">100"]:
        raise RuntimeError("fresh automatic promotion cohort contains movement over 100 m")
    return PromotionPlan(
        config=config,
        frozen=frozen,
        eligible=tuple(eligible),
        stale=tuple(stale),
        dependency_impact=_dependency_impact(eligible),
        movement_audit=movement,
        protected_before=before,
        protected_after=after,
        planned_at=planned_at or datetime.now(timezone.utc),
    )


def capture_before_state(
    connection: Any,
    plan: PromotionPlan,
    *,
    captured_at: datetime | None = None,
) -> BeforeStateSnapshot:
    """Capture deterministic rollback evidence before a real coordinate cutover."""

    property_ids = [row.property_id for row in plan.eligible]
    if not property_ids:
        raise RuntimeError("refusing to snapshot an empty promotion cohort")
    candidate_by_id = {row.property_id: row for row in plan.eligible}
    rows = _dict_rows(
        connection.execute(
            """
            select property.id as property_id,property.latitude,property.longitude,
                   property.geocode_provider,property.geocode_status,
                   property.geocode_confidence,property.geocode_result_id,
                   coalesce(projection.values,'[]'::jsonb) as latest_projections,
                   visibility.value as current_visibility
            from public.housing_properties property
            left join lateral (
              select jsonb_agg(jsonb_build_object(
                'id',observation.id,'listing_id',observation.listing_id,
                'latitude',observation.latitude,'longitude',observation.longitude,
                'distance_to_western_km',observation.distance_to_western_km,
                'map_ready',observation.map_ready,
                'geocode_status',observation.geocode_status,
                'geocode_confidence',observation.geocode_confidence,
                'geocode_quality_issue',observation.geocode_quality_issue,
                'provenance_data',observation.provenance_data
              ) order by observation.id) as values
              from public.housing_listings listing
              join lateral (
                select candidate.* from public.housing_listing_observations candidate
                where candidate.listing_id=listing.id
                order by candidate.observed_at desc,candidate.id desc limit 1
              ) observation on true
              where listing.property_id=property.id
                and listing.status=any(%s)
            ) projection on true
            left join lateral (
              select jsonb_build_object(
                'id',value.id,'policy_version',value.policy_version,
                'location_status',value.location_status,
                'map_visible',value.map_visible,
                'route_available',value.route_available,
                'reason_codes',value.reason_codes,
                'source_run_id',value.source_run_id,
                'source_fingerprint',value.source_fingerprint,
                'assessed_at',value.assessed_at
              ) as value
              from public.housing_property_location_visibility value
              where value.property_id=property.id and value.is_current
            ) visibility on true
            where property.id=any(%s)
            order by property.id
            """,
            (list(ACTIVE_LISTING_STATUSES), property_ids),
        )
    )
    if len(rows) != len(property_ids):
        raise RuntimeError("before-state property count mismatch")
    properties: list[dict[str, Any]] = []
    for row in rows:
        candidate = candidate_by_id[int(row["property_id"])]
        properties.append(
            {
                "property_id": int(row["property_id"]),
                "previous_property": {
                    "latitude": row["latitude"],
                    "longitude": row["longitude"],
                    "geocode_provider": row["geocode_provider"],
                    "geocode_status": row["geocode_status"],
                    "geocode_confidence": row["geocode_confidence"],
                    "geocode_result_id": row["geocode_result_id"],
                },
                "latest_projections": row["latest_projections"],
                "current_visibility": row["current_visibility"],
                "proposed_coordinate": {
                    "latitude": candidate.new_latitude,
                    "longitude": candidate.new_longitude,
                    "source": "city_of_london",
                    "distance_to_western_km": candidate.new_distance_to_western_km,
                },
                "movement_meters": candidate.movement_meters,
                "candidate_evidence_fingerprint": (
                    candidate.candidate_evidence_fingerprint
                ),
            }
        )
    observation_rows = _dict_rows(
        connection.execute(
            """
            select observation.id,observation.listing_id,observation.property_id,
                   observation.latitude,observation.longitude,
                   observation.distance_to_western_km,observation.map_ready,
                   observation.geocode_status,observation.geocode_confidence,
                   observation.geocode_quality_issue,observation.raw_data,
                   observation.provenance_data
            from public.housing_listing_observations observation
            where observation.property_id=any(%s)
            order by observation.id
            """,
            (property_ids,),
        )
    )
    state = {
        "promotion_policy_version": plan.config.version,
        "coordinate_selection_policy_version": (
            plan.config.coordinate_selection_policy_version
        ),
        "coordinate_selection_policy_fingerprint": (
            plan.config.coordinate_selection_policy_fingerprint
        ),
        "candidate_run_id": plan.config.candidate_run_id,
        "candidate_fingerprint": plan.config.candidate_fingerprint,
        "property_count": len(properties),
        "latest_projection_count": sum(
            len(row["latest_projections"]) for row in properties
        ),
        "properties": properties,
        "all_observations": observation_rows,
    }
    safe_state = _json_safe(state)
    fingerprint = _json_sha256(safe_state)
    return BeforeStateSnapshot(
        state={
            **safe_state,
            "captured_at": (
                captured_at or datetime.now(timezone.utc)
            ).isoformat(),
            "state_fingerprint": fingerprint,
        },
        state_fingerprint=fingerprint,
    )


def write_before_state_snapshot(
    snapshot: BeforeStateSnapshot, path: Path
) -> BeforeStateSnapshot:
    """Write and byte-fingerprint a local ignored rollback snapshot."""

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, snapshot.state)
    return BeforeStateSnapshot(
        state=snapshot.state,
        state_fingerprint=snapshot.state_fingerprint,
        artifact_fingerprint=_sha256_bytes(path.read_bytes()),
    )


def candidate_evidence(record: dict[str, str]) -> dict[str, Any]:
    """Canonical evidence fingerprint payload stored by the frozen candidate."""

    return {
        "property_id": _int(record["property_id"]),
        "display_address": _text(record["display_address"]),
        "current_source": _text(record["current_source"]),
        "current_status": _text(record["current_status"]),
        "current_latitude": _float(record["current_latitude"], 8),
        "current_longitude": _float(record["current_longitude"], 8),
        "geoapify_result_id": _int(record["geoapify_result_id"]),
        "geoapify_provider": _text(record["geoapify_provider"]),
        "geoapify_status": _text(record["geoapify_status"]),
        "geoapify_latitude": _float(record["geoapify_latitude"], 8),
        "geoapify_longitude": _float(record["geoapify_longitude"], 8),
        "geoapify_confidence": _float(record["geoapify_confidence"], 8),
        "city_dataset_run_id": _int(record["city_dataset_run_id"]),
        "city_dataset_fingerprint": _text(record["city_dataset_fingerprint"]),
        "city_dataset_is_current": _bool(record["city_dataset_is_current"]),
        "city_dataset_validation_status": _text(record["city_dataset_validation_status"]),
        "city_dataset_import_status": _text(record["city_dataset_import_status"]),
        "municipal_address_id": _int(record["municipal_address_id"]),
        "municipal_address_status": _text(record["municipal_address_status"]),
        "city_latitude": _float(record["city_latitude"], 8),
        "city_longitude": _float(record["city_longitude"], 8),
        "city_geometry_valid": _bool(record["city_geometry_valid"]),
        "city_geometry_srid": _int(record["city_geometry_srid"]),
        "city_match_method": _text(record["city_match_method"]),
        "city_match_confidence": _float(record["city_match_confidence"], 8),
        "city_review_required": _bool(record["city_review_required"]),
        "building_id": _int(record["building_id"]),
        "parcel_id": _int(record["parcel_id"]),
        "building_match_method": _text(record["building_match_method"]),
        "parcel_match_method": _text(record["parcel_match_method"]),
        "building_containing_count": _int(record["building_containing_count"]),
        "parcel_containing_count": _int(record["parcel_containing_count"]),
        "city_inside_building": _bool(record["city_inside_building"]),
        "city_inside_parcel": _bool(record["city_inside_parcel"]),
        "geoapify_building_distance_meters": _float(record["geoapify_building_distance_meters"], 2),
        "geoapify_parcel_distance_meters": _float(record["geoapify_parcel_distance_meters"], 2),
        "geoapify_inside_parcel": _bool(record["geoapify_inside_parcel"]),
        "parcel_area_square_meters": _float(record["parcel_area_square_meters"], 2),
        "parcel_building_count": _int(record["parcel_building_count"]),
        "parcel_municipal_address_count": _int(record["parcel_municipal_address_count"]),
        "shared_municipal_property_count": _int(record["shared_municipal_property_count"]),
        "movement_meters": _float(record["city_evidence_movement_meters"], 2),
    }


def current_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "property_id": _int(row.get("property_id")),
        "display_address": _text(row.get("display_address")),
        "current_source": _text(row.get("current_source")),
        "current_status": _text(row.get("current_status")),
        "current_latitude": _float(row.get("current_latitude"), 8),
        "current_longitude": _float(row.get("current_longitude"), 8),
        "geoapify_result_id": _int(row.get("geoapify_result_id")),
        "geoapify_provider": _text(row.get("geoapify_provider")),
        "geoapify_status": _text(row.get("geoapify_status")),
        "geoapify_latitude": _float(row.get("geoapify_latitude"), 8),
        "geoapify_longitude": _float(row.get("geoapify_longitude"), 8),
        "geoapify_confidence": _float(row.get("geoapify_confidence"), 8),
        "city_dataset_run_id": _int(row.get("city_dataset_run_id")),
        "city_dataset_fingerprint": _text(row.get("city_dataset_fingerprint")),
        "city_dataset_is_current": _bool(row.get("city_dataset_is_current")),
        "city_dataset_validation_status": _text(row.get("city_dataset_validation_status")),
        "city_dataset_import_status": _text(row.get("city_dataset_import_status")),
        "municipal_address_id": _int(row.get("municipal_address_id")),
        "municipal_address_status": _text(row.get("municipal_address_status")),
        "city_latitude": _float(row.get("city_latitude"), 8),
        "city_longitude": _float(row.get("city_longitude"), 8),
        "city_geometry_valid": _bool(row.get("city_geometry_valid")),
        "city_geometry_srid": _int(row.get("city_geometry_srid")),
        "city_match_method": _text(row.get("city_match_method")),
        "city_match_confidence": _float(row.get("city_match_confidence"), 8),
        "city_review_required": _bool(row.get("city_review_required")),
        "building_id": _int(row.get("building_id")),
        "parcel_id": _int(row.get("parcel_id")),
        "building_match_method": _text(row.get("building_match_method")),
        "parcel_match_method": _text(row.get("parcel_match_method")),
        "building_containing_count": _int(row.get("building_containing_count")),
        "parcel_containing_count": _int(row.get("parcel_containing_count")),
        "city_inside_building": _bool(row.get("city_inside_building")),
        "city_inside_parcel": _bool(row.get("city_inside_parcel")),
        "geoapify_building_distance_meters": _float(row.get("geoapify_building_distance_meters"), 2),
        "geoapify_parcel_distance_meters": _float(row.get("geoapify_parcel_distance_meters"), 2),
        "geoapify_inside_parcel": _bool(row.get("geoapify_inside_parcel")),
        "parcel_area_square_meters": _float(row.get("parcel_area_square_meters"), 2),
        "parcel_building_count": _int(row.get("parcel_building_count")),
        "parcel_municipal_address_count": _int(row.get("parcel_municipal_address_count")),
        "shared_municipal_property_count": _int(row.get("shared_municipal_property_count")),
        "movement_meters": _float(row.get("current_city_movement_meters"), 2),
    }


def movement_audit(values: Iterable[float]) -> dict[str, Any]:
    ordered = sorted(float(value) for value in values)
    buckets = {"0-5": 0, "5-10": 0, "10-20": 0, "20-50": 0, "50-100": 0, ">100": 0}
    for value in ordered:
        if value <= 5:
            buckets["0-5"] += 1
        elif value <= 10:
            buckets["5-10"] += 1
        elif value <= 20:
            buckets["10-20"] += 1
        elif value <= 50:
            buckets["20-50"] += 1
        elif value <= 100:
            buckets["50-100"] += 1
        else:
            buckets[">100"] += 1
    return {
        "count": len(ordered),
        "minimum": round(ordered[0], 2) if ordered else None,
        "median": round(statistics.median(ordered), 2) if ordered else None,
        "p75": _percentile(ordered, 0.75),
        "p90": _percentile(ordered, 0.90),
        "p95": _percentile(ordered, 0.95),
        "maximum": round(ordered[-1], 2) if ordered else None,
        "buckets": buckets,
    }


def haversine_km(latitude: float, longitude: float, config: PromotionConfig) -> float:
    """Match Stage 3's deterministic Western distance calculation."""

    radius_km = 6371.0088
    lat1 = math.radians(latitude)
    lat2 = math.radians(config.campus_latitude)
    delta_lat = math.radians(config.campus_latitude - latitude)
    delta_lon = math.radians(config.campus_longitude - longitude)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return round(radius_km * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value)), 3)


def write_promotion_artifacts(plan: PromotionPlan, output_dir: Path) -> dict[str, Path]:
    """Write local ignored evidence only; this function never writes the database."""

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "cohort": output_dir / "promotion-cohort.csv",
        "stale": output_dir / "excluded-stale.csv",
        "dependencies": output_dir / "dependency-impact.json",
        "planned_sql": output_dir / "planned-cutover.sql",
        "rollback_sql": output_dir / "rollback-plan.sql",
        "mutation": output_dir / "mutation-safety.json",
        "summary": output_dir / "summary.json",
    }
    _write_csv(paths["cohort"], [row.to_dict() for row in plan.eligible])
    _write_csv(
        paths["stale"],
        [
            {
                "property_id": row.property_id,
                "candidate_evidence_fingerprint": row.evidence_fingerprint,
                "current_evidence_fingerprint": row.current_evidence_fingerprint,
                "changed_evidence": json.dumps(row.changed_evidence, sort_keys=True),
            }
            for row in plan.stale
        ],
    )
    _write_json(paths["dependencies"], plan.dependency_impact)
    paths["planned_sql"].write_text(PLANNED_CUTOVER_SQL.strip() + "\n", encoding="utf-8", newline="\n")
    paths["rollback_sql"].write_text(PLANNED_ROLLBACK_SQL.strip() + "\n", encoding="utf-8", newline="\n")
    _write_json(
        paths["mutation"],
        {
            "dry_run_only": True,
            "fingerprints_match": plan.protected_before == plan.protected_after,
            "before": plan.protected_before,
            "after": plan.protected_after,
            "canonical_promotion_state_changed": 0,
            "routes_or_surfaces_rebuilt": 0,
        },
    )
    _write_json(
        paths["summary"],
        {
            "promotion_policy_version": plan.config.version,
            "selection_policy_version": plan.config.coordinate_selection_policy_version,
            "selection_policy_fingerprint": plan.frozen.policy_fingerprint,
            "candidate_run_id": plan.config.candidate_run_id,
            "candidate_fingerprint": plan.frozen.candidate_fingerprint,
            "expected_automatic_cohort": plan.config.expected_city_selected,
            "fresh_eligible_cohort": len(plan.eligible),
            "stale_candidate_count": len(plan.stale),
            "decision_counts": plan.frozen.decision_counts,
            "movement_audit": plan.movement_audit,
            "dependency_impact": plan.dependency_impact,
            "planned_at": plan.planned_at.isoformat(),
            "executed_against_canonical_database": False,
        },
    )
    return paths


def install_disposable_promotion_store(
    connection: Any, *, allow_disposable_test: bool = False
) -> None:
    """Install the proposed provenance store only on an explicitly approved test DB."""

    _require_disposable_database(connection, allow_disposable_test)
    connection.execute(FUTURE_PROMOTION_STORE_SQL)


def execute_disposable_promotion(
    connection: Any,
    plan: PromotionPlan,
    *,
    migration_run_id: str,
    allow_disposable_test: bool = False,
    allow_local_development: bool = False,
    before_state_fingerprint: str = ZERO_SHA256,
    backup_path: str = "disposable-test-fixture",
    backup_sha256: str = ZERO_SHA256,
    project_root: Path = PROJECT_ROOT,
    fail_after_property_update: bool = False,
) -> dict[str, int]:
    """Execute the guarded set-based cutover on a test or approved local DB."""

    _require_promotion_database(
        connection,
        allow_disposable_test=allow_disposable_test,
        allow_local_development=allow_local_development,
    )
    with connection.transaction():
        connection.execute(
            "select pg_advisory_xact_lock(hashtext('coordinate-promotion-v1'))"
        )
        if allow_local_development:
            plan = build_promotion_plan(
                connection,
                plan.config,
                project_root=project_root,
                planned_at=datetime.now(timezone.utc),
            )
            if (
                len(plan.eligible) != plan.config.expected_city_selected
                or plan.stale
                or plan.movement_audit["buckets"][">100"] != 0
            ):
                raise RuntimeError(
                    "final frozen-candidate validation failed inside cutover transaction"
                )
            active_runs = int(
                connection.execute(
                    """
                    select count(*) from public.housing_coordinate_promotion_runs
                    where status in ('executing','cutover_completed','recomputing')
                    """
                ).fetchone()[0]
            )
            if active_runs:
                raise RuntimeError("another coordinate promotion run is active")
            if (
                before_state_fingerprint == ZERO_SHA256
                or backup_sha256 == ZERO_SHA256
                or not backup_path.strip()
            ):
                raise RuntimeError(
                    "verified backup and before-state fingerprints are required"
                )
        payload = json.dumps([row.to_dict() for row in plan.eligible], sort_keys=True)
        connection.execute(
            """
            create temporary table coordinate_promotion_cohort on commit drop as
            select * from jsonb_to_recordset(%s::jsonb) as value (
                property_id bigint, display_address text,
                previous_latitude double precision,
                previous_longitude double precision, previous_source text,
                previous_status text, previous_confidence double precision,
                previous_geocode_result_id bigint,
                new_latitude double precision, new_longitude double precision,
                new_distance_to_western_km double precision,
                movement_meters double precision, city_dataset_run_id bigint,
                city_dataset_fingerprint text, municipal_address_id bigint,
                building_id bigint, parcel_id bigint, city_match_method text,
                city_match_confidence double precision, selection_reason text,
                candidate_evidence_fingerprint text,
                active_listing_count integer, latest_observation_count integer,
                accessibility_profile_count integer, walking_profile_count integer,
                cycling_profile_count integer, transit_profile_count integer,
                cached_exact_route_count integer, walking_surface_count integer,
                current_ranking_row_count integer
            )
            """,
            (payload,),
        )
        actual_count = int(
            connection.execute(
                "select count(*) from coordinate_promotion_cohort"
            ).fetchone()[0]
        )
        if actual_count != len(plan.eligible):
            raise RuntimeError("staged promotion cohort count mismatch")
        if actual_count == 0:
            raise RuntimeError("refusing an empty promotion cohort")
        locked = len(
            connection.execute(
                """
                select property.id from public.housing_properties property
                join coordinate_promotion_cohort cohort on cohort.property_id=property.id
                where property.latitude=cohort.previous_latitude
                  and property.longitude=cohort.previous_longitude
                  and property.geocode_provider=cohort.previous_source
                  and property.geocode_status=cohort.previous_status
                  and property.geocode_result_id is not distinct from
                      cohort.previous_geocode_result_id
                for update of property
                """
            ).fetchall()
        )
        if locked != actual_count:
            raise RuntimeError("property state drifted after promotion planning")

        promotion_id = int(
            connection.execute(
                """
                insert into public.housing_coordinate_promotion_runs (
                    run_id,promotion_policy_version,
                    coordinate_selection_policy_version,
                    coordinate_selection_policy_fingerprint,candidate_run_id,
                    candidate_fingerprint,status,expected_property_count,
                    eligible_property_count,stale_property_count,
                    before_state_fingerprint,backup_path,backup_sha256,
                    summary,started_at
                ) values (%s,%s,%s,%s,%s,%s,'executing',%s,%s,%s,%s,%s,%s,
                          %s::jsonb,now())
                returning id
                """,
                (
                    migration_run_id,
                    plan.config.version,
                    plan.config.coordinate_selection_policy_version,
                    plan.config.coordinate_selection_policy_fingerprint,
                    plan.config.candidate_run_id,
                    plan.config.candidate_fingerprint,
                    plan.config.expected_city_selected,
                    len(plan.eligible),
                    len(plan.stale),
                    before_state_fingerprint,
                    backup_path,
                    backup_sha256,
                    json.dumps(
                        {
                            "dry_run_fixture": allow_disposable_test,
                            "cutover_only": allow_local_development,
                        }
                    ),
                ),
            ).fetchone()[0]
        )
        connection.execute(
            """
            insert into public.housing_coordinate_promotion_items (
                promotion_run_id,property_id,candidate_evidence_fingerprint,
                previous_property_state,previous_map_projections,
                previous_visibility_state,new_coordinate,
                movement_meters,city_dataset_run_id,city_dataset_fingerprint,
                municipal_address_id,building_id,parcel_id,promotion_reason,promoted_at
            )
            select %s,cohort.property_id,cohort.candidate_evidence_fingerprint,
                   jsonb_build_object(
                     'latitude',property.latitude,'longitude',property.longitude,
                     'geocode_provider',property.geocode_provider,
                     'geocode_status',property.geocode_status,
                     'geocode_confidence',property.geocode_confidence,
                     'geocode_result_id',property.geocode_result_id
                   ),
                   coalesce(projection.values,'[]'::jsonb),
                   visibility.value,
                   jsonb_build_object(
                     'latitude',cohort.new_latitude,'longitude',cohort.new_longitude,
                     'source','city_of_london',
                     'distance_to_western_km',cohort.new_distance_to_western_km
                   ),
                   cohort.movement_meters,cohort.city_dataset_run_id,
                   cohort.city_dataset_fingerprint,cohort.municipal_address_id,
                   cohort.building_id,cohort.parcel_id,cohort.selection_reason,now()
            from coordinate_promotion_cohort cohort
            join public.housing_properties property on property.id=cohort.property_id
            left join lateral (
              select jsonb_agg(jsonb_build_object(
                'id',observation.id,'latitude',observation.latitude,
                'longitude',observation.longitude,
                'distance_to_western_km',observation.distance_to_western_km,
                'map_ready',observation.map_ready,
                'geocode_status',observation.geocode_status,
                'geocode_confidence',observation.geocode_confidence,
                'geocode_quality_issue',observation.geocode_quality_issue,
                'provenance_data',observation.provenance_data
              ) order by observation.id) as values
              from public.housing_listings listing
              join lateral (
                select candidate.* from public.housing_listing_observations candidate
                where candidate.listing_id=listing.id
                order by candidate.observed_at desc,candidate.id desc limit 1
              ) observation on true
              where listing.property_id=cohort.property_id
                and listing.status=any(%s)
            ) projection on true
            left join lateral (
              select jsonb_build_object(
                'id',value.id,'policy_version',value.policy_version,
                'location_status',value.location_status,
                'map_visible',value.map_visible,
                'route_available',value.route_available,
                'reason_codes',value.reason_codes,
                'source_run_id',value.source_run_id,
                'source_fingerprint',value.source_fingerprint
              ) as value
              from public.housing_property_location_visibility value
              where value.property_id=cohort.property_id and value.is_current
            ) visibility on true
            """,
            (promotion_id, list(ACTIVE_LISTING_STATUSES)),
        )
        changed_properties = connection.execute(
            """
            update public.housing_properties property set
              latitude=cohort.new_latitude,longitude=cohort.new_longitude,
              geocode_provider='city_of_london',geocode_status='ok',
              geocode_confidence=cohort.city_match_confidence,
              geocode_result_id=null,updated_at=now()
            from coordinate_promotion_cohort cohort
            where property.id=cohort.property_id
            """
        ).rowcount
        if changed_properties != actual_count:
            raise RuntimeError("canonical property update count mismatch")
        if fail_after_property_update:
            raise RuntimeError("injected failure after canonical property update")

        changed_observations = connection.execute(
            """
            with latest as (
              select distinct on (listing.id) observation.id,listing.property_id
              from public.housing_listings listing
              join public.housing_listing_observations observation
                on observation.listing_id=listing.id
              join coordinate_promotion_cohort cohort
                on cohort.property_id=listing.property_id
              where listing.status=any(%s)
              order by listing.id,observation.observed_at desc,observation.id desc
            )
            update public.housing_listing_observations observation set
              latitude=cohort.new_latitude,longitude=cohort.new_longitude,
              distance_to_western_km=cohort.new_distance_to_western_km,
              map_ready=true,geocode_status='ok',
              geocode_confidence=cohort.city_match_confidence,
              geocode_quality_issue=null,
              provenance_data=observation.provenance_data || jsonb_build_object(
                'coordinate_promotion',jsonb_build_object(
                  'migration_run_id',%s::text,'promotion_policy_version',%s::text,
                  'candidate_run_id',%s::text,'candidate_fingerprint',%s::text,
                  'source','city_of_london'
                )
              )
            from latest
            join coordinate_promotion_cohort cohort
              on cohort.property_id=latest.property_id
            where observation.id=latest.id
            """,
            (
                list(ACTIVE_LISTING_STATUSES),
                migration_run_id,
                plan.config.version,
                plan.config.candidate_run_id,
                plan.config.candidate_fingerprint,
            ),
        ).rowcount
        if changed_observations != plan.dependency_impact["latest_observation_count"]:
            raise RuntimeError("latest observation projection update count mismatch")

        invalidated = _invalidate_coordinate_dependencies(
            connection, stale_reason=f"coordinate_promotion:{migration_run_id}"
        )
        connection.execute(
            """
            update public.housing_coordinate_promotion_runs
            set status=%s,summary=summary || %s::jsonb
            where id=%s
            """,
            (
                "cutover_completed" if allow_local_development else "completed",
                json.dumps({"changed_properties": changed_properties, **invalidated}),
                promotion_id,
            ),
        )
        return {
            "properties": changed_properties,
            "observations": changed_observations,
            **invalidated,
        }


def rollback_disposable_promotion(
    connection: Any,
    *,
    promotion_run_id: str,
    rollback_run_id: str,
    allow_disposable_test: bool = False,
    allow_local_development: bool = False,
) -> dict[str, int]:
    """Execute the linked rollback on an approved test or local database."""

    _require_promotion_database(
        connection,
        allow_disposable_test=allow_disposable_test,
        allow_local_development=allow_local_development,
    )
    with connection.transaction():
        original = connection.execute(
            """
            select id,promotion_policy_version,coordinate_selection_policy_version,
                   coordinate_selection_policy_fingerprint,candidate_run_id,
                   candidate_fingerprint,expected_property_count,
                   eligible_property_count,stale_property_count,
                   before_state_fingerprint,backup_path,backup_sha256
            from public.housing_coordinate_promotion_runs
            where run_id=%s and status in (
              'cutover_completed','recomputing','validation_failed','completed'
            ) for update
            """,
            (promotion_run_id,),
        ).fetchone()
        if original is None:
            raise RuntimeError("completed promotion run not found")
        connection.execute(
            """
            create temporary table coordinate_promotion_cohort on commit drop as
            select item.property_id
            from public.housing_coordinate_promotion_items item
            where item.promotion_run_id=%s
            """,
            (original[0],),
        )
        rollback_id = int(
            connection.execute(
                """
                insert into public.housing_coordinate_promotion_runs (
                  run_id,promotion_policy_version,coordinate_selection_policy_version,
                  coordinate_selection_policy_fingerprint,candidate_run_id,
                  candidate_fingerprint,status,expected_property_count,
                  eligible_property_count,stale_property_count,
                  before_state_fingerprint,backup_path,backup_sha256,
                  rollback_of_run_id,summary,started_at
                ) values (%s,%s,%s,%s,%s,%s,'executing',%s,%s,%s,%s,%s,%s,%s,
                          '{"rollback":true}'::jsonb,now()) returning id
                """,
                (rollback_run_id, *original[1:12], original[0]),
            ).fetchone()[0]
        )
        properties = connection.execute(
            """
            update public.housing_properties property set
              latitude=(item.previous_property_state->>'latitude')::double precision,
              longitude=(item.previous_property_state->>'longitude')::double precision,
              geocode_provider=item.previous_property_state->>'geocode_provider',
              geocode_status=item.previous_property_state->>'geocode_status',
              geocode_confidence=(item.previous_property_state->>'geocode_confidence')::double precision,
              geocode_result_id=(item.previous_property_state->>'geocode_result_id')::bigint,
              updated_at=now()
            from public.housing_coordinate_promotion_items item
            where item.promotion_run_id=%s and property.id=item.property_id
            """,
            (original[0],),
        ).rowcount
        observations = connection.execute(
            """
            with previous as (
              select jsonb_array_elements(item.previous_map_projections) value
              from public.housing_coordinate_promotion_items item
              where item.promotion_run_id=%s
            )
            update public.housing_listing_observations observation set
              latitude=(previous.value->>'latitude')::double precision,
              longitude=(previous.value->>'longitude')::double precision,
              distance_to_western_km=(previous.value->>'distance_to_western_km')::double precision,
              map_ready=(previous.value->>'map_ready')::boolean,
              geocode_status=previous.value->>'geocode_status',
              geocode_confidence=(previous.value->>'geocode_confidence')::double precision,
              geocode_quality_issue=previous.value->>'geocode_quality_issue',
              provenance_data=previous.value->'provenance_data'
            from previous where observation.id=(previous.value->>'id')::bigint
            """,
            (original[0],),
        ).rowcount
        invalidated = _invalidate_coordinate_dependencies(
            connection, stale_reason=f"coordinate_promotion_rollback:{rollback_run_id}"
        )
        connection.execute(
            "update public.housing_coordinate_promotion_runs set status='rolled_back',superseded_by_run_id=%s where id=%s",
            (rollback_id, original[0]),
        )
        connection.execute(
            "update public.housing_coordinate_promotion_runs set status='rollback_completed',completed_at=now() where id=%s",
            (rollback_id,),
        )
        return {"properties": properties, "observations": observations, **invalidated}


def execute_local_promotion(
    connection: Any,
    plan: PromotionPlan,
    *,
    migration_run_id: str,
    before_state_fingerprint: str,
    backup_path: str,
    backup_sha256: str,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, int]:
    """Execute a real cutover only on the guarded loopback development DB."""

    return execute_disposable_promotion(
        connection,
        plan,
        migration_run_id=migration_run_id,
        allow_local_development=True,
        before_state_fingerprint=before_state_fingerprint,
        backup_path=backup_path,
        backup_sha256=backup_sha256,
        project_root=project_root,
    )


def validate_post_cutover(
    connection: Any,
    plan: PromotionPlan,
    snapshot: BeforeStateSnapshot,
    *,
    migration_run_id: str,
    allow_disposable_test: bool = False,
) -> dict[str, Any]:
    """Require exact cohort, atomicity, preservation, and fail-closed state."""

    _require_promotion_database(
        connection,
        allow_disposable_test=allow_disposable_test,
        allow_local_development=not allow_disposable_test,
    )
    expected = {row.property_id: row for row in plan.eligible}
    property_ids = sorted(expected)
    properties = _dict_rows(
        connection.execute(
            """
            select id,latitude,longitude,geocode_provider,geocode_status,
                   geocode_confidence,geocode_result_id
            from public.housing_properties where id=any(%s) order by id
            """,
            (property_ids,),
        )
    )
    property_mismatches = [
        int(row["id"])
        for row in properties
        if (
            float(row["latitude"]) != expected[int(row["id"])].new_latitude
            or float(row["longitude"]) != expected[int(row["id"])].new_longitude
            or row["geocode_provider"] != "city_of_london"
            or row["geocode_status"] != "ok"
            or row["geocode_result_id"] is not None
        )
    ]
    projections = _dict_rows(
        connection.execute(
            """
            with latest as (
              select distinct on (listing.id)
                     observation.id,observation.listing_id,listing.property_id,
                     observation.latitude,observation.longitude,
                     observation.distance_to_western_km,
                     observation.provenance_data
              from public.housing_listings listing
              join public.housing_listing_observations observation
                on observation.listing_id=listing.id
              where listing.property_id=any(%s) and listing.status=any(%s)
              order by listing.id,observation.observed_at desc,observation.id desc
            )
            select * from latest order by property_id,listing_id
            """,
            (property_ids, list(ACTIVE_LISTING_STATUSES)),
        )
    )
    projection_mismatches = [
        int(row["id"])
        for row in projections
        if (
            float(row["latitude"]) != expected[int(row["property_id"])].new_latitude
            or float(row["longitude"])
            != expected[int(row["property_id"])].new_longitude
            or float(row["distance_to_western_km"])
            != expected[int(row["property_id"])].new_distance_to_western_km
            or (row["provenance_data"] or {})
            .get("coordinate_promotion", {})
            .get("migration_run_id")
            != migration_run_id
        )
    ]
    previous_observations = {
        int(row["id"]): row for row in snapshot.state["all_observations"]
    }
    current_observations = _dict_rows(
        connection.execute(
            """
            select id,listing_id,property_id,latitude,longitude,
                   distance_to_western_km,map_ready,geocode_status,
                   geocode_confidence,geocode_quality_issue,raw_data,
                   provenance_data
            from public.housing_listing_observations
            where property_id=any(%s) order by id
            """,
            (property_ids,),
        )
    )
    latest_ids = {int(row["id"]) for row in projections}
    raw_changes: list[int] = []
    historical_changes: list[int] = []
    for row in current_observations:
        observation_id = int(row["id"])
        previous = previous_observations[observation_id]
        if _json_safe(row["raw_data"]) != previous["raw_data"]:
            raw_changes.append(observation_id)
        if observation_id not in latest_ids and any(
            _json_safe(row[field]) != previous[field]
            for field in (
                "latitude",
                "longitude",
                "distance_to_western_km",
                "map_ready",
                "geocode_status",
                "geocode_confidence",
                "geocode_quality_issue",
                "provenance_data",
            )
        ):
            historical_changes.append(observation_id)

    nonautomatic = [
        record
        for record in plan.frozen.records
        if record["decision"] != CITY_DECISION
    ]
    nonautomatic_ids = [int(record["property_id"]) for record in nonautomatic]
    nonautomatic_current = {
        int(row[0]): row
        for row in connection.execute(
            """
            select id,latitude,longitude,geocode_provider
            from public.housing_properties where id=any(%s)
            """,
            (nonautomatic_ids,),
        ).fetchall()
    }
    excluded_changes = []
    for record in nonautomatic:
        row = nonautomatic_current.get(int(record["property_id"]))
        if row is None:
            excluded_changes.append(int(record["property_id"]))
            continue
        candidate_coordinate = (
            _float(record["current_latitude"], 8),
            _float(record["current_longitude"], 8),
            _text(record["current_source"]),
        )
        current_coordinate = (
            _float(row[1], 8),
            _float(row[2], 8),
            _text(row[3]),
        )
        if candidate_coordinate != current_coordinate:
            excluded_changes.append(int(record["property_id"]))

    fail_closed = connection.execute(
        """
        select
          (select count(*) from public.housing_accessibility_profiles profile
           where profile.origin_property_id=any(%s) and not profile.is_stale),
          (select count(*) from public.housing_walk_time_surfaces surface
           where surface.property_id=any(%s)
             and surface.status in ('ready','computing')),
          (select count(*) from public.housing_listing_scores score
           join public.housing_listings listing on listing.id=score.listing_id
           where listing.property_id=any(%s) and score.is_current),
          (select count(*) from public.housing_property_location_visibility value
           where value.property_id=any(%s) and value.is_current)
        """,
        (property_ids, property_ids, property_ids, property_ids),
    ).fetchone()
    run = connection.execute(
        """
        select status,eligible_property_count,stale_property_count,
               coordinate_selection_policy_fingerprint,candidate_fingerprint,
               before_state_fingerprint
        from public.housing_coordinate_promotion_runs where run_id=%s
        """,
        (migration_run_id,),
    ).fetchone()
    result = {
        "properties_promoted": len(properties),
        "latest_projections_updated": len(projections),
        "property_mismatches": property_mismatches,
        "projection_mismatches": projection_mismatches,
        "historical_observation_changes": historical_changes,
        "raw_data_changes": raw_changes,
        "excluded_property_changes": excluded_changes,
        "fail_closed_current_counts": {
            "accessibility": int(fail_closed[0]),
            "surfaces": int(fail_closed[1]),
            "ranking": int(fail_closed[2]),
            "visibility": int(fail_closed[3]),
        },
        "run_status": run[0] if run else None,
        "recorded_policy_fingerprint": str(run[3]).strip() if run else None,
        "recorded_candidate_fingerprint": str(run[4]).strip() if run else None,
        "recorded_before_state_fingerprint": str(run[5]).strip() if run else None,
    }
    blockers = (
        len(properties) != plan.config.expected_city_selected
        or len(projections) != plan.dependency_impact["latest_observation_count"]
        or property_mismatches
        or projection_mismatches
        or historical_changes
        or raw_changes
        or excluded_changes
        or any(int(value) for value in fail_closed)
        or run is None
        or run[0]
        != ("completed" if allow_disposable_test else "cutover_completed")
        or int(run[1]) != plan.config.expected_city_selected
        or int(run[2]) != 0
        or str(run[3]).strip()
        != plan.config.coordinate_selection_policy_fingerprint
        or str(run[4]).strip() != plan.config.candidate_fingerprint
        or str(run[5]).strip() != snapshot.state_fingerprint
    )
    if blockers:
        raise RuntimeError(
            "post-cutover atomicity or fail-closed validation failed: "
            + json.dumps(result, sort_keys=True)
        )
    return result


def set_local_promotion_status(
    connection: Any,
    migration_run_id: str,
    status: str,
    *,
    summary: dict[str, Any] | None = None,
) -> None:
    """Advance a local run to recomputing or validation_failed."""

    _require_promotion_database(
        connection,
        allow_disposable_test=False,
        allow_local_development=True,
    )
    if status not in {"recomputing", "validation_failed"}:
        raise ValueError("invalid intermediate promotion status")
    with connection.transaction():
        updated = connection.execute(
            """
            update public.housing_coordinate_promotion_runs
            set status=%s,summary=summary || %s::jsonb
            where run_id=%s and status in ('cutover_completed','recomputing')
            """,
            (status, json.dumps(summary or {}, sort_keys=True), migration_run_id),
        ).rowcount
        if updated != 1:
            raise RuntimeError("promotion run cannot enter requested status")


def reevaluate_promoted_location_visibility(
    connection: Any,
    config: PromotionConfig,
    *,
    migration_run_id: str,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Apply the frozen selector's projected visibility to promoted rows last."""

    _require_promotion_database(
        connection,
        allow_disposable_test=False,
        allow_local_development=True,
    )
    frozen = load_frozen_candidate(config, project_root=project_root)
    projected = {
        int(record["property_id"]): record["projected_location_status"]
        for record in frozen.selected
    }
    transition_counts: dict[str, int] = {}
    inserted = 0
    source_fingerprint = _json_sha256(
        {
            "policy": "location-visibility-v1",
            "promotion_run_id": migration_run_id,
            "candidate_fingerprint": config.candidate_fingerprint,
        }
    )
    with connection.transaction():
        items = _dict_rows(
            connection.execute(
                """
                select item.property_id,item.previous_visibility_state
                from public.housing_coordinate_promotion_items item
                join public.housing_coordinate_promotion_runs run
                  on run.id=item.promotion_run_id
                where run.run_id=%s and run.status='recomputing'
                order by item.property_id for update of item
                """,
                (migration_run_id,),
            )
        )
        if len(items) != config.expected_city_selected:
            raise RuntimeError("visibility cohort count mismatch")
        for item in items:
            property_id = int(item["property_id"])
            previous = item["previous_visibility_state"] or {}
            before_status = str(previous.get("location_status") or "unavailable")
            after_status = projected[property_id]
            remaining_reasons = sorted(
                set(previous.get("reason_codes") or ())
                - {
                    "city_reference_disagreement",
                    "city_reference_minor_offset",
                    "city_reference_offset",
                }
            )
            if after_status == "available":
                remaining_reasons = []
            elif not remaining_reasons:
                remaining_reasons = ["not_assessed"]
            flags = {
                "available": (True, True),
                "limited": (True, False),
                "unavailable": (False, False),
            }[after_status]
            connection.execute(
                """
                insert into public.housing_property_location_visibility (
                  property_id,policy_version,location_status,map_visible,
                  route_available,reason_codes,source_run_id,
                  source_fingerprint,is_current,assessed_at
                ) values (%s,'location-visibility-v1',%s,%s,%s,%s::jsonb,%s,%s,true,now())
                """,
                (
                    property_id,
                    after_status,
                    flags[0],
                    flags[1],
                    json.dumps(remaining_reasons),
                    migration_run_id,
                    source_fingerprint,
                ),
            )
            inserted += 1
            key = f"{before_status}_to_{after_status}"
            transition_counts[key] = transition_counts.get(key, 0) + 1
    return {
        "inserted": inserted,
        "source_fingerprint": source_fingerprint,
        "transitions": dict(sorted(transition_counts.items())),
    }


def promotion_state_fingerprint(connection: Any, migration_run_id: str) -> str:
    """Fingerprint the finalized promotion and all protected product state."""

    protected = critical_table_fingerprints(connection)
    run = connection.execute(
        """
        select run_id,status,eligible_property_count,stale_property_count,
               before_state_fingerprint,backup_sha256,
               coalesce(recomputation_summary,'{}'::jsonb),
               coalesce(validation_summary,'{}'::jsonb)
        from public.housing_coordinate_promotion_runs where run_id=%s
        """,
        (migration_run_id,),
    ).fetchone()
    items = connection.execute(
        """
        select property_id,candidate_evidence_fingerprint,new_coordinate,
               movement_meters,city_dataset_fingerprint
        from public.housing_coordinate_promotion_items item
        join public.housing_coordinate_promotion_runs run
          on run.id=item.promotion_run_id
        where run.run_id=%s order by property_id
        """,
        (migration_run_id,),
    ).fetchall()
    return _json_sha256(_json_safe({"protected": protected, "run": run, "items": items}))


def finalize_local_promotion(
    connection: Any,
    migration_run_id: str,
    *,
    recomputation_summary: dict[str, Any],
    validation_summary: dict[str, Any],
    allow_disposable_test: bool = False,
) -> str:
    """Mark a fully validated local promotion completed and return its fingerprint."""

    _require_promotion_database(
        connection,
        allow_disposable_test=allow_disposable_test,
        allow_local_development=not allow_disposable_test,
    )
    with connection.transaction():
        updated = connection.execute(
            """
            update public.housing_coordinate_promotion_runs set
              status='completed',final_state_fingerprint=null,
              recomputation_summary=%s::jsonb,validation_summary=%s::jsonb,
              completed_at=now()
            where run_id=%s and status='recomputing'
            """,
            (
                json.dumps(recomputation_summary, sort_keys=True),
                json.dumps(validation_summary, sort_keys=True),
                migration_run_id,
            ),
        ).rowcount
        if updated != 1:
            raise RuntimeError("promotion run is not ready for completion")
        state_fingerprint = promotion_state_fingerprint(connection, migration_run_id)
        connection.execute(
            """
            update public.housing_coordinate_promotion_runs
            set final_state_fingerprint=%s where run_id=%s
            """,
            (state_fingerprint, migration_run_id),
        )
    return state_fingerprint


def _invalidate_coordinate_dependencies(connection: Any, *, stale_reason: str) -> dict[str, int]:
    profiles = connection.execute(
        """
        update public.housing_accessibility_profiles profile set
          is_stale=true,stale_at=now(),stale_reason=%s,updated_at=now()
        from coordinate_promotion_cohort cohort
        where profile.origin_property_id=cohort.property_id and not profile.is_stale
        """,
        (stale_reason,),
    ).rowcount
    surfaces = connection.execute(
        """
        update public.housing_walk_time_surfaces surface set
          status='failed',error_code='origin_coordinate_changed',
          error_detail=%s,computed_at=null,duration_ms=null,
          reachable_count=null,unavailable_count=null,raw_length=null,
          compressed_length=null,compressed_payload=null,
          destination_validity_mask=null,etag=null
        from coordinate_promotion_cohort cohort
        where surface.property_id=cohort.property_id
          and surface.status in ('ready','computing')
        """,
        (stale_reason,),
    ).rowcount
    rankings = connection.execute(
        """
        update public.housing_listing_scores score set
          is_current=false,superseded_at=now()
        from public.housing_listings listing,coordinate_promotion_cohort cohort
        where score.listing_id=listing.id and listing.property_id=cohort.property_id
          and score.is_current
        """
    ).rowcount
    visibility = connection.execute(
        """
        update public.housing_property_location_visibility visibility set
          is_current=false,superseded_at=now()
        from coordinate_promotion_cohort cohort
        where visibility.property_id=cohort.property_id and visibility.is_current
        """
    ).rowcount
    return {"accessibility": profiles, "surfaces": surfaces, "ranking": rankings, "visibility": visibility}


def _require_disposable_database(connection: Any, allowed: bool) -> None:
    _require_promotion_database(
        connection,
        allow_disposable_test=allowed,
        allow_local_development=False,
    )


def _require_promotion_database(
    connection: Any,
    *,
    allow_disposable_test: bool,
    allow_local_development: bool,
) -> None:
    if not allow_disposable_test and not allow_local_development:
        raise RuntimeError(
            "disposable coordinate-promotion execution requires an explicit "
            "test-only guard or explicit local-development guard"
        )
    if allow_disposable_test and allow_local_development:
        raise RuntimeError("coordinate-promotion database guards are mutually exclusive")
    name = str(connection.execute("select current_database()").fetchone()[0]).casefold()
    host = str(getattr(connection.info, "host", "")).casefold()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("coordinate-promotion execution requires a loopback database")
    if allow_disposable_test and "test" not in name:
        raise RuntimeError("coordinate-promotion executor refuses a non-test database")
    if allow_local_development and name != "uwo_housing_dev":
        raise RuntimeError(
            "real coordinate-promotion executor requires uwo_housing_dev"
        )


def _promotion_row(
    candidate: dict[str, str], row: dict[str, Any], config: PromotionConfig
) -> PromotionRow:
    def count(name: str) -> int:
        return int(row.get(name) or 0)

    confidence = _float(row.get("geoapify_confidence"), 8)
    return PromotionRow(
        property_id=int(candidate["property_id"]),
        display_address=candidate["display_address"],
        previous_latitude=float(candidate["current_latitude"]),
        previous_longitude=float(candidate["current_longitude"]),
        previous_source=candidate["current_source"],
        previous_status=candidate["current_status"],
        previous_confidence=confidence,
        previous_geocode_result_id=_int(candidate["geoapify_result_id"]),
        new_latitude=float(candidate["city_latitude"]),
        new_longitude=float(candidate["city_longitude"]),
        new_distance_to_western_km=haversine_km(
            float(candidate["city_latitude"]), float(candidate["city_longitude"]), config
        ),
        movement_meters=float(candidate["movement_meters"]),
        city_dataset_run_id=int(candidate["city_dataset_run_id"]),
        city_dataset_fingerprint=candidate["city_dataset_fingerprint"],
        municipal_address_id=int(candidate["municipal_address_id"]),
        building_id=_int(candidate["building_id"]),
        parcel_id=_int(candidate["parcel_id"]),
        city_match_method=candidate["city_match_method"],
        city_match_confidence=float(candidate["city_match_confidence"]),
        selection_reason=candidate["selection_reason"],
        candidate_evidence_fingerprint=_json_sha256(candidate_evidence(candidate)),
        active_listing_count=count("active_listing_count"),
        latest_observation_count=count("latest_observation_count"),
        accessibility_profile_count=count("accessibility_profile_count"),
        walking_profile_count=count("walking_profile_count"),
        cycling_profile_count=count("cycling_profile_count"),
        transit_profile_count=count("transit_profile_count"),
        cached_exact_route_count=count("cached_exact_route_count"),
        walking_surface_count=count("walking_surface_count"),
        current_ranking_row_count=count("current_ranking_row_count"),
    )


def _dependency_impact(rows: Iterable[PromotionRow]) -> dict[str, int]:
    values = tuple(rows)
    fields = (
        "active_listing_count",
        "latest_observation_count",
        "accessibility_profile_count",
        "walking_profile_count",
        "cycling_profile_count",
        "transit_profile_count",
        "cached_exact_route_count",
        "walking_surface_count",
        "current_ranking_row_count",
    )
    return {"properties": len(values), **{field: sum(getattr(row, field) for row in values) for field in fields}}


def _evidence_changes(
    expected: dict[str, Any], current: dict[str, Any] | None
) -> dict[str, dict[str, Any]]:
    if current is None:
        return {"property": {"candidate": "present", "current": "missing"}}
    return {
        key: {"candidate": expected.get(key), "current": current.get(key)}
        for key in expected
        if expected.get(key) != current.get(key)
    }


def _dict_rows(result: Any) -> list[dict[str, Any]]:
    names = [column.name for column in result.description]
    return [dict(zip(names, row, strict=True)) for row in result.fetchall()]


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    value = values[lower] if lower == upper else values[lower] + (values[upper] - values[lower]) * (index - lower)
    return round(value, 2)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_sha256(value: Any) -> str:
    return _sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _int(value: Any) -> int | None:
    return int(value) if _text(value) is not None else None


def _float(value: Any, digits: int) -> float | None:
    return round(float(value), digits) if _text(value) is not None else None


def _bool(value: Any) -> bool | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"true", "1", "yes"}


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fieldnames = list(records[0]) if records else ["property_id"]
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


FUTURE_PROMOTION_STORE_SQL = r"""
create table if not exists public.housing_coordinate_promotion_runs (
    id bigint generated always as identity primary key,
    run_id text not null unique,
    promotion_policy_version text not null,
    coordinate_selection_policy_version text not null,
    coordinate_selection_policy_fingerprint char(64) not null,
    candidate_run_id text not null,
    candidate_fingerprint char(64) not null,
    status text not null check (status in (
      'executing','cutover_completed','recomputing','validation_failed',
      'completed','rolled_back','rollback_completed','failed'
    )),
    expected_property_count integer not null check (expected_property_count > 0),
    eligible_property_count integer not null check (eligible_property_count > 0),
    stale_property_count integer not null check (stale_property_count >= 0),
    before_state_fingerprint char(64) not null,
    backup_path text not null,
    backup_sha256 char(64) not null,
    final_state_fingerprint char(64),
    rollback_of_run_id bigint references public.housing_coordinate_promotion_runs(id),
    superseded_by_run_id bigint references public.housing_coordinate_promotion_runs(id),
    summary jsonb not null default '{}'::jsonb,
    recomputation_summary jsonb not null default '{}'::jsonb,
    validation_summary jsonb not null default '{}'::jsonb,
    started_at timestamptz not null,
    completed_at timestamptz,
    created_at timestamptz not null default now()
);
create table if not exists public.housing_coordinate_promotion_items (
    id bigint generated always as identity primary key,
    promotion_run_id bigint not null references public.housing_coordinate_promotion_runs(id),
    property_id bigint not null references public.housing_properties(id),
    candidate_evidence_fingerprint char(64) not null,
    previous_property_state jsonb not null,
    previous_map_projections jsonb not null,
    previous_visibility_state jsonb,
    new_coordinate jsonb not null,
    movement_meters double precision not null,
    city_dataset_run_id bigint not null,
    city_dataset_fingerprint char(64) not null,
    municipal_address_id bigint not null,
    building_id bigint,
    parcel_id bigint,
    promotion_reason text not null,
    promoted_at timestamptz not null,
    unique (promotion_run_id,property_id)
);
"""


PLANNED_CUTOVER_SQL = r"""
-- FUTURE EXECUTION PLAN ONLY. Input is a transaction-local staged cohort.
begin;
-- 1. Reverify frozen policy/candidate/evidence fingerprints and expected count.
-- 2. Lock eligible properties and their active listings/latest observations.
-- 3. Insert immutable promotion run/items with complete previous state.
-- 4. Set-based update housing_properties latitude/longitude, source/status,
--    confidence/result link and updated_at.
-- 5. Set-based update ONLY each active listing's latest observation projection:
--    latitude, longitude, distance_to_western_km, map_ready, geocode status,
--    confidence/quality and provenance_data. Never update raw_data or old rows.
-- 6. Invalidate inside the same transaction: current location visibility,
--    current accessibility profiles/exact-route caches, walking surfaces and
--    current ranking scores. Record completed cutover state.
commit;
-- After commit: targeted accessibility/routes, surfaces, ranking, then visibility LAST.
"""


PLANNED_ROLLBACK_SQL = r"""
-- FUTURE ROLLBACK PLAN ONLY. Previous state comes from immutable promotion items.
begin;
-- 1. Create rollback run linked to the completed promotion run and lock its rows.
-- 2. Restore housing_properties and the exact latest observation projections together.
-- 3. Invalidate all coordinate-derived current state again; do not restore stale claims.
-- 4. Link promotion/rollback runs immutably and commit.
commit;
-- Recompute against restored coordinates; location visibility is reassessed LAST.
"""
