from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from pipeline.reference_data.london.coordinate_selection import (
    POLICY_VERSION,
    CoordinateSelectionEvidence,
    Coordinates,
    ResearchDisposition,
    ShadowDecision,
    evaluate_coordinate_selection,
    load_coordinate_selection_policy,
)


EVALUATED_AT = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)


def _evidence(**changes: object) -> CoordinateSelectionEvidence:
    values: dict[str, object] = {
        "property_id": 41,
        "current_source": "geoapify",
        "current_status": "ok",
        "current_coordinate": Coordinates(43.0, -81.25),
        "current_city_movement_meters": 8.0,
        "geoapify_result_id": 73,
        "geoapify_provider": "geoapify",
        "geoapify_status": "ok",
        "geoapify_coordinate": Coordinates(43.0, -81.25),
        "geoapify_confidence": 0.91,
        "city_dataset_run_id": 11,
        "city_dataset_fingerprint": "a" * 64,
        "city_dataset_is_current": True,
        "city_dataset_validation_status": "passed",
        "city_dataset_import_status": "promoted",
        "municipal_address_id": 101,
        "municipal_address_status": "IA",
        "city_coordinate": Coordinates(43.00005, -81.25005),
        "city_geometry_valid": True,
        "city_geometry_srid": 26917,
        "city_match_method": "EXACT_CIVIC_MATCH",
        "city_match_confidence": 0.98,
        "city_review_required": False,
        "city_match_reason_codes": (),
        "canonical_unit_identifiers": (),
        "city_normalized_unit": None,
        "shared_municipal_property_count": 1,
        "building_id": 201,
        "building_match_method": "EXACT_CONTAINMENT",
        "building_containing_count": 1,
        "city_inside_building": True,
        "geoapify_building_distance_meters": 0.0,
        "parcel_id": 301,
        "parcel_match_method": "ADDRESS_CONTAINMENT",
        "parcel_containing_count": 1,
        "city_inside_parcel": True,
        "parcel_area_square_meters": 500.0,
        "parcel_building_count": 1,
        "parcel_municipal_address_count": 1,
        "geoapify_inside_parcel": True,
        "geoapify_parcel_distance_meters": 0.0,
        "city_geoapify_distance_meters": 8.0,
    }
    values.update(changes)
    return CoordinateSelectionEvidence(**values)  # type: ignore[arg-type]


def _evaluate(evidence: CoordinateSelectionEvidence):
    return evaluate_coordinate_selection(
        evidence,
        load_coordinate_selection_policy(),
        evaluated_at=EVALUATED_AT,
    )


def test_close_exact_city_match_is_selected_in_shadow() -> None:
    result = _evaluate(_evidence())

    assert result.decision is ShadowDecision.CITY_SELECTED_SHADOW
    assert result.research_disposition is ResearchDisposition.BOTH_AGREE
    assert result.selected_source_shadow == "city_of_london"


def test_below_movement_threshold_with_strong_city_support_is_selected() -> None:
    result = _evaluate(
        _evidence(
            city_geoapify_distance_meters=85.0,
            current_city_movement_meters=85.0,
            geoapify_parcel_distance_meters=42.0,
            geoapify_inside_parcel=False,
        )
    )

    assert result.decision is ShadowDecision.CITY_SELECTED_SHADOW
    assert result.research_disposition is ResearchDisposition.CITY_STRONGLY_SUPPORTED


def test_above_movement_threshold_requires_review_despite_perfect_city_evidence() -> None:
    result = _evaluate(
        _evidence(
            city_geoapify_distance_meters=100.01,
            current_city_movement_meters=100.01,
            geoapify_inside_parcel=False,
            geoapify_parcel_distance_meters=80.0,
            geoapify_building_distance_meters=70.0,
        )
    )

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert (
        "automatic_city_movement_threshold_exceeded"
        in result.selection_reason_codes
    )


def test_large_parcel_without_unique_building_requires_review() -> None:
    result = _evaluate(
        _evidence(
            building_id=None,
            building_match_method="NO_BUILDING",
            building_containing_count=0,
            city_inside_building=None,
            parcel_area_square_meters=7000.0,
        )
    )

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert "large_parcel_without_unique_building" in result.selection_reason_codes


def test_large_parcel_with_unique_building_can_pass_below_movement_threshold() -> None:
    result = _evaluate(
        _evidence(
            city_geoapify_distance_meters=85.0,
            current_city_movement_meters=85.0,
            parcel_area_square_meters=7000.0,
            parcel_building_count=3,
            parcel_municipal_address_count=4,
            geoapify_inside_parcel=False,
            geoapify_parcel_distance_meters=50.0,
        )
    )

    assert result.decision is ShadowDecision.CITY_SELECTED_SHADOW


def test_multi_building_parcel_without_unique_building_requires_review() -> None:
    result = _evaluate(
        _evidence(
            building_id=None,
            building_match_method="NO_BUILDING",
            building_containing_count=0,
            city_inside_building=None,
            parcel_building_count=2,
        )
    )

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert (
        "multi_building_parcel_without_unique_building"
        in result.selection_reason_codes
    )


