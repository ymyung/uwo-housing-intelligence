"""Safe, versioned City-reference snapshot storage.  No current data is deleted."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import ArcGISClient
from .datasets import DatasetSpec, LondonSettings, utc_now
from .normalization import normalize_official_address
from .mobility import normalize_mobility_attributes, source_last_edit_value
from .validation import geometry_geojson, validate_snapshot


_LEGACY_TABLES = {
    "municipal_addresses": "municipal_addresses",
    "building_footprints": "building_footprints",
    "parcels": "parcels",
}
_MOBILITY_TABLES = {
    "bicycle_routes": "london_bicycle_routes",
    "recreation_paths_multi_use": "london_recreation_paths",
    "thames_valley_parkway": "london_recreation_paths",
    "walking_trails_unpaved": "london_recreation_paths",
    "sidewalks": "london_sidewalks",
    "walkways": "london_walkways",
    "parks": "london_parks",
    "pedestrian_crossovers": "london_pedestrian_crossovers",
    "signalized_intersections": "london_signalized_intersections",
}


def _driver():
    import psycopg
    from psycopg.types.json import Jsonb
    return psycopg, Jsonb


def save_snapshot(settings: LondonSettings, spec: DatasetSpec, metadata: dict[str, Any], features: list[dict[str, Any]]) -> Path:
    validation = validate_snapshot(
        spec, metadata, features, expected_srid=settings.native_srid, bounds=settings.bounds
    )
    root = settings.snapshot_root / spec.name
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{validation.content_sha256}.json"
    payload = {"dataset": spec.name, "downloaded_at": utc_now().isoformat(), "metadata": metadata, "features": features}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=root, delete=False) as temp:
        json.dump(payload, temp, ensure_ascii=False, separators=(",", ":"))
        temporary = Path(temp.name)
    temporary.replace(destination)
    return destination


def download_snapshot(settings: LondonSettings, spec: DatasetSpec, client: ArcGISClient | None = None) -> Path:
    metadata, features = (client or ArcGISClient()).download(spec)
    return save_snapshot(settings, spec, metadata, features)


def read_snapshot(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["metadata"], payload["features"]


def import_snapshot(database_url: str, settings: LondonSettings, spec: DatasetSpec, snapshot: Path) -> dict[str, Any]:
    metadata, features = read_snapshot(snapshot)
    validation = validate_snapshot(
        spec, metadata, features, expected_srid=settings.native_srid, bounds=settings.bounds
    )
    _, Jsonb = _driver()
    downloaded_at = datetime.fromisoformat(json.loads(snapshot.read_text(encoding="utf-8"))["downloaded_at"])
    source_updated_at = _source_updated_at(metadata)
    with _driver()[0].connect(database_url) as connection:
        existing = connection.execute(
            "select id, is_current from reference_data.dataset_runs where dataset_name=%s and content_sha256=%s",
            (spec.name, validation.content_sha256),
        ).fetchone()
        if existing:
            connection.execute("update reference_data.dataset_runs set import_status='reused' where id=%s", (existing[0],))
            return {"dataset": spec.name, "run_id": existing[0], "status": "reused", "feature_count": validation.feature_count}
        with connection.transaction():
            run_id = connection.execute(
                """insert into reference_data.dataset_runs (
                     dataset_name,source_url,source_dataset_id,downloaded_at,source_updated_at,source_schema,
                     source_schema_sha256,content_sha256,feature_count,invalid_feature_count,crs_srid,
                     importer_version,validation_status,import_status,validation_summary)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'passed','staging',%s) returning id""",
                (spec.name, spec.source_url, spec.source_dataset_id, downloaded_at, source_updated_at,
                 Jsonb({"fields": metadata.get("fields", []), "geometryType": metadata.get("geometryType"), "spatialReference": _spatial_reference(metadata)}),
                 validation.schema_sha256, validation.content_sha256, validation.feature_count,
                 validation.invalid_feature_count, settings.native_srid, settings.importer_version, Jsonb(validation.summary)),
            ).fetchone()[0]
            table = _LEGACY_TABLES.get(spec.name) or _MOBILITY_TABLES.get(spec.name)
            if table is None:
                raise RuntimeError(f"{spec.name}: no reference-data table is configured")
            if spec.name in _LEGACY_TABLES:
                _import_legacy_rows(connection, Jsonb, run_id, spec, features)
            else:
                _import_mobility_rows(connection, Jsonb, run_id, spec, features)
            actual = connection.execute(f"select count(*) from reference_data.{table} where dataset_run_id=%s", (run_id,)).fetchone()[0]
            invalid_geometry = connection.execute(f"select count(*) from reference_data.{table} where dataset_run_id=%s and (geometry is null or st_isempty(geometry) or not st_isvalid(geometry))", (run_id,)).fetchone()[0]
            if actual != validation.feature_count or invalid_geometry:
                raise RuntimeError(f"{spec.name}: import verification failed (count={actual}, invalid_geometry={invalid_geometry})")
            connection.execute("update reference_data.dataset_runs set is_current=false where dataset_name=%s and is_current", (spec.name,))
            connection.execute("update reference_data.dataset_runs set is_current=true, import_status='promoted', promoted_at=now() where id=%s", (run_id,))
    return {
        "dataset": spec.name, "run_id": run_id, "status": "promoted",
        "feature_count": actual,
        "raw_feature_count": validation.feature_count,
        "normalized_feature_count": actual,
        "rejected_feature_count": validation.invalid_feature_count,
        "validation": validation.summary,
    }


def _import_legacy_rows(connection: Any, Jsonb: Any, run_id: int, spec: DatasetSpec, features: list[dict[str, Any]]) -> None:
    address_rows = []
    shape_rows = []
    for feature in features:
        attributes = feature["attributes"]
        geojson = json.dumps(geometry_geojson(feature["geometry"], spec.geometry_type), separators=(",", ":"))
        source_id = str(attributes[spec.source_id_field])
        if spec.name == "municipal_addresses":
            normalized = normalize_official_address(attributes)
            address_rows.append((run_id, source_id, attributes.get("GIS_ID"), attributes.get("MunicipalNumber"), attributes.get("MunicipalNumberQualifier"), attributes.get("StreetName"), attributes.get("StreetType"), attributes.get("StreetDirection"), attributes.get("UnitNumber"), attributes.get("FullAddress"), attributes.get("Status"), attributes.get("LastEditDate"), normalized.address, normalized.civic_address, normalized.unit, Jsonb(attributes), geojson))
        else:
            shape_rows.append((run_id, source_id, attributes.get("GIS_ID"), attributes.get("LastEditDate"), Jsonb(attributes), geojson))
    with connection.cursor() as cursor:
        if address_rows:
            cursor.executemany("""insert into reference_data.municipal_addresses
              (dataset_run_id,source_object_id,gis_id,municipal_number,municipal_number_qualifier,street_name,street_type,street_direction,unit_number,full_address,status,source_last_edit_at,normalized_address,normalized_civic_address,normalized_unit,source_attributes,geometry)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s/1000.0),%s,%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),26917))""", address_rows)
        if shape_rows and spec.name == "parcels":
            cursor.executemany("""insert into reference_data.parcels (dataset_run_id,source_object_id,gis_id,source_last_edit_at,source_attributes,geometry)
              values (%s,%s,%s,to_timestamp(%s/1000.0),%s,ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),26917)),3)))""", shape_rows)
        elif shape_rows:
            cursor.executemany("""insert into reference_data.building_footprints (dataset_run_id,source_object_id,source_last_edit_at,source_attributes,geometry)
              values (%s,%s,to_timestamp(%s/1000.0),%s,ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),26917)),3)))""", [(row[0], row[1], row[3], row[4], row[5]) for row in shape_rows])


def _import_mobility_rows(connection: Any, Jsonb: Any, run_id: int, spec: DatasetSpec, features: list[dict[str, Any]]) -> None:
    rows: list[tuple[Any, ...]] = []
    for feature in features:
        attributes = feature["attributes"]
        normalized = normalize_mobility_attributes(spec, attributes)
        common = (run_id, str(attributes[spec.source_id_field]), source_last_edit_value(spec, attributes), Jsonb(attributes), json.dumps(geometry_geojson(feature["geometry"], spec.geometry_type), separators=(",", ":")))
        rows.append(_mobility_row(spec.name, common, normalized, Jsonb))
    statement = _MOBILITY_INSERTS[spec.name]
    with connection.cursor() as cursor:
        cursor.executemany(statement, rows)


def _mobility_row(name: str, common: tuple[Any, ...], values: dict[str, object], Jsonb: Any) -> tuple[Any, ...]:
    if name == "bicycle_routes":
        return common + tuple(values[key] for key in ("gis_id", "route_name", "from_street", "to_street", "facility_type", "directionality", "travel_direction", "status", "left_protection", "right_protection", "is_private", "installation_year"))
    if name in {"recreation_paths_multi_use", "thames_valley_parkway", "walking_trails_unpaved"}:
        return common + tuple(values[key] for key in ("gis_id", "path_name", "path_type", "source_category", "park_category", "status", "source", "asset_id"))
    if name == "sidewalks":
        return common + tuple(values[key] for key in ("assumed", "width_text", "material", "beat"))
    if name == "walkways":
        return common + tuple(values[key] for key in ("gis_id", "assumed", "from_number", "from_name", "to_number", "to_name"))
    if name == "parks":
        return common + tuple(values[key] for key in ("gis_id", "park_name", "address", "park_category", "legacy_park_category", "hectares", "acres", "park_number", "walking_trail_length", "paved_pathway_length", "total_pathway_length")) + (Jsonb(values["amenity_data"]),)
    if name == "pedestrian_crossovers":
        return common + tuple(values[key] for key in ("gis_id", "feature_key", "street", "location_text", "year_installed", "inspection_status"))
    if name == "signalized_intersections":
        return common + tuple(values[key] for key in ("gis_id", "intersection_identifier", "cartographic_subtype", "signal_status", "signal_type"))
    raise RuntimeError(f"{name}: no mobility insert row is configured")


_MOBILITY_INSERTS = {
    "bicycle_routes": """insert into reference_data.london_bicycle_routes (dataset_run_id,source_object_id,source_last_edit_at,source_attributes,geometry,gis_id,route_name,from_street,to_street,facility_type,directionality,travel_direction,status,left_protection,right_protection,is_private,installation_year) values (%s,%s,to_timestamp(%s/1000.0),%s,ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),26917)),2)),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
    "recreation_paths_multi_use": """insert into reference_data.london_recreation_paths (dataset_run_id,source_object_id,source_last_edit_at,source_attributes,geometry,gis_id,path_name,path_type,source_category,park_category,status,source,asset_id) values (%s,%s,to_timestamp(%s/1000.0),%s,ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),26917)),2)),%s,%s,%s,%s,%s,%s,%s,%s)""",
    "thames_valley_parkway": """insert into reference_data.london_recreation_paths (dataset_run_id,source_object_id,source_last_edit_at,source_attributes,geometry,gis_id,path_name,path_type,source_category,park_category,status,source,asset_id) values (%s,%s,to_timestamp(%s/1000.0),%s,ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),26917)),2)),%s,%s,%s,%s,%s,%s,%s,%s)""",
    "walking_trails_unpaved": """insert into reference_data.london_recreation_paths (dataset_run_id,source_object_id,source_last_edit_at,source_attributes,geometry,gis_id,path_name,path_type,source_category,park_category,status,source,asset_id) values (%s,%s,to_timestamp(%s/1000.0),%s,ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),26917)),2)),%s,%s,%s,%s,%s,%s,%s,%s)""",
    "sidewalks": """insert into reference_data.london_sidewalks (dataset_run_id,source_object_id,source_last_edit_at,source_attributes,geometry,assumed,width_text,material,beat) values (%s,%s,to_timestamp(%s/1000.0),%s,ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),26917)),2)),%s,%s,%s,%s)""",
    "walkways": """insert into reference_data.london_walkways (dataset_run_id,source_object_id,source_last_edit_at,source_attributes,geometry,gis_id,assumed,from_number,from_name,to_number,to_name) values (%s,%s,to_timestamp(%s/1000.0),%s,ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),26917)),2)),%s,%s,%s,%s,%s,%s)""",
    "parks": """insert into reference_data.london_parks (dataset_run_id,source_object_id,source_last_edit_at,source_attributes,geometry,gis_id,park_name,address,park_category,legacy_park_category,hectares,acres,park_number,walking_trail_length,paved_pathway_length,total_pathway_length,amenity_data) values (%s,%s,to_timestamp(%s/1000.0),%s,ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),26917)),3)),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
    "pedestrian_crossovers": """insert into reference_data.london_pedestrian_crossovers (dataset_run_id,source_object_id,source_last_edit_at,source_attributes,geometry,gis_id,feature_key,street,location_text,year_installed,inspection_status) values (%s,%s,to_timestamp(%s/1000.0),%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),26917),%s,%s,%s,%s,%s,%s)""",
    "signalized_intersections": """insert into reference_data.london_signalized_intersections (dataset_run_id,source_object_id,source_last_edit_at,source_attributes,geometry,gis_id,intersection_identifier,cartographic_subtype,signal_status,signal_type) values (%s,%s,to_timestamp(%s/1000.0),%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),26917),%s,%s,%s,%s,%s)""",
}


def _source_updated_at(metadata: dict[str, Any]) -> datetime | None:
    editing_info = metadata.get("editingInfo")
    value = editing_info.get("lastEditDate") if isinstance(editing_info, dict) else None
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)


def _spatial_reference(metadata: dict[str, Any]) -> object:
    direct = metadata.get("spatialReference")
    if isinstance(direct, dict):
        return direct
    extent = metadata.get("extent")
    return extent.get("spatialReference") if isinstance(extent, dict) else None


def database_url_from_environment() -> str:
    value = os.environ.get("ACCESSIBILITY_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("Set ACCESSIBILITY_DATABASE_URL or DATABASE_URL for local reference-data import")
    return value
