"""Deterministic PostGIS overlap tests for shadow Mobility Context V1."""

from __future__ import annotations

import pytest

from backend.domain import Coordinates
from backend.mobility_context import MobilityContextValidationError
from backend.mobility_context_store import (
    PostgresMobilityContextAnalyzer,
    current_city_versions,
)


pytestmark = pytest.mark.postgres


def _run(connection, dataset: str, index: int) -> int:
    return connection.execute(
        """insert into reference_data.dataset_runs
             (dataset_name,source_url,source_dataset_id,downloaded_at,
              source_schema,source_schema_sha256,content_sha256,feature_count,
              crs_srid,importer_version,validation_status,import_status,is_current)
           values (%s,'https://maps.london.ca/official',%s,now(),'{}',%s,%s,1,
                   26917,'test-v1','passed','promoted',true)
           returning id""",
        (dataset, f"layer-{index}", f"{index:x}" * 64, f"{index + 3:x}" * 64),
    ).fetchone()[0]


def _line(longitude_start: float, latitude: float, longitude_end: float) -> str:
    return f"LINESTRING({longitude_start} {latitude},{longitude_end} {latitude})"


def _insert_bicycle(
    connection,
    run_id: int,
    source_id: str,
    geometry: str,
    *,
    facility_type: str = "Separated",
    status: str = "Existing",
) -> None:
    connection.execute(
        """insert into reference_data.london_bicycle_routes
             (dataset_run_id,source_object_id,source_attributes,geometry,
              facility_type,status)
           values (%s,%s,'{}',
             st_transform(st_geomfromtext(%s,4326),26917),%s,%s)""",
        (run_id, source_id, geometry, facility_type, status),
    )


def _insert_path(
    connection,
    run_id: int,
    source_id: str,
    geometry: str,
) -> None:
    connection.execute(
        """insert into reference_data.london_recreation_paths
             (dataset_run_id,source_object_id,source_attributes,geometry)
           values (%s,%s,'{}',st_transform(st_geomfromtext(%s,4326),26917))""",
        (run_id, source_id, geometry),
    )


def _points(latitude: float, start: float = -81.2500, end: float = -81.2488):
    return (Coordinates(latitude, start), Coordinates(latitude, end))


def test_postgis_overlap_is_projected_tolerance_aware_and_never_double_counts(
    postgres_database,
) -> None:
    with postgres_database.transaction():
        with pytest.raises(MobilityContextValidationError, match="all eligible"):
            current_city_versions(postgres_database)

        bicycle = _run(postgres_database, "bicycle_routes", 1)
        paths = _run(postgres_database, "recreation_paths_multi_use", 2)
        tvp = _run(postgres_database, "thames_valley_parkway", 3)
        trail = _run(postgres_database, "walking_trails_unpaved", 4)
        exact = _line(-81.2500, 43.0000, -81.2488)
        _insert_bicycle(postgres_database, bicycle, "bike-1", exact)
        _insert_path(postgres_database, paths, "path-1", exact)
        _insert_path(
            postgres_database,
            tvp,
            "tvp-parallel",
            _line(-81.2500, 43.00106, -81.2488),
        )
        _insert_path(
            postgres_database,
            trail,
            "excluded-trail",
            _line(-81.2500, 43.0020, -81.2488),
        )

        analyzer = PostgresMobilityContextAnalyzer(postgres_database)
        analyzer.prepare_masks((5, 8))
        full = analyzer.analyze(
            _points(43.0000), origin=Coordinates(43.0000, -81.2500), tolerance_meters=5
        )
        assert 90 < full.route_meters < 110
        assert full.route_share == pytest.approx(1, abs=0.001)
        assert full.covered_meters <= full.route_meters + 0.01
        assert full.facility_meters["separated"] > 90
        assert full.facility_meters["multi_use_path"] > 90
        assert full.nearest_bicycle_route_meters == pytest.approx(0, abs=0.01)

        near_miss_5 = analyzer.analyze(
            _points(43.0010), origin=Coordinates(43.0010, -81.2500), tolerance_meters=5
        )
        near_miss_8 = analyzer.analyze(
            _points(43.0010), origin=Coordinates(43.0010, -81.2500), tolerance_meters=8
        )
        assert near_miss_5.route_share == pytest.approx(0, abs=0.001)
        assert near_miss_8.route_share > 0.95

        excluded = analyzer.analyze(
            _points(43.0020), origin=Coordinates(43.0020, -81.2500), tolerance_meters=8
        )
        assert excluded.route_share == pytest.approx(0, abs=0.001)


def test_postgis_overlap_handles_zero_partial_and_excluded_bicycle_statuses(
    postgres_database,
) -> None:
    with postgres_database.transaction():
        bicycle = _run(postgres_database, "bicycle_routes", 1)
        _run(postgres_database, "recreation_paths_multi_use", 2)
        _run(postgres_database, "thames_valley_parkway", 3)
        _insert_bicycle(
            postgres_database,
            bicycle,
            "half",
            _line(-81.2500, 43.0000, -81.2494),
            facility_type="Designated",
        )
        _insert_bicycle(
            postgres_database,
            bicycle,
            "future",
            _line(-81.2500, 43.0030, -81.2488),
            status="Future",
        )
        analyzer = PostgresMobilityContextAnalyzer(postgres_database)
        analyzer.prepare_masks((5,))
        partial = analyzer.analyze(
            _points(43.0000), origin=Coordinates(43.0000, -81.2500), tolerance_meters=5
        )
        assert 0.45 < partial.route_share < 0.60
        assert partial.facility_meters["designated"] > 40

        zero = analyzer.analyze(
            _points(43.0030), origin=Coordinates(43.0030, -81.2500), tolerance_meters=5
        )
        assert zero.covered_meters == pytest.approx(0, abs=0.01)
        assert zero.route_share == pytest.approx(0, abs=0.001)
