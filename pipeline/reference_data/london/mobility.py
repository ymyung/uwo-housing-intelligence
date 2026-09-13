"""Dataset-specific factual normalizers for City mobility and recreation data."""
from __future__ import annotations

from typing import Any

from .datasets import DatasetSpec


PARK_AMENITY_FIELDS = (
    "GravelParking", "PavedParking", "TennisCourts", "PoolName", "PoolType",
    "PlayStructure", "OffLeashPark", "CommunityCentre", "RecreationCentre",
    "Washroom", "DrinkingFountain", "AccessibleFacilities", "CommunityGarden",
)


def normalize_mobility_attributes(
    spec: DatasetSpec, attributes: dict[str, Any]
) -> dict[str, object]:
    """Return only factual, source-backed fields for one configured dataset."""

    value = lambda key: attributes.get(spec.field_mapping.get(key, ""))
    text = lambda key: _text(value(key))
    if spec.name == "bicycle_routes":
        return {
            "gis_id": text("gis_id"), "route_name": text("route_name"),
            "from_street": text("from_street"), "to_street": text("to_street"),
            "facility_type": text("facility_type"), "directionality": text("directionality"),
            "travel_direction": text("travel_direction"), "status": text("status"),
            "left_protection": text("left_protection"), "right_protection": text("right_protection"),
            "is_private": text("is_private"), "installation_year": text("installation_year"),
        }
    if spec.name in {
        "recreation_paths_multi_use", "thames_valley_parkway", "walking_trails_unpaved",
    }:
        return {
            "gis_id": text("gis_id"), "path_name": text("path_name"),
            "path_type": text("path_type"), "source_category": text("source_category"),
            "park_category": text("park_category"), "status": text("status"),
            "source": text("source"), "asset_id": text("asset_id"),
        }
    if spec.name == "sidewalks":
        return {
            "assumed": text("assumed"), "width_text": text("width_text"),
            "material": text("material"), "beat": text("beat"),
        }
    if spec.name == "walkways":
        return {
            "gis_id": text("gis_id"), "assumed": text("assumed"),
            "from_number": text("from_number"), "from_name": text("from_name"),
            "to_number": text("to_number"), "to_name": text("to_name"),
        }
    if spec.name == "parks":
        return {
            "gis_id": text("gis_id"), "park_name": text("park_name"),
            "address": text("address"), "park_category": text("park_category"),
            "legacy_park_category": text("legacy_park_category"),
            "hectares": _number(value("hectares")), "acres": _number(value("acres")),
            "park_number": text("park_number"),
            "walking_trail_length": _number(value("walking_trail_length")),
            "paved_pathway_length": _number(value("paved_pathway_length")),
            "total_pathway_length": _number(value("total_pathway_length")),
            "amenity_data": {key: attributes[key] for key in PARK_AMENITY_FIELDS if attributes.get(key) is not None},
        }
    if spec.name == "pedestrian_crossovers":
        return {
            "gis_id": text("gis_id"), "feature_key": text("feature_key"),
            "street": text("street"), "location_text": text("location_text"),
            "year_installed": text("year_installed"), "inspection_status": text("inspection_status"),
        }
    if spec.name == "signalized_intersections":
        return {
            "gis_id": text("gis_id"), "intersection_identifier": text("intersection_identifier"),
            "cartographic_subtype": text("cartographic_subtype"),
            "signal_status": text("signal_status"), "signal_type": text("signal_type"),
        }
    raise ValueError(f"{spec.name}: no mobility normalizer is configured")


def source_last_edit_value(spec: DatasetSpec, attributes: dict[str, Any]) -> object:
    return attributes.get(spec.source_last_edit_field) if spec.source_last_edit_field else None


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
