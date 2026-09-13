"""FastAPI housing-discovery API.

The app is repository- and provider-neutral. Supabase is initialized lazily on
the first request, while tests and local demos inject an in-memory CSV fixture.

Run against Supabase:
  python -m uvicorn backend.main:app --reload --port 8000

Run against an offline CSV:
  $env:HOUSING_FIXTURE_CSV = "tests/fixtures/accessibility_demo/listings.csv"
  $env:ACCESSIBILITY_FIXTURE_PATH = "tests/fixtures/accessibility_demo/profiles.json"
  python -m uvicorn backend.main:app --reload --port 8000
"""

from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware

from backend.accessibility import AccessibilityResolver
from backend.accessibility_inputs import load_routing_bundle, load_worker_config
from backend.accessibility_periods import DEFAULT_TRANSIT_PERIODS
from backend.accessibility_repository import (
    AccessibilityRepository,
    FixtureAccessibilityRepository,
    InMemoryAccessibilityRepository,
    PostgresAccessibilityRepository,
)
from backend.domain import (
    AccessibilityOrigin,
    AccessibilityRequest,
    AccessibilityResultType,
    Coordinates,
    Hotspot,
    Location,
    OriginType,
    TimePeriod,
    TravelMode,
    RouteRequest,
)
from backend.hotspots import DEFAULT_HOTSPOTS, HOTSPOT_CATEGORIES, find_hotspot
from backend.gtfs_freshness import GtfsFreshnessReport, inspect_gtfs_feed
from backend.location_visibility import (
    apply_public_location,
    public_location,
    route_use_prohibited,
)
from backend.listing_history import (
    observation_freshness,
    project_listing_history,
)
from backend.providers import RoutingProvider, StraightLineEstimateProvider, TravelTimeProvider, haversine_distance_meters
from backend.routing_provider import NoRouteError, OpenTripPlannerProvider, RoutingProviderError
from backend.ranking import field_quality, listing_quality
from backend.ranking_api import project_persisted_ranking
from backend.repository import (
    AccessibilityFilterContext,
    FixtureCsvListingRepository,
    ListingQuery,
    ListingHistoryRepository,
    ListingRepository,
    NON_SUMMER_AVAILABILITY_CATEGORIES,
    PostgresListingRepository,
    QueryableListingMapRepository,
    QueryableListingRepository,
    RankingSort,
    RankingStatus,
    SortOrder,
    SupabaseListingRepository,
    SUMMER_AVAILABILITY_CATEGORIES,
    default_sort_order,
)
from backend.transportation import (
    compact_transportation_summary,
    current_profile_index,
    route_detail,
    transportation_overview,
)
from backend.travel_time_surface_repository import PostgresSurfaceRepository
from backend.travel_time_surface_service import (
    HttpR5SurfaceClient,
    SurfaceError,
    SurfaceUnavailable,
    UnsupportedSurfaceMode,
    WalkingSurfaceService,
)
from backend.travel_time_surfaces import load_surface_policy

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

RANKING_STORAGE_FIELDS = {
    "ranking_version",
    "ranking_status",
    "ranking_overall_score",
    "ranking_value_score",
    "ranking_campus_access_score",
    "ranking_transit_score",
    "ranking_amenity_score",
    "ranking_data_quality_score",
    "ranking_explanation",
    "ranking_input_fingerprint",
    "ranking_computed_at",
}


@lru_cache(maxsize=1)
def _local_transport_configuration() -> dict[str, Any]:
    """Load version metadata from local ignored routing inputs when present."""

    config_path = PROJECT_ROOT / "config" / "accessibility-worker.toml"
    try:
        config = load_worker_config(config_path, PROJECT_ROOT)
        manifest = json.loads(
            config.build_manifest_path.read_text(encoding="utf-8-sig")
        )
        gtfs = inspect_gtfs_feed(
            config.gtfs_path,
            reference_week_start=config.reference_service_week,
        )
    except (OSError, ValueError, json.JSONDecodeError, KeyError):
        return {}
    return {
        "provider": config.routing_provider,
        "provider_profile": config.provider_profile,
        "schedule_version": manifest.get("schedule_version"),
        "network_version": manifest.get("network_version"),
        "gtfs": gtfs,
    }


def _truth(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _coordinates(row: dict[str, Any]) -> Coordinates | None:
    latitude = _number(row.get("latitude"))
    longitude = _number(row.get("longitude"))
    if latitude is None or longitude is None:
        return None
    try:
        return Coordinates(latitude, longitude)
    except ValueError:
        return None


def _summer_available(row: dict[str, Any]) -> bool | None:
    explicit = _truth(row.get("summer_available"))
    if explicit is not None:
        return explicit
    category = str(row.get("availability_category") or "").strip().lower()
    if category in SUMMER_AVAILABILITY_CATEGORIES:
        return True
    if category in NON_SUMMER_AVAILABILITY_CATEGORIES:
        return False
    return None


def _amenity_search_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value or "")


def _add_destination_estimate(row: dict[str, Any], hotspot: Hotspot) -> dict[str, Any]:
    enriched = dict(row)
    origin = _coordinates(row)
    if origin is None or hotspot.coordinates is None:
        distance_meters = None
    else:
        distance_meters = haversine_distance_meters(origin, hotspot.coordinates)
    enriched["selected_hotspot_id"] = hotspot.id
    enriched["selected_hotspot_distance_meters"] = distance_meters
    enriched["selected_hotspot_distance_km"] = (
        round(distance_meters / 1000, 2) if distance_meters is not None else None
    )
    enriched["distance_type"] = "straight_line" if distance_meters is not None else None
    return enriched


