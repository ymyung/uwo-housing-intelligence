"""Deterministic, shadow-only City/Geoapify coordinate selection policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import math
from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[3]
POLICY_VERSION = "coordinate-selection-v1"


class ShadowDecision(StrEnum):
    CITY_SELECTED_SHADOW = "CITY_SELECTED_SHADOW"
    GEOAPIFY_RETAINED_SHADOW = "GEOAPIFY_RETAINED_SHADOW"
    CONFLICT_REVIEW_SHADOW = "CONFLICT_REVIEW_SHADOW"
    NO_USABLE_COORDINATE_SHADOW = "NO_USABLE_COORDINATE_SHADOW"


class ResearchDisposition(StrEnum):
    BOTH_AGREE = "BOTH_AGREE"
    CITY_STRONGLY_SUPPORTED = "CITY_STRONGLY_SUPPORTED"
    CONFLICT_REVIEW = "CONFLICT_REVIEW"
    NO_EXACT_CITY_MATCH = "NO_EXACT_CITY_MATCH"


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float

    def valid(self) -> bool:
        return (
            math.isfinite(self.latitude)
            and math.isfinite(self.longitude)
            and -90 <= self.latitude <= 90
            and -180 <= self.longitude <= 180
        )

    def to_dict(self) -> dict[str, float]:
        return {"latitude": self.latitude, "longitude": self.longitude}


@dataclass(frozen=True)
class CoordinateSelectionPolicy:
    version: str
    expected_city_srid: int
    minimum_city_match_confidence: float
    near_equivalent_meters: float
    material_geometry_displacement_meters: float
    automatic_city_max_movement_meters: float
    large_parcel_area_square_meters: float
    geoapify_near_parcel_meters: float
    simple_parcel_max_municipal_address_count: int
    accepted_city_address_statuses: frozenset[str]
    accepted_city_match_methods: frozenset[str]
    accepted_dataset_validation_statuses: frozenset[str]
    accepted_dataset_import_statuses: frozenset[str]
    minimum_latitude: float
    maximum_latitude: float
    minimum_longitude: float
    maximum_longitude: float
    campus_id: str
    campus: Coordinates

    def __post_init__(self) -> None:
        if self.version != POLICY_VERSION:
            raise ValueError(f"unsupported coordinate selection version: {self.version}")
        if self.expected_city_srid <= 0:
            raise ValueError("expected_city_srid must be positive")
        if not 0 <= self.minimum_city_match_confidence <= 1:
            raise ValueError("minimum_city_match_confidence must be between 0 and 1")
        if self.near_equivalent_meters <= 0:
            raise ValueError("near_equivalent_meters must be positive")
        if self.material_geometry_displacement_meters <= 0:
            raise ValueError("material_geometry_displacement_meters must be positive")
        if self.automatic_city_max_movement_meters <= 0:
            raise ValueError("automatic_city_max_movement_meters must be positive")
        if self.large_parcel_area_square_meters <= 0:
            raise ValueError("large_parcel_area_square_meters must be positive")
        if self.geoapify_near_parcel_meters <= 0:
            raise ValueError("geoapify_near_parcel_meters must be positive")
        if self.simple_parcel_max_municipal_address_count < 1:
            raise ValueError(
                "simple_parcel_max_municipal_address_count must be at least one"
            )
        if self.minimum_latitude >= self.maximum_latitude:
            raise ValueError("latitude bounds are invalid")
        if self.minimum_longitude >= self.maximum_longitude:
            raise ValueError("longitude bounds are invalid")
        if not self.campus.valid():
            raise ValueError("campus coordinate is invalid")

    def inside_london(self, coordinates: Coordinates) -> bool:
        return (
            self.minimum_latitude <= coordinates.latitude <= self.maximum_latitude
            and self.minimum_longitude
            <= coordinates.longitude
            <= self.maximum_longitude
        )


@dataclass(frozen=True)
class CoordinateSelectionEvidence:
    property_id: int
    current_source: str | None
    current_status: str | None
    current_coordinate: Coordinates | None
    current_city_movement_meters: float | None
    geoapify_result_id: int | None
    geoapify_provider: str | None
    geoapify_status: str | None
    geoapify_coordinate: Coordinates | None
    geoapify_confidence: float | None
    city_dataset_run_id: int | None
    city_dataset_fingerprint: str | None
    city_dataset_is_current: bool
    city_dataset_validation_status: str | None
    city_dataset_import_status: str | None
    municipal_address_id: int | None
    municipal_address_status: str | None
    city_coordinate: Coordinates | None
    city_geometry_valid: bool
    city_geometry_srid: int | None
    city_match_method: str | None
    city_match_confidence: float | None
    city_review_required: bool
    city_match_reason_codes: tuple[str, ...]
    canonical_unit_identifiers: tuple[str, ...]
    city_normalized_unit: str | None
    shared_municipal_property_count: int
    building_id: int | None
    building_match_method: str | None
    building_containing_count: int
    city_inside_building: bool | None
    geoapify_building_distance_meters: float | None
    parcel_id: int | None
    parcel_match_method: str | None
    parcel_containing_count: int
    city_inside_parcel: bool | None
    parcel_area_square_meters: float | None
    parcel_building_count: int
    parcel_municipal_address_count: int
    geoapify_inside_parcel: bool | None
    geoapify_parcel_distance_meters: float | None
    city_geoapify_distance_meters: float | None


@dataclass(frozen=True)
class ShadowCoordinateSelection:
    property_id: int
    policy_version: str
    decision: ShadowDecision
    selected_source_shadow: str | None
    selected_coordinate_shadow: Coordinates | None
    movement_meters: float | None
    research_disposition: ResearchDisposition
    selection_reason: str
    selection_reason_codes: tuple[str, ...]
    unit_match_status: str
    shared_civic_point_status: str
    building_validation: str
    parcel_validation: str
    evaluated_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "property_id": self.property_id,
            "policy_version": self.policy_version,
            "decision": self.decision.value,
            "selected_source_shadow": self.selected_source_shadow,
            "selected_coordinate_shadow": (
                self.selected_coordinate_shadow.to_dict()
                if self.selected_coordinate_shadow
                else None
            ),
            "movement_meters": self.movement_meters,
            "research_disposition": self.research_disposition.value,
            "selection_reason": self.selection_reason,
            "selection_reason_codes": list(self.selection_reason_codes),
            "unit_match_status": self.unit_match_status,
            "shared_civic_point_status": self.shared_civic_point_status,
            "building_validation": self.building_validation,
            "parcel_validation": self.parcel_validation,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


def load_coordinate_selection_policy(
    path: Path | None = None,
) -> CoordinateSelectionPolicy:
    """Load and validate the committed coordinate-selection-v1 contract."""

    config_path = path or PROJECT_ROOT / "config" / "coordinate-selection-v1.toml"
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    policy = raw["policy"]
    campus = raw["campus"]
    return CoordinateSelectionPolicy(
        version=str(policy["version"]),
        expected_city_srid=int(policy["expected_city_srid"]),
        minimum_city_match_confidence=float(
            policy["minimum_city_match_confidence"]
        ),
        near_equivalent_meters=float(policy["near_equivalent_meters"]),
        material_geometry_displacement_meters=float(
            policy["material_geometry_displacement_meters"]
        ),
        automatic_city_max_movement_meters=float(
            policy["automatic_city_max_movement_meters"]
        ),
        large_parcel_area_square_meters=float(
            policy["large_parcel_area_square_meters"]
        ),
        geoapify_near_parcel_meters=float(policy["geoapify_near_parcel_meters"]),
        simple_parcel_max_municipal_address_count=int(
            policy["simple_parcel_max_municipal_address_count"]
        ),
        accepted_city_address_statuses=frozenset(
            str(value) for value in policy["accepted_city_address_statuses"]
        ),
        accepted_city_match_methods=frozenset(
            str(value) for value in policy["accepted_city_match_methods"]
        ),
        accepted_dataset_validation_statuses=frozenset(
            str(value)
            for value in policy["accepted_dataset_validation_statuses"]
        ),
        accepted_dataset_import_statuses=frozenset(
            str(value) for value in policy["accepted_dataset_import_statuses"]
        ),
        minimum_latitude=float(policy["minimum_latitude"]),
        maximum_latitude=float(policy["maximum_latitude"]),
        minimum_longitude=float(policy["minimum_longitude"]),
        maximum_longitude=float(policy["maximum_longitude"]),
        campus_id=str(campus["id"]),
        campus=Coordinates(
            latitude=float(campus["latitude"]),
            longitude=float(campus["longitude"]),
        ),
    )


def evaluate_coordinate_selection(
    evidence: CoordinateSelectionEvidence,
    policy: CoordinateSelectionPolicy,
    *,
    evaluated_at: datetime,
) -> ShadowCoordinateSelection:
    """Return an explanatory decision without mutating canonical state."""

    current_usable = (
        evidence.current_status == "ok"
        and evidence.current_coordinate is not None
        and evidence.current_coordinate.valid()
    )
    exact_city_match = evidence.city_match_method in policy.accepted_city_match_methods
    unit_status = _unit_match_status(evidence)
    shared_status = (
        "unique"
        if evidence.shared_municipal_property_count == 1
        else "shared"
        if evidence.shared_municipal_property_count > 1
        else "unavailable"
    )
    building_validation = _building_validation(evidence)
    parcel_validation = _parcel_validation(evidence)

    if not exact_city_match:
        if current_usable:
            return _decision(
                evidence,
                policy,
                evaluated_at,
                ShadowDecision.GEOAPIFY_RETAINED_SHADOW,
                evidence.current_source or "geoapify",
                evidence.current_coordinate,
                0.0,
                ResearchDisposition.NO_EXACT_CITY_MATCH,
                "no exact City address match; retain current geocode",
                ("no_exact_city_match",),
                unit_status,
                shared_status,
                building_validation,
                parcel_validation,
            )
        return _decision(
            evidence,
            policy,
            evaluated_at,
            ShadowDecision.NO_USABLE_COORDINATE_SHADOW,
            None,
            None,
            None,
            ResearchDisposition.NO_EXACT_CITY_MATCH,
            "no usable current coordinate and no exact City address match",
            ("no_usable_current_coordinate", "no_exact_city_match"),
            unit_status,
            shared_status,
            building_validation,
            parcel_validation,
        )

    blockers = _city_gate_blockers(
        evidence,
        policy,
        current_usable=current_usable,
        unit_status=unit_status,
        shared_status=shared_status,
        building_validation=building_validation,
        parcel_validation=parcel_validation,
    )
    if blockers:
        return _decision(
            evidence,
            policy,
            evaluated_at,
            (
                ShadowDecision.CONFLICT_REVIEW_SHADOW
                if current_usable
                else ShadowDecision.NO_USABLE_COORDINATE_SHADOW
            ),
            evidence.current_source if current_usable else None,
            evidence.current_coordinate if current_usable else None,
            0.0 if current_usable else None,
            ResearchDisposition.CONFLICT_REVIEW,
            "exact City evidence failed one or more promotion gates",
            blockers,
            unit_status,
            shared_status,
            building_validation,
            parcel_validation,
        )

    separation = evidence.city_geoapify_distance_meters
    assert separation is not None
    if separation <= policy.near_equivalent_meters:
        disposition = ResearchDisposition.BOTH_AGREE
        reason = "exact validated City point agrees with Geoapify"
        reason_codes = ("both_agree_within_tolerance",)
    elif _geoapify_materially_displaced(evidence, policy):
        disposition = ResearchDisposition.CITY_STRONGLY_SUPPORTED
        reason = "exact validated City point has stronger physical-geometry support"
        reason_codes = ("geoapify_materially_displaced_from_city_geometry",)
    else:
        return _decision(
            evidence,
            policy,
            evaluated_at,
            ShadowDecision.CONFLICT_REVIEW_SHADOW,
            evidence.current_source,
            evidence.current_coordinate,
            0.0,
            ResearchDisposition.CONFLICT_REVIEW,
            "City and Geoapify disagree without sufficient promotion evidence",
            ("unsupported_city_geoapify_disagreement",),
            unit_status,
            shared_status,
            building_validation,
            parcel_validation,
        )

    return _decision(
        evidence,
        policy,
        evaluated_at,
        ShadowDecision.CITY_SELECTED_SHADOW,
        "city_of_london",
        evidence.city_coordinate,
        evidence.current_city_movement_meters,
        disposition,
        reason,
        reason_codes,
        unit_status,
        shared_status,
        building_validation,
        parcel_validation,
    )


def _city_gate_blockers(
    evidence: CoordinateSelectionEvidence,
    policy: CoordinateSelectionPolicy,
    *,
    current_usable: bool,
    unit_status: str,
    shared_status: str,
    building_validation: str,
    parcel_validation: str,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if not current_usable:
        blockers.append("no_usable_current_coordinate")
    if not evidence.city_dataset_is_current:
        blockers.append("city_dataset_not_current")
    if (
        evidence.city_dataset_validation_status
        not in policy.accepted_dataset_validation_statuses
        or evidence.city_dataset_import_status
        not in policy.accepted_dataset_import_statuses
    ):
        blockers.append("city_dataset_not_accepted")
    if evidence.city_match_confidence is None or (
        evidence.city_match_confidence < policy.minimum_city_match_confidence
    ):
        blockers.append("city_match_confidence_below_threshold")
    if evidence.city_review_required:
        blockers.append("city_match_review_required")
    if evidence.city_match_reason_codes:
        blockers.append("city_match_has_unresolved_reasons")
    if unit_status == "unresolved":
        blockers.append("unit_requires_exact_unit_match")
    if shared_status != "unique":
        blockers.append("shared_municipal_address_point")
    if evidence.municipal_address_status not in policy.accepted_city_address_statuses:
        blockers.append("municipal_address_status_not_accepted")
    if (
        evidence.city_coordinate is None
        or not evidence.city_coordinate.valid()
        or not evidence.city_geometry_valid
        or evidence.city_geometry_srid != policy.expected_city_srid
    ):
        blockers.append("invalid_city_geometry")
    elif not policy.inside_london(evidence.city_coordinate):
        blockers.append("city_geometry_outside_london_bounds")
    if parcel_validation != "unique_contains":
        blockers.append("parcel_not_uniquely_resolved")
    if building_validation in {"ambiguous", "contradiction"}:
        blockers.append("building_not_uniquely_resolved")
    if (
        evidence.geoapify_status != "ok"
        or evidence.geoapify_provider != "geoapify"
        or evidence.geoapify_coordinate is None
        or not evidence.geoapify_coordinate.valid()
        or evidence.city_geoapify_distance_meters is None
    ):
        blockers.append("missing_usable_geoapify_comparison")
    movement = evidence.current_city_movement_meters
    if movement is None:
        blockers.append("missing_city_movement_comparison")
    elif movement > policy.automatic_city_max_movement_meters:
        blockers.append("automatic_city_movement_threshold_exceeded")

    unique_building = building_validation == "unique_contains"
    if (
        evidence.parcel_area_square_meters is not None
        and evidence.parcel_area_square_meters
        > policy.large_parcel_area_square_meters
        and not unique_building
    ):
        blockers.append("large_parcel_without_unique_building")
    if evidence.parcel_building_count > 1 and not unique_building:
        blockers.append("multi_building_parcel_without_unique_building")
    if (
        evidence.parcel_municipal_address_count
        > policy.simple_parcel_max_municipal_address_count
        and not unique_building
    ):
        blockers.append("multi_address_parcel_without_unique_building")

    materially_disagrees = (
        evidence.city_geoapify_distance_meters is not None
        and evidence.city_geoapify_distance_meters > policy.near_equivalent_meters
    )
    if materially_disagrees and evidence.geoapify_inside_parcel is True:
        blockers.append("geoapify_inside_matched_parcel_conflict")
    elif (
        materially_disagrees
        and evidence.geoapify_inside_parcel is False
        and evidence.geoapify_parcel_distance_meters is not None
        and evidence.geoapify_parcel_distance_meters
        <= policy.geoapify_near_parcel_meters
    ):
        blockers.append("geoapify_near_matched_parcel_conflict")
    return tuple(dict.fromkeys(blockers))


def _unit_match_status(evidence: CoordinateSelectionEvidence) -> str:
    units = {value.strip().casefold() for value in evidence.canonical_unit_identifiers if value.strip()}
    if not units:
        return "not_unit_specific"
    city_unit = str(evidence.city_normalized_unit or "").strip().casefold()
    if (
        evidence.city_match_method == "EXACT_UNIT_MATCH"
        and len(units) == 1
        and city_unit in units
    ):
        return "exact_unit_match"
    return "unresolved"


def _building_validation(evidence: CoordinateSelectionEvidence) -> str:
    if evidence.building_containing_count > 1 or evidence.building_match_method == "AMBIGUOUS":
        return "ambiguous"
    if evidence.building_id is None:
        return "none_resolved" if evidence.building_containing_count == 0 else "contradiction"
    if (
        evidence.building_match_method == "EXACT_CONTAINMENT"
        and evidence.building_containing_count == 1
        and evidence.city_inside_building is True
    ):
        return "unique_contains"
    return "contradiction"


def _parcel_validation(evidence: CoordinateSelectionEvidence) -> str:
    if (
        evidence.parcel_id is not None
        and evidence.parcel_match_method == "ADDRESS_CONTAINMENT"
        and evidence.parcel_containing_count == 1
        and evidence.city_inside_parcel is True
    ):
        return "unique_contains"
    return "ambiguous" if evidence.parcel_containing_count > 1 else "unresolved"


def _geoapify_materially_displaced(
    evidence: CoordinateSelectionEvidence,
    policy: CoordinateSelectionPolicy,
) -> bool:
    threshold = policy.material_geometry_displacement_meters
    parcel_support = (
        evidence.city_inside_parcel is True
        and evidence.geoapify_parcel_distance_meters is not None
        and evidence.geoapify_parcel_distance_meters > threshold
    )
    building_support = (
        evidence.building_id is not None
        and evidence.city_inside_building is True
        and evidence.geoapify_building_distance_meters is not None
        and evidence.geoapify_building_distance_meters > threshold
    )
    return parcel_support or building_support


def _decision(
    evidence: CoordinateSelectionEvidence,
    policy: CoordinateSelectionPolicy,
    evaluated_at: datetime,
    decision: ShadowDecision,
    source: str | None,
    coordinate: Coordinates | None,
    movement_meters: float | None,
    disposition: ResearchDisposition,
    reason: str,
    reason_codes: tuple[str, ...],
    unit_status: str,
    shared_status: str,
    building_validation: str,
    parcel_validation: str,
) -> ShadowCoordinateSelection:
    return ShadowCoordinateSelection(
        property_id=evidence.property_id,
        policy_version=policy.version,
        decision=decision,
        selected_source_shadow=source,
        selected_coordinate_shadow=coordinate,
        movement_meters=movement_meters,
        research_disposition=disposition,
        selection_reason=reason,
        selection_reason_codes=reason_codes,
        unit_match_status=unit_status,
        shared_civic_point_status=shared_status,
        building_validation=building_validation,
        parcel_validation=parcel_validation,
        evaluated_at=evaluated_at,
    )
