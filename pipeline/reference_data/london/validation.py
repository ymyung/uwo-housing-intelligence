"""Snapshot validation and ArcGIS-to-GeoJSON conversion before database writes."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from .datasets import DatasetSpec


@dataclass(frozen=True)
class SnapshotValidation:
    schema_sha256: str
    content_sha256: str
    feature_count: int
    invalid_feature_count: int
    summary: dict[str, object]


def fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_snapshot(
    spec: DatasetSpec,
    metadata: dict[str, Any],
    features: list[dict[str, Any]],
    *,
    expected_srid: int | None = None,
    bounds: tuple[float, float, float, float] | None = None,
) -> SnapshotValidation:
    fields = metadata.get("fields", [])
    names = {field.get("name") for field in fields}
    missing_fields = {
        spec.source_id_field,
        *spec.required_fields,
        *spec.field_mapping.values(),
    } - names
    if missing_fields:
        raise ValueError(f"{spec.name}: missing required source field(s) {', '.join(sorted(missing_fields))}")
    if metadata.get("geometryType") != spec.geometry_type:
        raise ValueError(f"{spec.name}: expected {spec.geometry_type}, got {metadata.get('geometryType')}")
    source_srid = _source_srid(metadata)
    if expected_srid is not None and source_srid != expected_srid:
        raise ValueError(f"{spec.name}: expected EPSG:{expected_srid}, got EPSG:{source_srid}")
    ids: set[str] = set()
    invalid = 0
    empty_geometry = duplicate_ids = invalid_geometry = 0
    for feature in features:
        source_id = feature.get("attributes", {}).get(spec.source_id_field)
        if source_id is None:
            invalid += 1
            continue
        if str(source_id) in ids:
            duplicate_ids += 1
            invalid += 1
            continue
        ids.add(str(source_id))
        if not feature.get("geometry"):
            empty_geometry += 1
            invalid += 1
            continue
        try:
            geojson = geometry_geojson(feature["geometry"], spec.geometry_type)
            if bounds is not None:
                _validate_bounds(geojson["coordinates"], bounds)
        except (KeyError, TypeError, ValueError):
            invalid_geometry += 1
            invalid += 1
    if invalid:
        raise ValueError(f"{spec.name}: {invalid} invalid or duplicate source feature(s)")
    schema = {
        "fields": fields,
        "geometryType": metadata.get("geometryType"),
        "spatialReference": _spatial_reference(metadata),
    }
    return SnapshotValidation(fingerprint(schema), fingerprint(features), len(features), invalid, {
        "field_count": len(fields),
        "raw_feature_count": len(features),
        "normalized_feature_count": len(features),
        "rejected_feature_count": 0,
        "duplicate_source_id_count": duplicate_ids,
        "empty_geometry_count": empty_geometry,
        "invalid_geometry_count": invalid_geometry,
        "duplicate_or_invalid": invalid,
        "geometry_type": spec.geometry_type,
        "geometry_family": spec.geometry_family,
        "source_srid": source_srid,
    })


def geometry_geojson(geometry: dict[str, Any], geometry_type: str) -> dict[str, object]:
    if geometry_type == "esriGeometryPoint":
        coordinates = [geometry["x"], geometry["y"]]
        _validate_coordinate(coordinates)
        return {"type": "Point", "coordinates": coordinates}
    if geometry_type == "esriGeometryPolyline":
        paths = geometry["paths"]
        if not isinstance(paths, list) or not paths:
            raise ValueError("polyline has no paths")
        for path in paths:
            if not isinstance(path, list) or len(path) < 2:
                raise ValueError("polyline path has fewer than two coordinates")
            for coordinate in path:
                _validate_coordinate(coordinate)
        return {
            "type": "LineString" if len(paths) == 1 else "MultiLineString",
            "coordinates": paths[0] if len(paths) == 1 else paths,
        }
    if geometry_type == "esriGeometryPolygon":
        rings = geometry["rings"]
        if not isinstance(rings, list) or not rings:
            raise ValueError("polygon has no rings")
        for ring in rings:
            if not isinstance(ring, list) or len(ring) < 4:
                raise ValueError("polygon ring has fewer than four coordinates")
            for coordinate in ring:
                _validate_coordinate(coordinate)
        return {"type": "Polygon", "coordinates": rings}
    raise ValueError(f"Unsupported ArcGIS geometry type: {geometry_type}")


def _validate_coordinate(coordinate: object) -> None:
    if not isinstance(coordinate, list | tuple) or len(coordinate) < 2:
        raise ValueError("invalid coordinate")
    if not all(isinstance(value, int | float) and math.isfinite(value) for value in coordinate[:2]):
        raise ValueError("non-finite coordinate")


def _source_srid(metadata: dict[str, Any]) -> int | None:
    spatial_reference = _spatial_reference(metadata)
    if not isinstance(spatial_reference, dict):
        return None
    for key in ("wkid", "latestWkid"):
        value = spatial_reference.get(key)
        if isinstance(value, int):
            return value
    return None


def _spatial_reference(metadata: dict[str, Any]) -> object:
    direct = metadata.get("spatialReference")
    if isinstance(direct, dict):
        return direct
    extent = metadata.get("extent")
    return extent.get("spatialReference") if isinstance(extent, dict) else None


def _validate_bounds(
    coordinates: object, bounds: tuple[float, float, float, float]
) -> None:
    if isinstance(coordinates, list | tuple) and len(coordinates) >= 2 and all(
        isinstance(value, int | float) for value in coordinates[:2]
    ):
        minimum_x, minimum_y, maximum_x, maximum_y = bounds
        x, y = float(coordinates[0]), float(coordinates[1])
        if not minimum_x <= x <= maximum_x or not minimum_y <= y <= maximum_y:
            raise ValueError("geometry falls outside configured London bounds")
        return
    if not isinstance(coordinates, list | tuple):
        raise ValueError("invalid coordinate nesting")
    for child in coordinates:
        _validate_bounds(child, bounds)