def _listing_matches(
    row: dict[str, Any],
    *,
    listing_ids: set[str],
    search: str | None,
    min_price: float | None,
    max_price: float | None,
    bedrooms: int | None,
    housing_type: str | None,
    roommates_wanted: bool,
    lease_type: str | None,
    is_sublet: bool | None,
    summer_available: bool | None,
    preferred_gender: str | None,
    furnished: bool | None,
    utilities_included: bool | None,
    parking_available: bool | None,
    laundry: bool | None,
    pet_policy: str | None,
    map_ready: bool | None,
    max_distance_km: float | None,
    max_walk_minutes: int | None,
    max_transit_minutes: int | None,
    data_quality: str | None,
    ranking_status: RankingStatus | None,
    min_score: float | None,
    max_score: float | None,
    min_value_score: float | None,
    min_campus_access_score: float | None,
    min_transit_score: float | None,
) -> bool:
    if listing_ids and str(row.get("listing_id")) not in listing_ids:
        return False
    if (
        max_walk_minutes is not None or max_transit_minutes is not None
    ) and route_use_prohibited(row):
        return False
    price = _number(row.get("price_monthly"))
    if min_price is not None and (price is None or price < min_price):
        return False
    if max_price is not None and (price is None or price > max_price):
        return False
    if bedrooms is not None and _number(row.get("bedrooms")) != bedrooms:
        return False
    if roommates_wanted and row.get("housing_type") not in {
        "house_to_share",
        "apartment_to_share",
    }:
        return False
    text_filters = {
        "housing_type": housing_type,
        "lease_type": lease_type,
        "preferred_gender": preferred_gender,
        "pet_policy": pet_policy,
    }
    for field, expected in text_filters.items():
        if expected and str(row.get(field) or "").casefold() != expected.casefold():
            return False
    bool_filters = {
        "is_sublet": is_sublet,
        "furnished": furnished,
        "utilities_included": utilities_included,
        "parking_available": parking_available,
        "laundry": laundry,
        "map_ready": map_ready,
    }
    for field, expected in bool_filters.items():
        if field == "map_ready":
            if expected is not None and public_location(row)["map_visible"] is not expected:
                return False
            continue
        if expected is not None and _truth(row.get(field)) is not expected:
            return False
    if summer_available is not None and _summer_available(row) is not summer_available:
        return False
    distance = _number(row.get("selected_hotspot_distance_km"))
    if max_distance_km is not None and (distance is None or distance > max_distance_km):
        return False
    for field, maximum in (
        ("_walk_commute_minutes", max_walk_minutes),
        ("_transit_commute_minutes", max_transit_minutes),
    ):
        minutes = _number(row.get(field))
        if maximum is not None and (minutes is None or minutes > maximum):
            return False
    if data_quality and listing_quality(row)["status"] != data_quality:
        return False
    if ranking_status and row.get("ranking_status") != ranking_status:
        return False
    for field, minimum in (
        ("ranking_overall_score", min_score),
        ("ranking_value_score", min_value_score),
        ("ranking_campus_access_score", min_campus_access_score),
        ("ranking_transit_score", min_transit_score),
    ):
        value = _number(row.get(field))
        if minimum is not None and (value is None or value < minimum):
            return False
    overall = _number(row.get("ranking_overall_score"))
    if max_score is not None and (overall is None or overall > max_score):
        return False
    if search:
        needle = search.casefold().strip()
        haystack = " ".join(
            str(value)
            for value in (
                row.get("title"),
                row.get("address"),
                row.get("housing_type"),
                row.get("lease_type"),
                _amenity_search_text(row.get("amenities")),
            )
            if value not in (None, "")
        ).casefold()
        if needle not in haystack:
            return False
    return True


def _sort_listings(
    rows: list[dict[str, Any]], sort: RankingSort, order: SortOrder
) -> list[dict[str, Any]]:
    def listing_id(row: dict[str, Any]) -> str:
        return str(row.get("listing_id") or "")

    numeric_fields = {
        "recommended": "ranking_overall_score",
        "overall_score": "ranking_overall_score",
        "value_score": "ranking_value_score",
        "campus_access_score": "ranking_campus_access_score",
        "transit_score": "ranking_transit_score",
        "price_low": "price_monthly",
        "price_high": "price_monthly",
        "distance": "selected_hotspot_distance_km",
    }
    if sort in numeric_fields:
        field = numeric_fields[sort]

        def numeric_key(row: dict[str, Any]) -> tuple[bool, float, str]:
            value = _number(row.get(field))
            ordered = -value if value is not None and order == "desc" else value or 0
            return value is None, ordered, listing_id(row)

        return sorted(rows, key=numeric_key)

    def newest_key(row: dict[str, Any]) -> tuple[bool, float, str]:
        value = str(row.get("last_seen_at") or "")
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            timestamp = 0.0
        ordered = -timestamp if order == "desc" else timestamp
        return not bool(value), ordered, listing_id(row)

    return sorted(rows, key=newest_key)


def _project_listing_ranking(row: dict[str, Any]) -> dict[str, Any]:
    result = apply_public_location(row)
    result["ranking"] = project_persisted_ranking(row)
    result["freshness"] = observation_freshness(row)
    for field in RANKING_STORAGE_FIELDS:
        result.pop(field, None)
    return result