def test_multi_address_parcel_without_unique_building_requires_review() -> None:
    result = _evaluate(
        _evidence(
            building_id=None,
            building_match_method="NO_BUILDING",
            building_containing_count=0,
            city_inside_building=None,
            parcel_municipal_address_count=2,
        )
    )

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert (
        "multi_address_parcel_without_unique_building"
        in result.selection_reason_codes
    )


def test_geoapify_inside_parcel_with_material_disagreement_requires_review() -> None:
    result = _evaluate(
        _evidence(
            city_geoapify_distance_meters=60.0,
            current_city_movement_meters=60.0,
            geoapify_inside_parcel=True,
            geoapify_parcel_distance_meters=0.0,
        )
    )

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert "geoapify_inside_matched_parcel_conflict" in result.selection_reason_codes


def test_geoapify_near_parcel_with_material_disagreement_requires_review() -> None:
    result = _evaluate(
        _evidence(
            city_geoapify_distance_meters=60.0,
            current_city_movement_meters=60.0,
            geoapify_inside_parcel=False,
            geoapify_parcel_distance_meters=24.0,
        )
    )

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert "geoapify_near_matched_parcel_conflict" in result.selection_reason_codes


def test_geoapify_far_outside_parcel_can_leave_strong_city_evidence_selected() -> None:
    result = _evaluate(
        _evidence(
            city_geoapify_distance_meters=60.0,
            current_city_movement_meters=60.0,
            geoapify_inside_parcel=False,
            geoapify_parcel_distance_meters=40.0,
        )
    )

    assert result.decision is ShadowDecision.CITY_SELECTED_SHADOW


def test_no_exact_city_match_retains_geoapify() -> None:
    result = _evaluate(
        _evidence(
            city_match_method="NO_MATCH",
            city_match_confidence=None,
            municipal_address_id=None,
            city_coordinate=None,
        )
    )

    assert result.decision is ShadowDecision.GEOAPIFY_RETAINED_SHADOW
    assert result.selected_source_shadow == "geoapify"


def test_unit_specific_property_requires_exact_unit_match() -> None:
    result = _evaluate(
        _evidence(
            canonical_unit_identifiers=("4",),
            city_normalized_unit=None,
        )
    )

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert "unit_requires_exact_unit_match" in result.selection_reason_codes
    assert result.selected_source_shadow == "geoapify"


def test_exact_matching_unit_can_pass_the_unit_gate() -> None:
    result = _evaluate(
        _evidence(
            canonical_unit_identifiers=("4",),
            city_normalized_unit="4",
            city_match_method="EXACT_UNIT_MATCH",
            city_match_confidence=1.0,
        )
    )

    assert result.decision is ShadowDecision.CITY_SELECTED_SHADOW
    assert result.unit_match_status == "exact_unit_match"


def test_shared_civic_point_is_not_promoted() -> None:
    result = _evaluate(_evidence(shared_municipal_property_count=2))

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert "shared_municipal_address_point" in result.selection_reason_codes


def test_review_required_city_match_is_not_promoted() -> None:
    result = _evaluate(_evidence(city_review_required=True))

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert "city_match_review_required" in result.selection_reason_codes


def test_invalid_city_geometry_retains_current_geoapify_coordinate() -> None:
    evidence = _evidence(city_geometry_valid=False)
    result = _evaluate(evidence)

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert result.selected_coordinate_shadow == evidence.current_coordinate
    assert result.selected_source_shadow == "geoapify"
    assert "invalid_city_geometry" in result.selection_reason_codes


def test_multiple_containing_parcels_require_review() -> None:
    result = _evaluate(_evidence(parcel_containing_count=2))

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert "parcel_not_uniquely_resolved" in result.selection_reason_codes


def test_unsupported_disagreement_never_overwrites_current_coordinate() -> None:
    evidence = _evidence(
        city_geoapify_distance_meters=65.0,
        current_city_movement_meters=65.0,
        geoapify_parcel_distance_meters=4.0,
        geoapify_building_distance_meters=3.0,
    )
    result = _evaluate(evidence)

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert result.selected_coordinate_shadow == evidence.current_coordinate
    assert result.movement_meters == 0.0


def test_stale_city_dataset_retains_current_coordinate_for_review() -> None:
    result = _evaluate(_evidence(city_dataset_is_current=False))

    assert result.decision is ShadowDecision.CONFLICT_REVIEW_SHADOW
    assert "city_dataset_not_current" in result.selection_reason_codes
    assert result.selected_source_shadow == "geoapify"


def test_policy_does_not_change_property_or_listing_identity() -> None:
    evidence = _evidence()
    result = _evaluate(evidence)

    assert result.property_id == evidence.property_id
    assert replace(evidence) == evidence
    assert not hasattr(result, "listing_id")
    assert not hasattr(result, "match_key")
    assert not hasattr(result, "routing_snap_coordinate")


def test_policy_version_and_provenance_are_emitted() -> None:
    result = _evaluate(_evidence())
    serialized = result.to_dict()

    assert result.policy_version == POLICY_VERSION
    assert serialized["policy_version"] == "coordinate-selection-v1"
    assert serialized["evaluated_at"] == EVALUATED_AT.isoformat()
    assert serialized["selection_reason_codes"] == [
        "both_agree_within_tolerance"
    ]
