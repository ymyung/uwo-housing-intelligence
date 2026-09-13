"""Disposable PostGIS proof for versioned City mobility reference imports."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.reference_data.london.datasets import DatasetSpec, LondonSettings
from pipeline.reference_data.london.importer import import_snapshot, save_snapshot


pytestmark = pytest.mark.postgres


def _spec() -> DatasetSpec:
    return DatasetSpec(
        name="bicycle_routes",
        source_url="https://maps.london.ca/server/rest/services/OpenData/OpenData_Transportation/MapServer/20",
        source_dataset_id="OpenData_Transportation/MapServer/20",
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


def _metadata(spec: DatasetSpec) -> dict:
    fields = sorted({spec.source_id_field, *spec.required_fields, *spec.field_mapping.values()})
    return {
        "name": "Bicycle Routes - On Street",
        "geometryType": spec.geometry_type,
        "spatialReference": {"wkid": 26917},
        "fields": [{"name": value} for value in fields],
        "editingInfo": {"lastEditDate": 1_700_000_000_000},
    }


def _features(*, status: str = "Existing") -> list[dict]:
    return [{
        "attributes": {
            "OBJECTID": 1, "GIS_ID": "BL_0001", "Street": "Example Street",
            "FromStreet": "A", "ToStreet": "B", "GeneralType": "Separated",
            "UniorBidirectional": "Uni-directional", "DirofTravel": "Two-way",
            "Status": status, "LeftSideProtection": "Curb", "RightSideProtection": None,
            "Private": "No", "InstallationYear": "2024", "LastEditDate": 1_700_000_000_000,
        },
        "geometry": {"paths": [[[500000, 4760000], [500120, 4760120]]]},
    }]


def _settings(root: Path, spec: DatasetSpec) -> LondonSettings:
    return LondonSettings(root, root, 26917, "mobility-test-v1", {spec.name: spec})


def test_mobility_import_is_spatial_versioned_and_idempotent(
    postgres_database, postgres_target, tmp_path: Path
) -> None:
    spec = _spec()
    settings = _settings(tmp_path, spec)
    snapshot = save_snapshot(settings, spec, _metadata(spec), _features())
    first = import_snapshot(postgres_target.url, settings, spec, snapshot)
    assert first["status"] == "promoted"
    assert first["raw_feature_count"] == first["normalized_feature_count"] == 1
    row = postgres_database.execute("""select st_srid(geometry), st_geometrytype(geometry), facility_type, status, round(st_length(geometry)::numeric, 2) from reference_data.london_bicycle_routes""").fetchone()
    assert row[:4] == (26917, "ST_MultiLineString", "Separated", "Existing")
    assert float(row[4]) == 169.71
    assert import_snapshot(postgres_target.url, settings, spec, snapshot)["status"] == "reused"
    assert postgres_database.execute("select count(*) from reference_data.london_bicycle_routes").fetchone()[0] == 1
    changed = save_snapshot(settings, spec, _metadata(spec), _features(status="Planned"))
    assert import_snapshot(postgres_target.url, settings, spec, changed)["status"] == "promoted"
    assert postgres_database.execute("select count(*) from reference_data.dataset_runs where dataset_name='bicycle_routes'").fetchone()[0] == 2
    assert postgres_database.execute("select count(*) from reference_data.dataset_runs where dataset_name='bicycle_routes' and is_current").fetchone()[0] == 1
    assert postgres_database.execute("select count(*) from reference_data.london_bicycle_routes").fetchone()[0] == 2
    nearest = postgres_database.execute("""select round(st_distance(st_setsrid(st_makepoint(500010,4760010),26917), geometry)::numeric, 2) from reference_data.london_bicycle_routes f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='bicycle_routes' and r.is_current""").fetchone()[0]
    assert nearest == 0
