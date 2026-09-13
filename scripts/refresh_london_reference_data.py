"""Refresh and inspect official City of London reference-data snapshots.

This command is local-only.  It never updates housing properties, rankings,
accessibility profiles, routes, or product/API state.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from time import perf_counter
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.reference_data.london.client import ArcGISClient
from pipeline.reference_data.london.datasets import DatasetSpec, load_settings
from pipeline.reference_data.london.importer import (
    database_url_from_environment,
    download_snapshot,
    import_snapshot,
    read_snapshot,
)
from pipeline.reference_data.london.validation import validate_snapshot


def _selected(settings: Any, names: list[str], all_enabled: bool) -> list[DatasetSpec]:
    if names and all_enabled:
        raise ValueError("Use --dataset or --all-enabled, not both")
    if names:
        return [settings.datasets[name] for name in names]
    if all_enabled:
        return settings.enabled_datasets()
    return []


def _source_preview(spec: DatasetSpec, client: ArcGISClient) -> dict[str, object]:
    metadata = client.metadata(spec)
    return {
        "dataset": spec.name,
        "name": metadata.get("name"),
        "source_url": spec.source_url,
        "source_dataset_id": spec.source_dataset_id,
        "source_organization": "Corporation of the City of London",
        "geometry_type": metadata.get("geometryType"),
        "source_srid": (
            metadata.get("spatialReference")
            or (metadata.get("extent") or {}).get("spatialReference")
            or {}
        ).get("wkid"),
        "raw_feature_count": client.feature_count(spec),
        "required_fields": list(spec.required_fields),
        "refresh_policy": spec.refresh_policy,
    }


def _current_summary(database_url: str) -> dict[str, object]:
    import psycopg

    queries = {
        "bicycle_routes": """select count(*), coalesce(sum(st_length(f.geometry)), 0) from reference_data.london_bicycle_routes f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='bicycle_routes' and r.is_current""",
        "recreation_paths": """select count(*), coalesce(sum(st_length(f.geometry)), 0) from reference_data.london_recreation_paths f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name in ('recreation_paths_multi_use','thames_valley_parkway','walking_trails_unpaved') and r.is_current""",
        "sidewalks": """select count(*), coalesce(sum(st_length(f.geometry)), 0) from reference_data.london_sidewalks f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='sidewalks' and r.is_current""",
        "walkways": """select count(*), coalesce(sum(st_length(f.geometry)), 0) from reference_data.london_walkways f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='walkways' and r.is_current""",
        "parks": """select count(*), coalesce(sum(st_area(f.geometry)), 0) from reference_data.london_parks f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='parks' and r.is_current""",
        "pedestrian_crossovers": """select count(*) from reference_data.london_pedestrian_crossovers f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='pedestrian_crossovers' and r.is_current""",
        "signalized_intersections": """select count(*) from reference_data.london_signalized_intersections f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='signalized_intersections' and r.is_current""",
    }
    with psycopg.connect(database_url) as connection:
        runs = connection.execute("""select dataset_name,id,feature_count,invalid_feature_count,content_sha256,source_schema_sha256,downloaded_at,source_updated_at,validation_summary from reference_data.dataset_runs where is_current order by dataset_name""").fetchall()
        summary = {"current_runs": [dict(zip(("dataset", "run_id", "raw_feature_count", "invalid_feature_count", "content_sha256", "schema_sha256", "downloaded_at", "source_updated_at", "validation"), row)) for row in runs]}
        for name, query in queries.items():
            row = connection.execute(query).fetchone()
            summary[name] = {"feature_count": int(row[0]), "total_measure_meters": float(row[1]) if len(row) > 1 else None}
        summary["bicycle_facility_types"] = [dict(zip(("facility_type", "count"), row)) for row in connection.execute("""select coalesce(f.facility_type, 'unknown'),count(*) from reference_data.london_bicycle_routes f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='bicycle_routes' and r.is_current group by 1 order by 1""").fetchall()]
        summary["recreation_path_categories"] = [
            dict(zip(("dataset", "source_category", "count"), row))
            for row in connection.execute(
                """select r.dataset_name,coalesce(f.source_category, 'unknown'),count(*)
                from reference_data.london_recreation_paths f
                join reference_data.dataset_runs r on r.id=f.dataset_run_id
                where r.dataset_name in ('recreation_paths_multi_use','thames_valley_parkway','walking_trails_unpaved') and r.is_current
                group by 1,2 order by 1,2"""
            ).fetchall()
        ]
        summary["recreation_path_lengths"] = [
            dict(zip(("dataset", "feature_count", "total_length_meters"), row))
            for row in connection.execute(
                """select r.dataset_name,count(*),coalesce(sum(st_length(f.geometry)), 0)
                from reference_data.london_recreation_paths f
                join reference_data.dataset_runs r on r.id=f.dataset_run_id
                where r.dataset_name in ('recreation_paths_multi_use','thames_valley_parkway','walking_trails_unpaved') and r.is_current
                group by 1 order by 1"""
            ).fetchall()
        ]
        summary["park_categories"] = [dict(zip(("park_category", "count"), row)) for row in connection.execute("""select coalesce(f.park_category, 'unknown'),count(*) from reference_data.london_parks f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='parks' and r.is_current group by 1 order by 1""").fetchall()]
        summary["park_name_coverage"] = dict(
            zip(
                ("named", "unnamed"),
                connection.execute(
                    """select count(*) filter (where nullif(trim(f.park_name), '') is not null),
                    count(*) filter (where nullif(trim(f.park_name), '') is null)
                    from reference_data.london_parks f
                    join reference_data.dataset_runs r on r.id=f.dataset_run_id
                    where r.dataset_name='parks' and r.is_current"""
                ).fetchone(),
            )
        )
        summary["sidewalk_field_coverage"] = dict(zip(("width_text", "material"), connection.execute("""select count(*) filter (where f.width_text is not null),count(*) filter (where f.material is not null) from reference_data.london_sidewalks f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='sidewalks' and r.is_current""").fetchone()))
    return summary


def _property_poc(database_url: str, property_ids: list[int]) -> list[dict[str, object]]:
    import psycopg

    query = """
      with selected as (
        select p.id, st_transform(st_setsrid(st_makepoint(p.longitude,p.latitude),4326),26917) as point
        from public.housing_properties p
        where p.id = any(%s) and p.latitude is not null and p.longitude is not null
      )
      select selected.id,
        (select min(st_distance(selected.point, f.geometry)) from reference_data.london_bicycle_routes f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='bicycle_routes' and r.is_current) as nearest_bicycle_route_m,
        (select min(st_distance(selected.point, f.geometry)) from reference_data.london_recreation_paths f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name in ('recreation_paths_multi_use','thames_valley_parkway','walking_trails_unpaved') and r.is_current) as nearest_recreation_path_m,
        (select min(st_distance(selected.point, f.geometry)) from reference_data.london_parks f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='parks' and r.is_current) as nearest_park_m,
        (select coalesce(sum(st_length(f.geometry)),0) from reference_data.london_sidewalks f join reference_data.dataset_runs r on r.id=f.dataset_run_id where r.dataset_name='sidewalks' and r.is_current and st_dwithin(selected.point, f.geometry, 500)) as sidewalk_length_within_500m
      from selected order by selected.id
    """
    start = perf_counter()
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(query, (property_ids,)).fetchall()
    elapsed_ms = round((perf_counter() - start) * 1000, 2)
    return [
        {
            "property_id": row[0],
            "nearest_bicycle_route_m": _rounded(row[1]),
            "nearest_recreation_path_m": _rounded(row[2]),
            "nearest_park_m": _rounded(row[3]),
            "sidewalk_length_within_500m": _rounded(row[4]),
            "query_elapsed_ms": elapsed_ms,
        }
        for row in rows
    ]


def _rounded(value: object) -> float | None:
    return round(float(value), 2) if value is not None else None


def main(argv: list[str] | None = None) -> None:
    settings = load_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("refresh", "download", "validate", "import", "status", "poc"))
    parser.add_argument("--dataset", action="append", choices=sorted(settings.datasets))
    parser.add_argument("--all-enabled", action="store_true")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--property-id", type=int, action="append", default=[])
    args = parser.parse_args(argv)

    if args.command == "poc":
        if not args.property_id:
            parser.error("poc requires one or more --property-id values")
        print(json.dumps(_property_poc(database_url_from_environment(), args.property_id), indent=2))
        return
    if args.command == "status":
        print(json.dumps(_current_summary(database_url_from_environment()), indent=2, default=str))
        return

    selected = _selected(settings, args.dataset or [], args.all_enabled)
    if not selected:
        parser.error("select at least one --dataset or use --all-enabled")
    client = ArcGISClient()
    if args.dry_run:
        print(json.dumps([_source_preview(spec, client) for spec in selected], indent=2))
        return
    if args.command == "validate":
        if not args.snapshot or len(selected) != 1:
            parser.error("validate requires exactly one --dataset and --snapshot")
        metadata, features = read_snapshot(args.snapshot)
        validation = validate_snapshot(selected[0], metadata, features, expected_srid=settings.native_srid, bounds=settings.bounds)
        print(json.dumps(validation.summary | {"content_sha256": validation.content_sha256, "schema_sha256": validation.schema_sha256}, indent=2))
        return
    if args.command == "download":
        print(json.dumps({spec.name: str(download_snapshot(settings, spec, client)) for spec in selected}, indent=2))
        return
    database_url = database_url_from_environment()
    results = []
    for spec in selected:
        snapshot = args.snapshot if args.snapshot else download_snapshot(settings, spec, client)
        results.append(import_snapshot(database_url, settings, spec, snapshot))
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
