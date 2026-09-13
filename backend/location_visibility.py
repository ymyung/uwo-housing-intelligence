"""Student-facing location visibility derived from a reviewed property decision."""

from __future__ import annotations

from typing import Any, Literal, Mapping, TypedDict


LocationStatus = Literal["available", "limited", "unavailable"]


class PublicLocation(TypedDict):
    status: LocationStatus
    map_visible: bool
    route_available: bool
    message: str


_MESSAGES: dict[LocationStatus, str] = {
    "available": "Map and route information are available.",
    "limited": "This home's approximate location is shown, but route details are unavailable.",
    "unavailable": "This listing is available in search, but its location is not shown.",
}


def _truth(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _has_coordinates(row: Mapping[str, Any]) -> bool:
    try:
        latitude = float(row.get("latitude"))
        longitude = float(row.get("longitude"))
    except (TypeError, ValueError):
        return False
    return -90 <= latitude <= 90 and -180 <= longitude <= 180


def public_location(row: Mapping[str, Any]) -> PublicLocation:
    """Return a fail-closed public contract, with fixture-compatible fallback."""

    has_coordinates = _has_coordinates(row)
    explicit_status = str(row.get("location_status") or "").strip().casefold()
    if explicit_status in _MESSAGES:
        status: LocationStatus = explicit_status  # type: ignore[assignment]
        map_visible = (
            _truth(row.get("location_map_visible")) is True and has_coordinates
        )
        route_available = (
            _truth(row.get("location_route_available")) is True
            and map_visible
            and status == "available"
        )
        if status == "available" and not route_available:
            status = "unavailable"
            map_visible = False
        elif status == "limited":
            route_available = False
        elif status == "unavailable":
            map_visible = False
            route_available = False
    elif any(
        key in row
        for key in (
            "location_status",
            "location_map_visible",
            "location_route_available",
        )
    ):
        status = "unavailable"
        map_visible = False
        route_available = False
    elif has_coordinates and _truth(row.get("map_ready")) is not False:
        # In-memory and committed CSV fixtures predate the persisted contract.
        status = "available"
        map_visible = True
        route_available = True
    elif has_coordinates:
        status = "limited"
        map_visible = True
        route_available = False
    else:
        status = "unavailable"
        map_visible = False
        route_available = False

    return {
        "status": status,
        "map_visible": map_visible,
        "route_available": route_available,
        "message": _MESSAGES[status],
    }


def route_use_prohibited(row: Mapping[str, Any]) -> bool:
    """Reject routing only when the persisted contract explicitly forbids it."""

    has_explicit_contract = any(
        key in row
        for key in (
            "location_status",
            "location_map_visible",
            "location_route_available",
        )
    )
    return has_explicit_contract and not public_location(row)["route_available"]


def apply_public_location(row: Mapping[str, Any]) -> dict[str, Any]:
    """Attach public semantics and suppress coordinates not approved for display."""

    result = dict(row)
    location = public_location(row)
    result["location"] = location
    for key in (
        "location_status",
        "location_map_visible",
        "location_route_available",
        "location_reason_codes",
    ):
        result.pop(key, None)
    if not location["map_visible"]:
        result["latitude"] = None
        result["longitude"] = None
        result["distance_to_western_km"] = None
        result["selected_hotspot_distance_meters"] = None
        result["selected_hotspot_distance_km"] = None
        result["distance_type"] = None
    return result
