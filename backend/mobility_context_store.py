"""Read-only PostGIS analysis over exact routes and City mobility versions."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from time import perf_counter
from typing import Any, Iterable

from backend.domain import Coordinates
from backend.mobility_context import (
    FACILITY_CATEGORIES,
    INCLUDED_BICYCLE_FACILITY_TYPES,
    INCLUDED_BICYCLE_STATUSES,
    INCLUDED_CITY_DATASETS,
    CityDatasetVersion,
    MobilityContextValidationError,
    route_share,
)


@dataclass(frozen=True)
class SpatialMobilityContext:
    route_meters: float
    covered_meters: float
    route_share: float
    nearest_bicycle_route_meters: float | None
    nearest_path_meters: float | None
    facility_meters: dict[str, float]
    elapsed_milliseconds: float

    def to_dict(self) -> dict[str, object]:
        return {
            "route_meters": round(self.route_meters, 2),
            "covered_meters": round(self.covered_meters, 2),
            "route_share": round(self.route_share, 6),
            "nearest_bicycle_route_meters": _rounded(
                self.nearest_bicycle_route_meters
            ),
            "nearest_path_meters": _rounded(self.nearest_path_meters),
            "facility_meters": {
                key: round(value, 2) for key, value in self.facility_meters.items()
            },
            "elapsed_milliseconds": round(self.elapsed_milliseconds, 2),
        }


def _rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def current_city_versions(connection: Any) -> tuple[CityDatasetVersion, ...]:
    rows = connection.execute(
        """select dataset_name,id,content_sha256,source_schema_sha256,feature_count
           from reference_data.dataset_runs
           where is_current and dataset_name = any(%s)
           order by dataset_name""",
        (list(INCLUDED_CITY_DATASETS),),
    ).fetchall()
    versions = tuple(
        CityDatasetVersion(
            dataset=str(row[0]),
            run_id=int(row[1]),
            content_sha256=str(row[2]),
            schema_sha256=str(row[3]),
            feature_count=int(row[4]),
        )
        for row in rows
    )
    if {version.dataset for version in versions} != set(INCLUDED_CITY_DATASETS):
        raise MobilityContextValidationError(
            "all eligible City mobility datasets must have a current version"
        )
    return versions


class PostgresMobilityContextAnalyzer:
    """Build session-local masks and run factual route spatial measurements."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._prepared_tolerances: set[int] = set()

    def prepare_masks(self, tolerances_meters: Iterable[int]) -> float:
        tolerances = sorted(set(tolerances_meters))
        if not tolerances or any(value <= 0 or value > 100 for value in tolerances):
            raise ValueError("one or more reasonable positive tolerances are required")
        current_city_versions(self.connection)
        start = perf_counter()
        self.connection.execute(
            """create temporary table if not exists mobility_context_masks (
                 tolerance_meters integer not null,
                 facility_category text not null,
                 geometry geometry(Geometry,26917) not null,
                 primary key (tolerance_meters,facility_category)
               ) on commit drop"""
        )
        missing = [value for value in tolerances if value not in self._prepared_tolerances]
        if missing:
            self.connection.execute(
                """with eligible as (
                     select lower(f.facility_type) as category,f.geometry
                     from reference_data.london_bicycle_routes f
                     join reference_data.dataset_runs r on r.id=f.dataset_run_id
                     where r.dataset_name='bicycle_routes' and r.is_current
                       and lower(coalesce(f.status,''))=any(%s)
                       and lower(coalesce(f.facility_type,''))=any(%s)
                     union all
                     select 'multi_use_path',f.geometry
                     from reference_data.london_recreation_paths f
                     join reference_data.dataset_runs r on r.id=f.dataset_run_id
                     where r.dataset_name='recreation_paths_multi_use' and r.is_current
                     union all
                     select 'thames_valley_parkway',f.geometry
                     from reference_data.london_recreation_paths f
                     join reference_data.dataset_runs r on r.id=f.dataset_run_id
                     where r.dataset_name='thames_valley_parkway' and r.is_current
                   ), tolerances as (
                     select unnest(%s::integer[]) as tolerance_meters
                   ), category_masks as (
                     select tolerance_meters,category,
                       st_unaryunion(st_collect(st_buffer(geometry,tolerance_meters))) as geometry
                     from eligible cross join tolerances group by 1,2
                   ), masks as (
                     select tolerance_meters,category,geometry from category_masks
                     union all
                     select tolerance_meters,'__all__',st_unaryunion(st_collect(geometry))
                     from category_masks group by 1
                   )
                   insert into mobility_context_masks
                     (tolerance_meters,facility_category,geometry)
                   select tolerance_meters,category,geometry from masks
                   on conflict (tolerance_meters,facility_category) do nothing""",
                (
                    list(INCLUDED_BICYCLE_STATUSES),
                    list(INCLUDED_BICYCLE_FACILITY_TYPES),
                    missing,
                ),
            )
            self._prepared_tolerances.update(missing)
        return (perf_counter() - start) * 1000

    def analyze(
        self,
        coordinates: tuple[Coordinates, ...],
        *,
        origin: Coordinates,
        tolerance_meters: int,
    ) -> SpatialMobilityContext:
        if tolerance_meters not in self._prepared_tolerances:
            raise RuntimeError("prepare_masks must include the requested tolerance")
        if len(coordinates) < 2:
            raise MobilityContextValidationError("route requires at least two points")
        route_geojson = json.dumps(
            {
                "type": "LineString",
                "coordinates": [
                    [point.longitude, point.latitude] for point in coordinates
                ],
            },
            separators=(",", ":"),
        )
        start = perf_counter()
        row = self.connection.execute(
            """with route as (
                 select st_transform(st_setsrid(st_geomfromgeojson(%s),4326),26917) geometry
               ), origin as (
                 select st_transform(st_setsrid(st_makepoint(%s,%s),4326),26917) geometry
               ), current_bicycle as (
                 select f.geometry from reference_data.london_bicycle_routes f
                 join reference_data.dataset_runs r on r.id=f.dataset_run_id
                 where r.dataset_name='bicycle_routes' and r.is_current
                   and lower(coalesce(f.status,''))=any(%s)
                   and lower(coalesce(f.facility_type,''))=any(%s)
               ), current_path as (
                 select f.geometry from reference_data.london_recreation_paths f
                 join reference_data.dataset_runs r on r.id=f.dataset_run_id
                 where r.dataset_name in ('recreation_paths_multi_use','thames_valley_parkway')
                   and r.is_current
               )
               select st_length(route.geometry),
                 st_length(st_intersection(route.geometry,mask.geometry)),
                 (select min(st_distance(origin.geometry,b.geometry)) from current_bicycle b),
                 (select min(st_distance(origin.geometry,p.geometry)) from current_path p)
               from route cross join origin
               join mobility_context_masks mask
                 on mask.tolerance_meters=%s and mask.facility_category='__all__'""",
            (
                route_geojson,
                origin.longitude,
                origin.latitude,
                list(INCLUDED_BICYCLE_STATUSES),
                list(INCLUDED_BICYCLE_FACILITY_TYPES),
                tolerance_meters,
            ),
        ).fetchone()
        if row is None:
            raise MobilityContextValidationError("eligible City infrastructure mask is missing")
        route_meters = float(row[0])
        covered_meters = float(row[1])
        ratio = route_share(covered_meters, route_meters)
        if ratio is None:
            raise MobilityContextValidationError("route overlap could not be measured")
        category_rows = self.connection.execute(
            """with route as (
                 select st_transform(st_setsrid(st_geomfromgeojson(%s),4326),26917) geometry
               )
               select mask.facility_category,
                 st_length(st_intersection(route.geometry,mask.geometry))
               from route join mobility_context_masks mask
                 on mask.tolerance_meters=%s and mask.facility_category<>'__all__'
               order by mask.facility_category""",
            (route_geojson, tolerance_meters),
        ).fetchall()
        elapsed = (perf_counter() - start) * 1000
        facility_meters = {category: 0.0 for category in FACILITY_CATEGORIES}
        facility_meters.update(
            {str(category): float(meters) for category, meters in category_rows}
        )
        return SpatialMobilityContext(
            route_meters=route_meters,
            covered_meters=covered_meters,
            route_share=ratio,
            nearest_bicycle_route_meters=(float(row[2]) if row[2] is not None else None),
            nearest_path_meters=(float(row[3]) if row[3] is not None else None),
            facility_meters=facility_meters,
            elapsed_milliseconds=elapsed,
        )

    def diagnostic_features(
        self,
        coordinates: tuple[Coordinates, ...],
        *,
        distance_meters: int = 25,
        limit: int = 500,
    ) -> dict[str, object]:
        """Return a bounded local GeoJSON overlay for manual alignment review."""

        if not 0 < distance_meters <= 100 or not 1 <= limit <= 2_000:
            raise ValueError("diagnostic distance or feature limit is invalid")
        route_geojson = json.dumps(
            {
                "type": "LineString",
                "coordinates": [
                    [point.longitude, point.latitude] for point in coordinates
                ],
            },
            separators=(",", ":"),
        )
        rows = self.connection.execute(
            """with route as (
                 select st_transform(st_setsrid(st_geomfromgeojson(%s),4326),26917) geometry
               ), eligible as (
                 select f.source_object_id,
                   lower(f.facility_type) as category,f.geometry
                 from reference_data.london_bicycle_routes f
                 join reference_data.dataset_runs r on r.id=f.dataset_run_id
                 where r.dataset_name='bicycle_routes' and r.is_current
                   and lower(coalesce(f.status,''))=any(%s)
                   and lower(coalesce(f.facility_type,''))=any(%s)
                 union all
                 select f.source_object_id,'multi_use_path',f.geometry
                 from reference_data.london_recreation_paths f
                 join reference_data.dataset_runs r on r.id=f.dataset_run_id
                 where r.dataset_name='recreation_paths_multi_use' and r.is_current
                 union all
                 select f.source_object_id,'thames_valley_parkway',f.geometry
                 from reference_data.london_recreation_paths f
                 join reference_data.dataset_runs r on r.id=f.dataset_run_id
                 where r.dataset_name='thames_valley_parkway' and r.is_current
               )
               select source_object_id,category,
                 st_asgeojson(st_transform(eligible.geometry,4326))::jsonb
               from eligible cross join route
               where st_dwithin(eligible.geometry,route.geometry,%s)
               order by category,source_object_id limit %s""",
            (
                route_geojson,
                list(INCLUDED_BICYCLE_STATUSES),
                list(INCLUDED_BICYCLE_FACILITY_TYPES),
                distance_meters,
                limit,
            ),
        ).fetchall()
        features: list[dict[str, object]] = [
            {
                "type": "Feature",
                "properties": {"kind": "otp_bicycle_route"},
                "geometry": json.loads(route_geojson),
            }
        ]
        features.extend(
            {
                "type": "Feature",
                "properties": {
                    "kind": "city_infrastructure",
                    "source_object_id": str(source_id),
                    "facility_category": str(category),
                },
                "geometry": geometry,
            }
            for source_id, category, geometry in rows
        )
        return {"type": "FeatureCollection", "features": features}


def database_url_from_environment() -> str:
    value = os.environ.get("ACCESSIBILITY_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("Set ACCESSIBILITY_DATABASE_URL for local mobility analysis")
    return value
