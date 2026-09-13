"""Locally configured student destinations.

Only coordinates already established in this repository are active. Named
placeholders make future product configuration explicit without inventing data.
"""

from backend.domain import Coordinates, Hotspot

HOTSPOT_CATEGORIES = {
    "campus",
    "grocery",
    "shopping",
    "transit",
    "entertainment",
    "health",
    "other",
}

DEFAULT_HOTSPOTS = (
    Hotspot(
        id="western-main-campus",
        name="Western University main campus",
        category="campus",
        coordinates=Coordinates(43.0096, -81.2737),
        address="1151 Richmond Street, London, Ontario",
        is_active=True,
        display_order=10,
        source="existing_project_campus_reference",
    ),
    Hotspot(
        id="campus-buildings-pending",
        name="Selected campus buildings",
        category="campus",
        coordinates=None,
        is_active=False,
        display_order=20,
        source="coordinate_verification_required",
    ),
    Hotspot(
        id="masonville-place-pending",
        name="Masonville Place",
        category="shopping",
        coordinates=None,
        is_active=False,
        display_order=30,
        source="coordinate_verification_required",
    ),
    Hotspot(
        id="richmond-row-pending",
        name="Downtown / Richmond Row",
        category="entertainment",
        coordinates=None,
        is_active=False,
        display_order=40,
        source="coordinate_verification_required",
    ),
    Hotspot(
        id="grocery-destinations-pending",
        name="Major grocery destinations",
        category="grocery",
        coordinates=None,
        is_active=False,
        display_order=50,
        source="coordinate_verification_required",
    ),
)


def find_hotspot(hotspots: tuple[Hotspot, ...], hotspot_id: str) -> Hotspot | None:
    return next((hotspot for hotspot in hotspots if hotspot.id == hotspot_id), None)