def _public_summary(
    row: dict[str, Any], *, transportation: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = _project_listing_ranking(row)
    result.pop("description", None)
    result.pop("_walk_commute_minutes", None)
    result.pop("_transit_commute_minutes", None)
    result["summer_available"] = _summer_available(row)
    result["field_quality"] = {
        field: field_quality(row, field)
        for field in ("price_monthly", "bedrooms", "is_sublet", "furnished", "address")
    }
    unavailable_transportation = {
        "availability": "unavailable",
        "walking": {"status": "unavailable", "duration_minutes": None},
        "cycling": {"status": "unavailable", "duration_minutes": None},
        "transit": {"status": "unavailable", "duration_minutes": None},
    }
    result["transportation"] = (
        transportation
        if result["location"]["route_available"] and transportation
        else unavailable_transportation
    )
    return result


def _public_map_markers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project the compact, fail-closed contract used by the discovery map."""
    markers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        public = apply_public_location(row)
        listing_id = str(public.get("listing_id") or "")
        coordinates = _coordinates(public)
        if (
            not listing_id
            or listing_id in seen
            or not public["location"]["map_visible"]
            or coordinates is None
        ):
            continue
        seen.add(listing_id)
        markers.append(
            {
                "listing_id": listing_id,
                "title": public.get("title"),
                "address": public.get("address"),
                "price_monthly": _number(public.get("price_monthly")),
                "housing_type": public.get("housing_type"),
                "latitude": coordinates.latitude,
                "longitude": coordinates.longitude,
                "location": {
                    key: public["location"][key]
                    for key in ("status", "map_visible", "route_available")
                },
            }
        )
    return markers


def create_app(
    *,
    repository: ListingRepository | None = None,
    hotspots: tuple[Hotspot, ...] = DEFAULT_HOTSPOTS,
    travel_provider: TravelTimeProvider | None = None,
    accessibility_repository: AccessibilityRepository | None = None,
    accessibility_provider_name: str | None = None,
    accessibility_provider_profile: str | None = None,
    schedule_version: str | None = None,
    network_version: str | None = None,
    gtfs_freshness: GtfsFreshnessReport | None = None,
    walking_surface_service: WalkingSurfaceService | None = None,
    exact_routing_provider: RoutingProvider | None = None,
) -> FastAPI:
    app = FastAPI(
        title="UWO Housing API",
        description="Provider-neutral housing discovery API for Western students.",
        version="0.3.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip()
            for origin in os.getenv(
                "CORS_ORIGINS",
                "http://127.0.0.1:5173,http://localhost:5173",
            ).split(",")
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    repository_holder: dict[str, ListingRepository | None] = {"value": repository}
    accessibility_holder: dict[str, AccessibilityRepository | None] = {
        "value": accessibility_repository
    }
    surface_holder: dict[str, WalkingSurfaceService | None] = {"value": walking_surface_service}
    exact_route_holder: dict[str, RoutingProvider | None] = {"value": exact_routing_provider}
    provider = travel_provider or StraightLineEstimateProvider(
        walking_speed_kmh=float(os.getenv("WALKING_SPEED_KMH", "4.8")),
        cycling_speed_kmh=float(os.getenv("CYCLING_SPEED_KMH", "15")),
    )

    def transport_configuration() -> dict[str, Any]:
        profile_repository = get_accessibility_repository()
        fixture_configuration = getattr(
            profile_repository, "fixture_configuration", {}
        )
        local = _local_transport_configuration()
        return {
            "provider": (
                accessibility_provider_name
                or fixture_configuration.get("provider")
                or os.getenv("ACCESSIBILITY_PROVIDER", "").strip()
                or local.get("provider")
                or "offline"
            ),
            "provider_profile": (
                accessibility_provider_profile
                or fixture_configuration.get("provider_profile")
                or os.getenv("ACCESSIBILITY_PROVIDER_PROFILE", "").strip()
                or local.get("provider_profile")
                or "v1"
            ),
            "schedule_version": (
                schedule_version
                if schedule_version is not None
                else fixture_configuration.get("schedule_version")
                or os.getenv("ACCESSIBILITY_SCHEDULE_VERSION", "").strip()
                or local.get("schedule_version")
            ),
            "network_version": (
                network_version
                if network_version is not None
                else fixture_configuration.get("network_version")
                or os.getenv("ACCESSIBILITY_NETWORK_VERSION", "").strip()
                or local.get("network_version")
            ),
            "gtfs": gtfs_freshness or (
                None if fixture_configuration else local.get("gtfs")
            ),
            "fixture_configuration": fixture_configuration,
        }

    def property_transport_profiles(
        property_ids: list[int], hotspot: Hotspot
    ) -> list[Any]:
        configuration = transport_configuration()
        return get_accessibility_repository().find_current_property_profiles(
            property_ids,
            hotspot.id,
            at=datetime.now(timezone.utc),
            provider=configuration["provider"],
            provider_profile=configuration["provider_profile"],
            schedule_version=configuration["schedule_version"],
            network_version=configuration["network_version"],
        )

    def fallback_filtered_listings(
        listing_repository: ListingRepository,
        query: ListingQuery,
        hotspot: Hotspot,
    ) -> tuple[list[dict[str, Any]], dict[int, list[Any]] | None]:
        """Apply the collection filter contract for non-queryable fixtures."""
        enriched = [
            _add_destination_estimate(row, hotspot)
            for row in listing_repository.list_summaries(limit=3000)
        ]
        profiles_by_property: dict[int, list[Any]] | None = None
        if query.max_walk_minutes is not None or query.max_transit_minutes is not None:
            source_property_ids = sorted(
                {
                    property_id
                    for row in enriched
                    if (property_id := _integer(row.get("property_id"))) is not None
                }
            )
            profiles_by_property = {
                property_id: [] for property_id in source_property_ids
            }
            try:
                for profile in property_transport_profiles(source_property_ids, hotspot):
                    property_id = profile.origin.property_id
                    if property_id is not None:
                        profiles_by_property.setdefault(property_id, []).append(profile)
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Transportation profile store is unavailable",
                ) from exc
            for row in enriched:
                property_id = _integer(row.get("property_id"))
                commute = compact_transportation_summary(
                    profiles_by_property.get(property_id, []),
                    hotspot=hotspot,
                    gtfs=transport_configuration()["gtfs"],
                )
                walking = commute["walking"]
                transit = commute["transit"]
                row["_walk_commute_minutes"] = (
                    walking["duration_minutes"]
                    if walking["status"] == "available"
                    else None
                )
                row["_transit_commute_minutes"] = (
                    transit["duration_minutes"]
                    if transit["status"] == "available"
                    else None
                )
        matched = [
            row
            for row in enriched
            if _listing_matches(
                row,
                listing_ids=set(query.listing_ids),
                search=query.search,
                min_price=query.min_price,
                max_price=query.max_price,
                bedrooms=query.bedrooms,
                housing_type=query.housing_type,
                roommates_wanted=query.roommates_wanted,
                lease_type=query.lease_type,
                is_sublet=query.is_sublet,
                summer_available=query.summer_available,
                preferred_gender=query.preferred_gender,
                furnished=query.furnished,
                utilities_included=query.utilities_included,
                parking_available=query.parking_available,
                laundry=query.laundry,
                pet_policy=query.pet_policy,
                map_ready=query.map_ready,
                max_distance_km=query.max_distance_km,
                max_walk_minutes=query.max_walk_minutes,
                max_transit_minutes=query.max_transit_minutes,
                data_quality=query.data_quality,
                ranking_status=query.ranking_status,
                min_score=query.min_score,
                max_score=query.max_score,
                min_value_score=query.min_value_score,
                min_campus_access_score=query.min_campus_access_score,
                min_transit_score=query.min_transit_score,
            )
        ]
        return _sort_listings(matched, query.sort, query.order), profiles_by_property

    def get_repository() -> ListingRepository:
        if repository_holder["value"] is not None:
            return repository_holder["value"]
        fixture = os.getenv("HOUSING_FIXTURE_CSV")
        try:
            if fixture:
                repository_holder["value"] = FixtureCsvListingRepository(
                    (PROJECT_ROOT / fixture).resolve()
                )
            elif os.getenv("DATABASE_URL", "").strip() or os.getenv(
                "ACCESSIBILITY_DATABASE_URL", ""
            ).strip():
                repository_holder["value"] = (
                    PostgresListingRepository.from_environment()
                )
            else:
                repository_holder["value"] = (
                    SupabaseListingRepository.from_environment()
                )
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail=f"Listing repository unavailable: {exc}") from exc
        return repository_holder["value"]

    def public_listing_history(
        listing_repository: ListingRepository,
        listing: dict[str, Any],
    ) -> dict[str, Any]:
        observations = (
            listing_repository.list_observations(
                str(listing["listing_id"]), limit=51
            )
            if isinstance(listing_repository, ListingHistoryRepository)
            else []
        )
        return project_listing_history(listing, observations, event_limit=20)

    def get_accessibility_repository() -> AccessibilityRepository:
        if accessibility_holder["value"] is None:
            accessibility_fixture = os.getenv("ACCESSIBILITY_FIXTURE_PATH", "").strip()
            try:
                if accessibility_fixture:
                    accessibility_holder["value"] = FixtureAccessibilityRepository(
                        (PROJECT_ROOT / accessibility_fixture).resolve()
                    )
                elif os.getenv("HOUSING_FIXTURE_CSV", "").strip():
                    accessibility_holder["value"] = InMemoryAccessibilityRepository()
                elif os.getenv("DATABASE_URL", "").strip() or os.getenv(
                    "ACCESSIBILITY_DATABASE_URL", ""
                ).strip():
                    accessibility_holder["value"] = (
                        PostgresAccessibilityRepository.from_environment()
                    )
                else:
                    accessibility_holder["value"] = InMemoryAccessibilityRepository()
            except (OSError, RuntimeError, ValueError) as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"Accessibility repository unavailable: {exc}",
                ) from exc
        return accessibility_holder["value"]

    def get_walking_surface_service() -> WalkingSurfaceService:
        if surface_holder["value"] is None:
            try:
                policy = load_surface_policy(PROJECT_ROOT / "config" / "travel-time-surface.example.toml")
                surface_holder["value"] = WalkingSurfaceService(
                    repository=PostgresSurfaceRepository.from_environment(),
                    r5=HttpR5SurfaceClient(), policy=policy,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=503, detail=f"Walking surface service unavailable: {exc}") from exc
        return surface_holder["value"]

    def get_exact_routing_provider() -> RoutingProvider:
        if exact_route_holder["value"] is None:
            try:
                config = load_worker_config(PROJECT_ROOT / "config" / "accessibility-worker.toml", PROJECT_ROOT)
                bundle = load_routing_bundle(config)
                exact_route_holder["value"] = OpenTripPlannerProvider(
                    base_url=config.otp_base_url, router_id=config.otp_router_id,
                    timeout_seconds=config.otp_request_timeout_seconds, metadata=bundle.metadata,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=503, detail="Exact walking route is unavailable") from exc
        return exact_route_holder["value"]

    @app.get("/health")
    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/hotspots")
    def get_hotspots(
        category: str | None = Query(default=None),
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        if category is not None and category not in HOTSPOT_CATEGORIES:
            raise HTTPException(status_code=422, detail="Invalid hotspot category")
        selected = [
            hotspot
            for hotspot in hotspots
            if (include_inactive or hotspot.is_active)
            and (category is None or hotspot.category == category)
        ]
        selected.sort(key=lambda hotspot: (hotspot.display_order, hotspot.id))
        return {"count": len(selected), "hotspots": [hotspot.to_dict() for hotspot in selected]}

    @app.get("/api/accessibility/time-periods")
    def get_accessibility_time_periods() -> dict[str, Any]:
        return {
            "count": len(DEFAULT_TRANSIT_PERIODS),
            "time_periods": [period.to_dict() for period in DEFAULT_TRANSIT_PERIODS],
            "live_departures_supported": False,
        }

    @app.get("/api/travel-time-surfaces/grid")
    def get_walking_surface_grid() -> dict[str, Any]:
        return get_walking_surface_service().policy.grid_metadata()

    @app.get("/api/listings/{listing_id}/travel-time-surface")
    def get_listing_walking_surface(listing_id: str, mode: str = "walking") -> dict[str, Any]:
        if mode != "walking":
            raise HTTPException(status_code=422, detail="Numerical travel-time surfaces support walking only")
        listing = get_repository().get_detail(listing_id)
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        if route_use_prohibited(listing):
            raise HTTPException(
                status_code=422,
                detail="Walking information is unavailable for this listing location",
            )
        property_id = _integer(listing.get("property_id"))
        coordinates = _coordinates(listing)
        if property_id is None or coordinates is None:
            raise HTTPException(status_code=422, detail="Listing has no trusted canonical property routing origin")
        property_origin = getattr(get_repository(), "get_property_origin", lambda _: None)(property_id)
        if property_origin is not None:
            coordinates = Coordinates(*property_origin)
        service = get_walking_surface_service()
        try:
            surface, cache_hit = service.get_or_compute(property_id=property_id, latitude=coordinates.latitude, longitude=coordinates.longitude, mode=mode)
        except UnsupportedSurfaceMode as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SurfaceUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except SurfaceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        body = service.metadata(surface, cache_hit=cache_hit)
        if surface.status == "computing":
            body["retry_after_seconds"] = 2
        return body

    @app.get("/api/travel-time-surfaces/{surface_id}/values")
    def get_walking_surface_values(surface_id: str, response: Response) -> Response:
        record = get_walking_surface_service().repository.find_by_id(surface_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Travel-time surface not found")
        if record.status != "ready" or record.compressed_payload is None:
            raise HTTPException(status_code=409, detail=f"Travel-time surface is {record.status}")
        response.headers["Content-Encoding"] = "gzip"
        response.headers["ETag"] = record.etag or ""
        response.headers["Cache-Control"] = "private, max-age=3600"
        return Response(content=record.compressed_payload, media_type="application/octet-stream", headers=dict(response.headers))

    @app.get("/api/travel-time-surfaces/{surface_id}/validity")
    def get_walking_surface_validity(surface_id: str) -> Response:
        record = get_walking_surface_service().repository.find_by_id(surface_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Travel-time surface not found")
        if record.status != "ready" or record.validity_mask is None:
            raise HTTPException(status_code=409, detail=f"Travel-time surface is {record.status}")
        return Response(content=record.validity_mask, media_type="application/octet-stream", headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/listings/{listing_id}/routes/walking")
    def get_listing_exact_walking_route(
        listing_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Route from the trusted canonical property origin to a map click."""
        try:
            destination = Coordinates(
                float(payload.get("destination_latitude")),
                float(payload.get("destination_longitude")),
            )
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="A valid destination latitude and longitude are required") from None
        # Keep arbitrary browser input inside the locally built London graph.
        if not (42.80 <= destination.latitude <= 43.20 and -81.50 <= destination.longitude <= -80.90):
            raise HTTPException(status_code=422, detail="Destination is outside the supported London routing area")
        listing = get_repository().get_detail(listing_id)
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        if route_use_prohibited(listing):
            raise HTTPException(
                status_code=422,
                detail="Exact routes are unavailable for this listing location",
            )
        property_id = _integer(listing.get("property_id"))
        property_origin = getattr(get_repository(), "get_property_origin", lambda _: None)(property_id) if property_id is not None else None
        if property_origin is None:
            raise HTTPException(status_code=422, detail="Listing has no trusted canonical property routing origin")
        origin = Coordinates(*property_origin)
        try:
            route = get_exact_routing_provider().get_route(
                RouteRequest(origin=origin, destination=destination, mode=TravelMode.WALKING)
            )
        except NoRouteError:
            return {"route": {"status": "unavailable", "mode": "walking", "origin": origin.to_dict(), "destination": destination.to_dict(), "is_estimate": False, "itinerary": None}, "message": "Exact walking route unavailable for this destination."}
        except RoutingProviderError:
            raise HTTPException(status_code=503, detail="Exact walking route is temporarily unavailable") from None
        if route.status.value != "available" or route.duration_seconds is None or route.distance_meters is None or route.itinerary is None:
            return {"route": {"status": "unavailable", "mode": "walking", "origin": origin.to_dict(), "destination": destination.to_dict(), "is_estimate": False, "itinerary": None}, "message": "Exact walking route unavailable for this destination."}
        return {"route": route.to_dict(), "message": None}

    @app.get("/api/listings")
    @app.get("/listings")
    def get_listings(
        listing_ids: str | None = Query(default=None, max_length=2000),
        search: str | None = Query(default=None, max_length=120),
        min_price: float | None = Query(default=None, ge=0),
        max_price: float | None = Query(default=None, ge=0),
        bedrooms: int | None = Query(default=None, ge=0, le=30),
        housing_type: str | None = Query(default=None, max_length=80),
        roommates_wanted: bool = False,
        lease_type: str | None = Query(default=None, max_length=80),
        is_sublet: bool | None = None,
        summer_available: bool | None = None,
        preferred_gender: str | None = Query(default=None, max_length=80),
        furnished: bool | None = None,
        utilities_included: bool | None = None,
        parking_available: bool | None = None,
        laundry: bool | None = None,
        pet_policy: str | None = Query(default=None, max_length=80),
        map_ready: bool | None = None,
        max_distance_km: float | None = Query(default=None, ge=0, le=100),
        max_walk_minutes: int | None = Query(default=None, ge=1, le=180),
        max_transit_minutes: int | None = Query(default=None, ge=1, le=180),
        hotspot_id: str = "western-main-campus",
        data_quality: str | None = Query(default=None, pattern="^(confirmed|parsed|needs-review)$"),
        ranking_status: RankingStatus | None = None,
        min_score: float | None = Query(default=None, ge=0, le=100),
        max_score: float | None = Query(default=None, ge=0, le=100),
        min_value_score: float | None = Query(default=None, ge=0, le=100),
        min_campus_access_score: float | None = Query(default=None, ge=0, le=100),
        min_transit_score: float | None = Query(default=None, ge=0, le=100),
        sort: RankingSort = "recommended",
        order: SortOrder | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        requested_listing_ids = tuple(
            dict.fromkeys(
                value.strip()
                for value in (listing_ids or "").split(",")
                if value.strip()
            )
        )
        if len(requested_listing_ids) > 100:
            raise HTTPException(
                status_code=422, detail="listing_ids supports at most 100 values"
            )
        if min_price is not None and max_price is not None and min_price > max_price:
            raise HTTPException(status_code=422, detail="min_price cannot exceed max_price")
        if min_score is not None and max_score is not None and min_score > max_score:
            raise HTTPException(status_code=422, detail="min_score cannot exceed max_score")
        hotspot = find_hotspot(hotspots, hotspot_id)
        if hotspot is None or not hotspot.is_active:
            raise HTTPException(status_code=404, detail="Hotspot not found or inactive")
        resolved_order = order or default_sort_order(sort)
        start = (page - 1) * page_size
        listing_repository = get_repository()
        configuration = transport_configuration()
        accessibility_context = AccessibilityFilterContext(
            hotspot_id=hotspot.id,
            provider=configuration["provider"],
            provider_profile=configuration["provider_profile"],
            schedule_version=configuration["schedule_version"],
            network_version=configuration["network_version"],
            at=datetime.now(timezone.utc),
        )
        listing_query = ListingQuery(
            listing_ids=requested_listing_ids,
            search=search,
            min_price=min_price,
            max_price=max_price,
            bedrooms=bedrooms,
            housing_type=housing_type,
            roommates_wanted=roommates_wanted,
            lease_type=lease_type,
            is_sublet=is_sublet,
            summer_available=summer_available,
            preferred_gender=preferred_gender,
            furnished=furnished,
            utilities_included=utilities_included,
            parking_available=parking_available,
            laundry=laundry,
            pet_policy=pet_policy,
            map_ready=map_ready,
            max_distance_km=max_distance_km,
            max_walk_minutes=max_walk_minutes,
            max_transit_minutes=max_transit_minutes,
            accessibility=accessibility_context,
            data_quality=data_quality,
            ranking_status=ranking_status,
            min_score=min_score,
            max_score=max_score,
            min_value_score=min_value_score,
            min_campus_access_score=min_campus_access_score,
            min_transit_score=min_transit_score,
            sort=sort,
            order=resolved_order,
            offset=start,
            limit=page_size,
        )
        profiles_by_property: dict[int, list[Any]] | None = None
        if isinstance(listing_repository, QueryableListingRepository):
            result_page = listing_repository.query_summaries(listing_query)
            paged = [
                _add_destination_estimate(row, hotspot) for row in result_page.rows
            ]
            total = result_page.total
        else:
            ordered, profiles_by_property = fallback_filtered_listings(
                listing_repository,
                listing_query,
                hotspot,
            )
            paged = ordered[start : start + page_size]
            total = len(ordered)
        property_ids = sorted(
            {
                property_id
                for row in paged
                if (property_id := _integer(row.get("property_id"))) is not None
            }
        )
        if profiles_by_property is None:
            profiles_by_property = {property_id: [] for property_id in property_ids}
            try:
                for profile in property_transport_profiles(property_ids, hotspot):
                    property_id = profile.origin.property_id
                    if property_id is not None:
                        profiles_by_property.setdefault(property_id, []).append(profile)
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Transportation profile store is unavailable",
                ) from exc
        public_rows = []
        for row in paged:
            property_id = _integer(row.get("property_id"))
            summary = compact_transportation_summary(
                profiles_by_property.get(property_id, []),
                hotspot=hotspot,
                gtfs=configuration["gtfs"],
            )
            public_rows.append(_public_summary(row, transportation=summary))
        return {
            "count": len(paged),
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
            "sort": sort,
            "order": resolved_order,
            "hotspot": hotspot.to_dict(),
            "listings": public_rows,
        }

    @app.get("/api/listings/map")
    def get_filtered_map_listings(
        listing_ids: str | None = Query(default=None, max_length=2000),
        search: str | None = Query(default=None, max_length=120),
        min_price: float | None = Query(default=None, ge=0),
        max_price: float | None = Query(default=None, ge=0),
        bedrooms: int | None = Query(default=None, ge=0, le=30),
        housing_type: str | None = Query(default=None, max_length=80),
        roommates_wanted: bool = False,
        lease_type: str | None = Query(default=None, max_length=80),
        is_sublet: bool | None = None,
        summer_available: bool | None = None,
        preferred_gender: str | None = Query(default=None, max_length=80),
        furnished: bool | None = None,
        utilities_included: bool | None = None,
        parking_available: bool | None = None,
        laundry: bool | None = None,
        pet_policy: str | None = Query(default=None, max_length=80),
        map_ready: bool | None = None,
        max_distance_km: float | None = Query(default=None, ge=0, le=100),
        max_walk_minutes: int | None = Query(default=None, ge=1, le=180),
        max_transit_minutes: int | None = Query(default=None, ge=1, le=180),
        hotspot_id: str = "western-main-campus",
        data_quality: str | None = Query(
            default=None, pattern="^(confirmed|parsed|needs-review)$"
        ),
        ranking_status: RankingStatus | None = None,
        min_score: float | None = Query(default=None, ge=0, le=100),
        max_score: float | None = Query(default=None, ge=0, le=100),
        min_value_score: float | None = Query(default=None, ge=0, le=100),
        min_campus_access_score: float | None = Query(default=None, ge=0, le=100),
        min_transit_score: float | None = Query(default=None, ge=0, le=100),
        sort: RankingSort = "recommended",
        order: SortOrder | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """Return all safely mappable listings matching discovery filters."""
        requested_listing_ids = tuple(
            dict.fromkeys(
                value.strip()
                for value in (listing_ids or "").split(",")
                if value.strip()
            )
        )
        if len(requested_listing_ids) > 100:
            raise HTTPException(
                status_code=422, detail="listing_ids supports at most 100 values"
            )
        if min_price is not None and max_price is not None and min_price > max_price:
            raise HTTPException(
                status_code=422, detail="min_price cannot exceed max_price"
            )
        if min_score is not None and max_score is not None and min_score > max_score:
            raise HTTPException(
                status_code=422, detail="min_score cannot exceed max_score"
            )
        hotspot = find_hotspot(hotspots, hotspot_id)
        if hotspot is None or not hotspot.is_active:
            raise HTTPException(status_code=404, detail="Hotspot not found or inactive")
        configuration = transport_configuration()
        query = ListingQuery(
            listing_ids=requested_listing_ids,
            search=search,
            min_price=min_price,
            max_price=max_price,
            bedrooms=bedrooms,
            housing_type=housing_type,
            roommates_wanted=roommates_wanted,
            lease_type=lease_type,
            is_sublet=is_sublet,
            summer_available=summer_available,
            preferred_gender=preferred_gender,
            furnished=furnished,
            utilities_included=utilities_included,
            parking_available=parking_available,
            laundry=laundry,
            pet_policy=pet_policy,
            map_ready=map_ready,
            max_distance_km=max_distance_km,
            max_walk_minutes=max_walk_minutes,
            max_transit_minutes=max_transit_minutes,
            accessibility=AccessibilityFilterContext(
                hotspot_id=hotspot.id,
                provider=configuration["provider"],
                provider_profile=configuration["provider_profile"],
                schedule_version=configuration["schedule_version"],
                network_version=configuration["network_version"],
                at=datetime.now(timezone.utc),
            ),
            data_quality=data_quality,
            ranking_status=ranking_status,
            min_score=min_score,
            max_score=max_score,
            min_value_score=min_value_score,
            min_campus_access_score=min_campus_access_score,
            min_transit_score=min_transit_score,
            sort=sort,
            order=order or default_sort_order(sort),
            # Deliberately independent of accepted sidebar pagination parameters.
            offset=0,
            limit=3000,
        )
        listing_repository = get_repository()
        if isinstance(listing_repository, QueryableListingMapRepository):
            rows = listing_repository.query_map_markers(query)
        else:
            rows, _profiles = fallback_filtered_listings(
                listing_repository, query, hotspot
            )
        markers = _public_map_markers(rows)
        return {
            "count": len(markers),
            "listings": markers,
        }

    @app.get("/listings/map")
    def get_map_listings(limit: int = Query(default=2000, ge=1, le=3000)) -> dict[str, Any]:
        rows = [
            row
            for row in get_repository().list_summaries(limit=limit)
            if public_location(row)["map_visible"] and _coordinates(row) is not None
        ]
        return {
            "count": len(rows),
            "listings": [_project_listing_ranking(row) for row in rows],
        }

    @app.get("/api/listings/{listing_id}/accessibility")
    def get_listing_accessibility(
        listing_id: str,
        hotspot_id: str = "western-main-campus",
        mode: TravelMode | None = None,
        time_period: TimePeriod | None = None,
        include_geometry: bool = True,
    ) -> dict[str, Any]:
        listing = get_repository().get_detail(listing_id)
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        hotspot = find_hotspot(hotspots, hotspot_id)
        if hotspot is None or not hotspot.is_active:
            raise HTTPException(status_code=404, detail="Hotspot not found or inactive")
        origin = Location(
            id=listing_id,
            name=str(listing.get("address") or listing.get("title") or "Listing"),
            coordinates=_coordinates(listing),
            address=listing.get("address"),
        )
        if mode is TravelMode.TRANSIT and time_period is None:
            raise HTTPException(
                status_code=422,
                detail="time_period is required when mode=transit",
            )
        property_id = _integer(listing.get("property_id"))
        location = public_location(listing)
        if route_use_prohibited(listing):
            if mode is not None:
                raise HTTPException(
                    status_code=422,
                    detail="Routes are unavailable for this listing location",
                )
            return {
                "listing_id": listing_id,
                "property_id": property_id,
                "destination": hotspot.to_dict(),
                "location": location,
                "results": [],
                "transportation": {
                    "availability": "unavailable",
                    "walking": {"status": "unavailable", "duration_minutes": None},
                    "cycling": {"status": "unavailable", "duration_minutes": None},
                    "transit": {"status": "unavailable", "duration_minutes": None},
                },
            }
        configuration = transport_configuration()
        try:
            profiles = property_transport_profiles(
                [property_id] if property_id is not None else [], hotspot
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Transportation profile store is unavailable",
            ) from exc
        response: dict[str, Any] = {
            "listing_id": listing_id,
            "property_id": property_id,
            "destination": hotspot.to_dict(),
            "location": location,
        }
        if mode is None:
            response["results"] = [
                result.to_dict()
                for result in provider.get_travel_times(
                    origin, [hotspot], list(TravelMode)
                )
            ]
            response["transportation"] = transportation_overview(
                profiles,
                hotspot=hotspot,
                gtfs=configuration["gtfs"],
            )
        else:
            coordinates = _coordinates(listing)
            origin_type = (
                OriginType.PROPERTY
                if property_id is not None
                else OriginType.ENTRANCE
                if listing.get("property_match_key")
                else OriginType.COORDINATE
            )
            walking_minutes = _number(listing.get("walking_minutes_to_nearest_stop"))
            request = AccessibilityRequest(
                origin=AccessibilityOrigin(
                    coordinates=coordinates,
                    property_id=property_id,
                    origin_type=origin_type,
                    entrance_fingerprint=listing.get("property_match_key"),
                    nearest_stop_id=listing.get("nearest_stop_id"),
                    walking_to_stop_seconds=(
                        round(walking_minutes * 60)
                        if walking_minutes is not None
                        else None
                    ),
                ),
                hotspot=hotspot,
                travel_mode=mode,
                time_period=time_period if mode is TravelMode.TRANSIT else None,
                provider=configuration["provider"],
                provider_profile=configuration["provider_profile"],
                requested_at=datetime.now(timezone.utc),
                schedule_version=(
                    configuration["schedule_version"]
                    if mode is TravelMode.TRANSIT
                    else None
                ),
                network_version=configuration["network_version"],
            )
            try:
                decision = AccessibilityResolver(
                    get_accessibility_repository()
                ).resolve(request, listing_id=listing_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Transportation profile store is unavailable",
                ) from exc
            profile = decision.result
            exact_route = decision.result_type in {
                AccessibilityResultType.CACHED_EXACT_PROPERTY,
                AccessibilityResultType.CACHED_EXACT_ORIGIN,
            }
            samples = (
                get_accessibility_repository().load_samples(profile.profile_id)
                if mode is TravelMode.TRANSIT
                and exact_route
                and profile.profile_id is not None
                else []
            )
            response.update(decision.to_dict())
            route_payload = route_detail(
                profile,
                samples=samples,
                gtfs=configuration["gtfs"],
                include_geometry=include_geometry,
            )
            if not exact_route:
                route_payload["geometry_available"] = False
                route_payload["itinerary"] = None
            route_payload.update(
                {
                    "result_type": response.get("result_type"),
                    "minimum_duration_seconds": response.get(
                        "minimum_duration_seconds"
                    ),
                    "maximum_duration_seconds": response.get(
                        "maximum_duration_seconds"
                    ),
                    "reuse_explanation": response.get("reuse_explanation"),
                }
            )
            response["route"] = route_payload
            response["schedule"] = transportation_overview(
                [], hotspot=hotspot, gtfs=configuration["gtfs"]
            )["schedule"]
        fixture_configuration = configuration["fixture_configuration"]
        if fixture_configuration:
            response["fixture_data"] = True
            response["fixture_notice"] = fixture_configuration.get("warning")
        return response

    @app.get("/api/listings/{listing_id}")
    @app.get("/listings/{listing_id}")
    def get_listing_by_id(listing_id: str) -> dict[str, Any]:
        listing_repository = get_repository()
        listing = listing_repository.get_detail(listing_id)
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        result = _project_listing_ranking(listing)
        history = public_listing_history(listing_repository, listing)
        result["history"] = history
        result["freshness"] = {
            "first_observed_at": history["first_observed_at"],
            "last_observed_at": history["last_observed_at"],
            "last_meaningful_source_change_at": history[
                "last_meaningful_source_change_at"
            ],
        }
        result["data_quality"] = listing_quality(result)
        result["field_quality"] = {
            field: field_quality(result, field)
            for field in ("price_monthly", "bedrooms", "is_sublet", "furnished", "address", "description")
        }
        return result

    @app.get("/api/listings/{listing_id}/history")
    def get_listing_history(listing_id: str) -> dict[str, Any]:
        listing_repository = get_repository()
        listing = listing_repository.get_detail(listing_id)
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        return public_listing_history(listing_repository, listing)

    @app.get("/stats")
    def get_stats() -> dict[str, Any]:
        rows = get_repository().list_summaries(limit=3000)
        prices = [value for row in rows if (value := _number(row.get("price_monthly"))) is not None]
        return {
            "total_listings": len(rows),
            "map_ready_listings": sum(_truth(row.get("map_ready")) is True for row in rows),
            "average_price": round(sum(prices) / len(prices), 2) if prices else None,
        }

    return app


app = create_app()
