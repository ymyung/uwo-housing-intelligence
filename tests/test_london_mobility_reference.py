"""Offline contracts for official City mobility/recreation reference data."""
from __future__ import annotations

import pytest

from pipeline.reference_data.london.datasets import DatasetSpec, load_settings
from pipeline.reference_data.london.mobility import normalize_mobility_attributes
from pipeline.reference_data.london.validation import geometry_geojson, validate_snapshot


def _spec(name: str = "bicycle_routes") -> DatasetSpec:
    return DatasetSpec(
        name=name,
        source_url="https://maps.london.ca/server/rest/services/example/MapServer/1",
        source_dataset_id="example/MapServer/1",
        geometry_type="esriGeometryPolyline",
        source_id_field="OBJECTID",
        geometry_family="line",
        table_name="london_bicycle_routes",
        category="mobility",
        source_last_edit_field="LastEditDate",
        field_mapping={
            "gis_id": "GIS_ID", "route_name": "Street", "from_street": "FromStreet",
            "to_street": "ToStreet", "facility_type": "GeneralType",
            "directionality": "UniorBidirectional", "travel_direction": "DirofTravel",
            "status": "Status", "left_protection": "LeftSideProtection",
            "right_protection": "RightSideProtection", "is_private": "Private",
            "installation_year": "InstallationYear",
        },
        required_fields=("OBJECTID", "GIS_ID", "Street", "GeneralType", "Status"),
    )


def _metadata(spec: DatasetSpec, *, srid: int = 26917) -> dict:
    names = sorted({spec.source_id_field, *spec.required_fields, *spec.field_mapping.values()})
    return {
        "geometryType": spec.geometry_type,
        "spatialReference": {"wkid": srid},
        "fields": [{"name": name} for name in names],
    }


def _feature(object_id: int = 1) -> dict:
    return {
        "attributes": {
            "OBJECTID": object_id, "GIS_ID": "BL_1", "Street": "Example Street",
            "FromStreet": "A", "ToStreet": "B", "GeneralType": "Separated",
            "UniorBidirectional": "Uni-directional", "DirofTravel": "Two-way",
            "Status": "Existing", "LeftSideProtection": "Curb",
            "RightSideProtection": None, "Private": "No", "InstallationYear": "2024",
            "LastEditDate": 1_700_000_000_000,
        },
        "geometry": {"paths": [[ [500000, 4760000], [500100, 4760100] ]]},
    }


def test_catalog_contains_only_enabled_official_mobility_and_recreation_targets() -> None:
    settings = load_settings()
    expected = {
        "bicycle_routes", "recreation_paths_multi_use", "thames_valley_parkway",
        "walking_trails_unpaved", "sidewalks", "walkways", "parks",
        "pedestrian_crossovers", "signalized_intersections",
    }
    assert expected <= set(settings.datasets)
    for name in expected:
        spec = settings.datasets[name]
        assert spec.enabled is True
        assert spec.source_url.startswith("https://maps.london.ca/")
        assert spec.source_dataset_id.endswith(tuple(str(value) for value in (2, 4, 6, 7, 8, 9, 12, 14, 20)))
        assert spec.required_fields


def test_polyline_conversion_preserves_multiple_paths_without_flattening() -> None:
    value = geometry_geojson(
        {"paths": [[[500000, 4760000], [500001, 4760001]], [[500010, 4760010], [500011, 4760011]]]},
        "esriGeometryPolyline",
    )
    assert value["type"] == "MultiLineString"
    assert len(value["coordinates"]) == 2


def test_validation_fails_closed_for_srid_bounds_invalid_geometry_and_duplicate_ids() -> None:
    spec = _spec()
    with pytest.raises(ValueError, match="EPSG"):
        validate_snapshot(spec, _metadata(spec, srid=4326), [_feature()], expected_srid=26917)
    outside = _feature()
    outside["geometry"] = {"paths": [[[1, 2], [3, 4]]]}
    with pytest.raises(ValueError, match="invalid or duplicate"):
        validate_snapshot(spec, _metadata(spec), [outside], expected_srid=26917, bounds=(460000, 4740000, 510000, 4780000))
    with pytest.raises(ValueError, match="invalid or duplicate"):
        validate_snapshot(spec, _metadata(spec), [_feature(), _feature()], expected_srid=26917)
    empty = _feature()
    empty["geometry"] = {"paths": []}
    with pytest.raises(ValueError, match="invalid or duplicate"):
        validate_snapshot(spec, _metadata(spec), [empty], expected_srid=26917)


def test_validation_fingerprint_is_deterministic_and_changes_with_source_content() -> None:
    spec = _spec()
    first = validate_snapshot(spec, _metadata(spec), [_feature()], expected_srid=26917)
    assert first.content_sha256 == validate_snapshot(spec, _metadata(spec), [_feature()], expected_srid=26917).content_sha256
    changed = _feature()
    changed["attributes"]["Status"] = "Planned"
    assert first.content_sha256 != validate_snapshot(spec, _metadata(spec), [changed], expected_srid=26917).content_sha256
    assert first.summary["raw_feature_count"] == first.summary["normalized_feature_count"] == 1
    assert first.summary["rejected_feature_count"] == 0


def test_bicycle_normalization_keeps_factual_source_categories_without_safety_inference() -> None:
    value = normalize_mobility_attributes(_spec(), _feature()["attributes"])
    assert value["facility_type"] == "Separated"
    assert value["left_protection"] == "Curb"
    assert "safety" not in value
    assert "comfort" not in value


@pytest.mark.parametrize(
    ("dataset", "expected_key"),
    [
        ("recreation_paths_multi_use", "path_type"),
        ("thames_valley_parkway", "source_category"),
        ("walking_trails_unpaved", "path_name"),
        ("sidewalks", "width_text"),
        ("walkways", "from_name"),
        ("parks", "amenity_data"),
        ("pedestrian_crossovers", "location_text"),
        ("signalized_intersections", "signal_type"),
    ],
)
def test_each_mobility_normalizer_uses_only_configured_source_fields(
    dataset: str, expected_key: str
) -> None:
    spec = load_settings().datasets[dataset]
    attributes = {field: f"source-{field}" for field in spec.required_fields}
    attributes.update({field: f"source-{field}" for field in spec.field_mapping.values()})
    if dataset == "parks":
        attributes.update({"Hectares": 4.5, "Acres": 11.1, "TennisCourts": 2})
    normalized = normalize_mobility_attributes(spec, attributes)
    assert expected_key in normalized
    assert not set(normalized).intersection({"safety_score", "walkability_score", "comfort_score"})
