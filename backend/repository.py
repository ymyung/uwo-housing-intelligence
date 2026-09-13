"""Replaceable listing repositories for PostgreSQL, Supabase, and fixtures."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable


BASE_SUMMARY_COLUMNS = """
image_url,image_urls,available_from,available_to,availability_text,
availability_category,otp_status,walk_time_to_western_min,
transit_time_to_western_min,transit_walk_time_min,transit_bus_time_min,
transit_transfers,otp_used_transit,otp_route_summary,nearest_stop_id,
nearest_stop_name,nearest_stop_distance_m,walking_minutes_to_nearest_stop,
nearby_stop_count,nearby_route_ids,nearby_route_names,western_route_ids,
western_route_names,has_direct_western_route,transit_score,listing_id,
listing_url,title,address,price_numeric,price_monthly,price_text,price_period,
housing_type,bedrooms,lease_type,lease_term_months,is_sublet,furnished,
utilities_included,utilities_status,parking_available,parking_spaces,laundry,
dishwasher,air_conditioning,bathroom_type,bathrooms,preferred_gender,
tenant_type,latitude,longitude,distance_to_western_km,map_ready,
geocode_status,geocode_confidence,geocode_quality_issue,amenities,scraped_ok,
listing_status,first_seen_at,last_seen_at,property_id,property_match_key
""".replace("\n", "")

LOCATION_API_COLUMNS = """
location_status,location_map_visible,location_route_available,
location_reason_codes
""".replace("\n", "")

RANKING_API_COLUMNS = """
provenance_data,review_flags,needs_manual_review,pet_policy,data_quality_status,
ranking_version,ranking_status,ranking_overall_score,ranking_value_score,
ranking_campus_access_score,ranking_transit_score,ranking_amenity_score,
ranking_data_quality_score,ranking_explanation,ranking_input_fingerprint,
ranking_computed_at
""".replace("\n", "")

SUMMARY_COLUMNS = (
    f"{BASE_SUMMARY_COLUMNS},{RANKING_API_COLUMNS},{LOCATION_API_COLUMNS}"
)

MAP_MARKER_COLUMNS = """
listing_id,title,address,price_monthly,housing_type,latitude,longitude,
location_status,location_map_visible,location_route_available,
location_reason_codes
""".replace("\n", "")

HISTORY_OBSERVATION_COLUMNS = """
id,observed_at,change_type,comparison_data,title,description,availability_text
""".replace("\n", "")

RankingStatus = Literal["ranked", "partial", "excluded"]
RankingSort = Literal[
    "recommended",
    "overall_score",
    "value_score",
    "campus_access_score",
    "transit_score",
    "price_low",
    "price_high",
    "distance",
    "newest",
]
SortOrder = Literal["asc", "desc"]

SUMMER_AVAILABILITY_CATEGORIES = ("summer", "summer_only", "summer_available")
NON_SUMMER_AVAILABILITY_CATEGORIES = ("non_summer",)


@dataclass(frozen=True)
class AccessibilityFilterContext:
    """Version identity required to filter against current persisted profiles."""

    hotspot_id: str
    provider: str
    provider_profile: str
    schedule_version: str | None
    network_version: str | None
    at: datetime


@dataclass(frozen=True)
class ListingQuery:
    listing_ids: tuple[str, ...] = ()
    search: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    bedrooms: int | None = None
    housing_type: str | None = None
    roommates_wanted: bool = False
    lease_type: str | None = None
    is_sublet: bool | None = None
    summer_available: bool | None = None
    preferred_gender: str | None = None
    furnished: bool | None = None
    utilities_included: bool | None = None
    parking_available: bool | None = None
    laundry: bool | None = None
    pet_policy: str | None = None
    map_ready: bool | None = None
    max_distance_km: float | None = None
    max_walk_minutes: int | None = None
    max_transit_minutes: int | None = None
    accessibility: AccessibilityFilterContext | None = None
    data_quality: str | None = None
    ranking_status: RankingStatus | None = None
    min_score: float | None = None
    max_score: float | None = None
    min_value_score: float | None = None
    min_campus_access_score: float | None = None
    min_transit_score: float | None = None
    sort: RankingSort = "recommended"
    order: SortOrder = "desc"
    offset: int = 0
    limit: int = 50


@dataclass(frozen=True)
class ListingPage:
    rows: list[dict[str, Any]]
    total: int


class ListingRepository(Protocol):
    def list_summaries(self, *, limit: int = 3000) -> list[dict[str, Any]]: ...

    def get_detail(self, listing_id: str) -> dict[str, Any] | None: ...


@runtime_checkable
class ListingHistoryRepository(ListingRepository, Protocol):
    def list_observations(
        self, listing_id: str, *, limit: int = 51
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class QueryableListingRepository(ListingRepository, Protocol):
    def query_summaries(self, query: ListingQuery) -> ListingPage: ...


@runtime_checkable
class QueryableListingMapRepository(ListingRepository, Protocol):
    def query_map_markers(self, query: ListingQuery) -> list[dict[str, Any]]: ...


class InMemoryListingRepository:
    def __init__(
        self,
        listings: list[dict[str, Any]],
        *,
        observations: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._listings = [dict(listing) for listing in listings]
        self._observations = {
            str(listing_id): [dict(observation) for observation in rows]
            for listing_id, rows in (observations or {}).items()
        }

    def list_summaries(self, *, limit: int = 3000) -> list[dict[str, Any]]:
        return [dict(listing) for listing in self._listings[:limit]]

    def get_detail(self, listing_id: str) -> dict[str, Any] | None:
        return next(
            (dict(row) for row in self._listings if str(row.get("listing_id")) == listing_id),
            None,
        )

    def list_observations(
        self, listing_id: str, *, limit: int = 51
    ) -> list[dict[str, Any]]:
        rows = self._observations.get(str(listing_id), [])
        ordered = sorted(
            rows,
            key=lambda row: (
                str(row.get("observed_at") or ""),
                int(row.get("id") or 0),
            ),
            reverse=True,
        )
        return [dict(row) for row in ordered[:limit]]


def _fixture_value(key: str, value: str) -> Any:
    text = value.strip()
    if not text:
        return None
    if key in {
        "is_sublet",
        "furnished",
        "utilities_included",
        "parking_available",
        "laundry",
        "dishwasher",
        "air_conditioning",
        "map_ready",
        "needs_manual_review",
        "scraped_ok",
        "location_map_visible",
        "location_route_available",
        "has_direct_western_route",
        "otp_used_transit",
    }:
        return text.lower() in {"true", "1", "yes"}
    if key in {"bedrooms", "lease_term_months", "parking_spaces"}:
        try:
            return int(float(text))
        except ValueError:
            return None
    if key in {
        "price_numeric",
        "price_monthly",
        "latitude",
        "longitude",
        "distance_to_western_km",
        "geocode_confidence",
        "bathrooms",
        "ranking_overall_score",
        "ranking_value_score",
        "ranking_campus_access_score",
        "ranking_transit_score",
        "ranking_amenity_score",
        "ranking_data_quality_score",
    }:
        try:
            return float(text)
        except ValueError:
            return None
    if key in {
        "review_flags",
        "amenities",
        "amenities_list",
        "location_reason_codes",
        "image_urls",
        "nearby_route_ids",
        "nearby_route_names",
        "western_route_ids",
        "western_route_names",
    }:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return [part.strip() for part in text.split("|") if part.strip()]
    if key in {"ranking_explanation", "provenance_data"}:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}
    return text


class FixtureCsvListingRepository(InMemoryListingRepository):
    def __init__(self, path: Path) -> None:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            listings = []
            for source in csv.DictReader(handle):
                row = {key: _fixture_value(key, value or "") for key, value in source.items()}
                if "amenities_list" in row and "amenities" not in row:
                    row["amenities"] = row.pop("amenities_list")
                if row.get("price_monthly") is None and row.get("price_numeric") is not None:
                    amount = float(row["price_numeric"])
                    period = str(row.get("price_period") or "").lower()
                    if period in {"month", "monthly", "per month", "month_per_bedroom"}:
                        row["price_monthly"] = amount
                    elif period in {"week", "weekly", "per week"}:
                        row["price_monthly"] = round(amount * 52 / 12, 2)
                    elif period in {"day", "daily", "per day"}:
                        row["price_monthly"] = round(amount * 365 / 12, 2)
                listings.append(row)
        super().__init__(listings)


SORT_COLUMNS: dict[RankingSort, tuple[str, SortOrder]] = {
    "recommended": ("ranking_overall_score", "desc"),
    "overall_score": ("ranking_overall_score", "desc"),
    "value_score": ("ranking_value_score", "desc"),
    "campus_access_score": ("ranking_campus_access_score", "desc"),
    "transit_score": ("ranking_transit_score", "desc"),
    "price_low": ("price_monthly", "asc"),
    "price_high": ("price_monthly", "desc"),
    "distance": ("distance_to_western_km", "asc"),
    "newest": ("last_seen_at", "desc"),
}


def default_sort_order(sort: RankingSort) -> SortOrder:
    """Return the established direction for a sort when no order is supplied."""

    return SORT_COLUMNS[sort][1]


def _postgres_where(query: ListingQuery) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    parameters: list[Any] = []

    def compare(column: str, operator: str, value: Any) -> None:
        if value is not None:
            clauses.append(f"{column} {operator} %s")
            parameters.append(value)

    compare("price_monthly", ">=", query.min_price)
    compare("price_monthly", "<=", query.max_price)
    compare("bedrooms", "=", query.bedrooms)
    compare("is_sublet", "=", query.is_sublet)
    compare("furnished", "=", query.furnished)
    compare("utilities_included", "=", query.utilities_included)
    compare("parking_available", "=", query.parking_available)
    compare("laundry", "=", query.laundry)
    compare("location_map_visible", "=", query.map_ready)
    compare("distance_to_western_km", "<=", query.max_distance_km)
    compare("ranking_status", "=", query.ranking_status)
    compare("ranking_overall_score", ">=", query.min_score)
    compare("ranking_overall_score", "<=", query.max_score)
    compare("ranking_value_score", ">=", query.min_value_score)
    compare(
        "ranking_campus_access_score", ">=", query.min_campus_access_score
    )
    compare("ranking_transit_score", ">=", query.min_transit_score)

    if query.listing_ids:
        clauses.append("listing_id = any(%s)")
        parameters.append(list(query.listing_ids))

    if query.roommates_wanted:
        clauses.append("housing_type in ('house_to_share', 'apartment_to_share')")

    def commute(
        *, mode: str, maximum_minutes: int | None, time_period: str | None
    ) -> None:
        if maximum_minutes is None:
            return
        clauses.append("location_route_available")
        context = query.accessibility
        if context is None:
            clauses.append("false")
            return
        clauses.append(
            "exists ("
            "select 1 from public.housing_accessibility_profiles profile "
            "where profile.id = ("
            "select latest.id from public.housing_accessibility_profiles latest "
            "where latest.origin_property_id = property_id "
            "and latest.hotspot_id = %s and latest.travel_mode = %s "
            "and latest.time_period is not distinct from %s "
            "and latest.provider = %s and latest.provider_profile = %s "
            "and latest.network_version is not distinct from %s "
            "and (latest.travel_mode <> 'transit' "
            "or latest.schedule_version is not distinct from %s) "
            "and not latest.is_stale "
            "and (latest.expires_at is null or latest.expires_at > %s) "
            "order by latest.calculated_at desc, latest.id desc limit 1) "
            "and profile.result_type in "
            "('exact_route', 'cached_exact_property', 'cached_exact_origin') "
            "and profile.provider_metadata ->> 'quality_status' = 'complete' "
            "and profile.representative_duration_seconds is not null "
            "and profile.representative_duration_seconds <= %s)"
        )
        parameters.extend(
            (
                context.hotspot_id,
                mode,
                time_period,
                context.provider,
                context.provider_profile,
                context.network_version,
                context.schedule_version,
                context.at,
                maximum_minutes * 60,
            )
        )

    commute(mode="walking", maximum_minutes=query.max_walk_minutes, time_period=None)
    commute(
        mode="transit",
        maximum_minutes=query.max_transit_minutes,
        time_period="weekday_morning_commute",
    )

    for column, value in (
        ("housing_type", query.housing_type),
        ("lease_type", query.lease_type),
        ("preferred_gender", query.preferred_gender),
        ("pet_policy", query.pet_policy),
        ("data_quality_status", query.data_quality),
    ):
        if value:
            clauses.append(f"lower(coalesce({column}, '')) = lower(%s)")
            parameters.append(value)

    if query.summer_available is True:
        clauses.append(
            "lower(coalesce(availability_category, '')) "
            "in ('summer', 'summer_only', 'summer_available')"
        )
    elif query.summer_available is False:
        clauses.append(
            "lower(coalesce(availability_category, '')) in ('non_summer')"
        )
    if query.search:
        clauses.append(
            "concat_ws(' ', title, address, housing_type, lease_type, "
            "amenities::text) ilike %s"
        )
        parameters.append(f"%{query.search.strip()}%")
    return (" where " + " and ".join(clauses) if clauses else ""), parameters


class PostgresListingRepository:
    """Read-only listing API adapter with database filtering and pagination."""

    RELATION = "public.product_housing_listings"

    def __init__(self, database_url: str) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._database_url = database_url

    @classmethod
    def from_environment(cls) -> "PostgresListingRepository":
        database_url = (
            os.getenv("DATABASE_URL", "").strip()
            or os.getenv("ACCESSIBILITY_DATABASE_URL", "").strip()
        )
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL or ACCESSIBILITY_DATABASE_URL is not configured"
            )
        return cls(database_url)

    def _connect(self) -> Any:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "PostgreSQL listing API requires requirements-database.txt"
            ) from exc
        return psycopg.connect(self._database_url, row_factory=dict_row)

    def list_summaries(self, *, limit: int = 3000) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                f"select {SUMMARY_COLUMNS} from {self.RELATION} "
                "order by listing_id limit %s",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_detail(self, listing_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                f"select * from {self.RELATION} where listing_id = %s limit 1",
                (listing_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_observations(
        self, listing_id: str, *, limit: int = 51
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select observation.id, observation.observed_at,
                       observation.change_type, observation.comparison_data,
                       observation.title, observation.description,
                       observation.availability_text,
                       count(*) over () as total_observation_count
                from public.housing_listing_observations observation
                join public.housing_listings listing
                  on listing.id = observation.listing_id
                where listing.source = 'uwo_offcampus'
                  and listing.source_listing_id = %s
                order by observation.observed_at desc, observation.id desc
                limit %s
                """,
                (listing_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_property_origin(self, property_id: int) -> tuple[float, float] | None:
        """Return the canonical-property geocode, never an advertisement copy."""
        with self._connect() as connection:
            row = connection.execute(
                "select latitude, longitude from public.housing_properties where id = %s",
                (property_id,),
            ).fetchone()
        if row is None or row["latitude"] is None or row["longitude"] is None:
            return None
        return float(row["latitude"]), float(row["longitude"])

    def query_summaries(self, query: ListingQuery) -> ListingPage:
        where_sql, parameters = _postgres_where(query)
        sort_column, _ = SORT_COLUMNS[query.sort]
        direction = "desc" if query.order == "desc" else "asc"
        order_sql = (
            f" order by {sort_column} {direction} nulls last, listing_id asc"
        )
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"select count(*) from {self.RELATION}{where_sql}",
                    parameters,
                ).fetchone()["count"]
            )
            rows = connection.execute(
                f"select {SUMMARY_COLUMNS} from {self.RELATION}"
                f"{where_sql}{order_sql} "
                "offset %s limit %s",
                [*parameters, query.offset, query.limit],
            ).fetchall()
        return ListingPage(rows=[dict(row) for row in rows], total=total)

    def query_map_markers(self, query: ListingQuery) -> list[dict[str, Any]]:
        """Return every filtered marker candidate in one fail-closed query."""
        where_sql, parameters = _postgres_where(query)
        visibility_clause = (
            "location_map_visible and latitude is not null and longitude is not null"
        )
        where_sql = (
            f"{where_sql} and {visibility_clause}"
            if where_sql
            else f" where {visibility_clause}"
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"select {MAP_MARKER_COLUMNS} from {self.RELATION}"
                f"{where_sql} order by listing_id",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]


class SupabaseListingRepository:
    """Lazy Supabase adapter; constructing the FastAPI app performs no I/O."""

    PAGE_SIZE = 500

    def __init__(self, url: str, service_role_key: str, relation: str) -> None:
        from supabase import create_client

        self._client = create_client(url, service_role_key)
        self._relation = relation

    @classmethod
    def from_environment(cls) -> "SupabaseListingRepository":
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are not configured")
        return cls(
            url,
            key,
            os.getenv("HOUSING_LISTINGS_RELATION") or "product_housing_listings",
        )

    def list_summaries(self, *, limit: int = 3000) -> list[dict[str, Any]]:
        listings: list[dict[str, Any]] = []
        while len(listings) < limit:
            page_size = min(self.PAGE_SIZE, limit - len(listings))
            start = len(listings)
            page = (
                self._client.table(self._relation)
                .select(SUMMARY_COLUMNS)
                .order("listing_id")
                .range(start, start + page_size - 1)
                .execute()
            ).data
            listings.extend(page)
            if len(page) < page_size:
                break
        return listings

    def get_detail(self, listing_id: str) -> dict[str, Any] | None:
        rows = (
            self._client.table(self._relation)
            .select("*")
            .eq("listing_id", listing_id)
            .limit(1)
            .execute()
        ).data
        return rows[0] if rows else None

    def list_observations(
        self, listing_id: str, *, limit: int = 51
    ) -> list[dict[str, Any]]:
        identities = (
            self._client.table("housing_listings")
            .select("id")
            .eq("source", "uwo_offcampus")
            .eq("source_listing_id", listing_id)
            .limit(1)
            .execute()
        ).data
        if not identities:
            return []
        response = (
            self._client.table("housing_listing_observations")
            .select(HISTORY_OBSERVATION_COLUMNS, count="exact")
            .eq("listing_id", identities[0]["id"])
            .order("observed_at", desc=True)
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        total = response.count if response.count is not None else len(response.data)
        return [
            {**row, "total_observation_count": total} for row in response.data
        ]

    @staticmethod
    def _apply_query_filters(request: Any, query: ListingQuery) -> Any:
        if query.listing_ids:
            request = request.in_("listing_id", list(query.listing_ids))
        if query.max_walk_minutes is not None or query.max_transit_minutes is not None:
            request = request.eq("location_route_available", True)
        for column, value in (
            ("housing_type", query.housing_type),
            ("lease_type", query.lease_type),
            ("preferred_gender", query.preferred_gender),
            ("pet_policy", query.pet_policy),
            ("data_quality_status", query.data_quality),
        ):
            if value:
                request = request.ilike(column, value)
        for column, value in (
            ("bedrooms", query.bedrooms),
            ("is_sublet", query.is_sublet),
            ("furnished", query.furnished),
            ("utilities_included", query.utilities_included),
            ("parking_available", query.parking_available),
            ("laundry", query.laundry),
            ("location_map_visible", query.map_ready),
            ("ranking_status", query.ranking_status),
        ):
            if value is not None:
                request = request.eq(column, value)
        for column, value in (
            ("price_monthly", query.min_price),
            ("ranking_overall_score", query.min_score),
            ("ranking_value_score", query.min_value_score),
            ("ranking_campus_access_score", query.min_campus_access_score),
            ("ranking_transit_score", query.min_transit_score),
        ):
            if value is not None:
                request = request.gte(column, value)
        for column, value in (
            ("price_monthly", query.max_price),
            ("distance_to_western_km", query.max_distance_km),
            ("ranking_overall_score", query.max_score),
        ):
            if value is not None:
                request = request.lte(column, value)
        summer_categories = list(SUMMER_AVAILABILITY_CATEGORIES)
        non_summer_categories = list(NON_SUMMER_AVAILABILITY_CATEGORIES)
        if query.summer_available is True:
            request = request.in_("availability_category", summer_categories)
        elif query.summer_available is False:
            request = request.in_("availability_category", non_summer_categories)
        if query.search:
            value = query.search.strip().replace(",", " ")
            request = request.or_(
                ",".join(
                    f"{column}.ilike.%{value}%"
                    for column in ("title", "address", "housing_type", "lease_type")
                )
            )
        return request

    def _commute_property_ids(
        self, query: ListingQuery, *, mode: str, time_period: str | None
    ) -> set[int] | None:
        maximum = (
            query.max_walk_minutes if mode == "walking" else query.max_transit_minutes
        )
        if maximum is None:
            return None
        context = query.accessibility
        if context is None:
            return set()
        request = (
            self._client.table("housing_accessibility_profiles")
            .select(
                "id,origin_property_id,representative_duration_seconds,"
                "result_type,provider_metadata,calculated_at"
            )
            .eq("hotspot_id", context.hotspot_id)
            .eq("travel_mode", mode)
            .eq("provider", context.provider)
            .eq("provider_profile", context.provider_profile)
            .eq("is_stale", False)
            .or_(f"expires_at.is.null,expires_at.gt.{context.at.isoformat()}")
        )
        request = (
            request.is_("network_version", "null")
            if context.network_version is None
            else request.eq("network_version", context.network_version)
        )
        if time_period is None:
            request = request.is_("time_period", "null")
        else:
            request = request.eq("time_period", time_period)
            request = (
                request.is_("schedule_version", "null")
                if context.schedule_version is None
                else request.eq("schedule_version", context.schedule_version)
            )
        rows = (
            request.order("calculated_at", desc=True)
            .order("id", desc=True)
            .execute()
            .data
        )
        latest: dict[int, dict[str, Any]] = {}
        for row in rows:
            property_id = row.get("origin_property_id")
            if property_id is not None:
                latest.setdefault(int(property_id), row)
        accepted_types = {
            "exact_route",
            "cached_exact_property",
            "cached_exact_origin",
        }
        output = set()
        for property_id, row in latest.items():
            metadata = row.get("provider_metadata")
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            duration = row.get("representative_duration_seconds")
            if (
                row.get("result_type") in accepted_types
                and isinstance(metadata, dict)
                and metadata.get("quality_status") == "complete"
                and duration is not None
                and int(duration) <= maximum * 60
            ):
                output.add(property_id)
        return output

    def query_summaries(self, query: ListingQuery) -> ListingPage:
        property_sets = [
            values
            for values in (
                self._commute_property_ids(query, mode="walking", time_period=None),
                self._commute_property_ids(
                    query,
                    mode="transit",
                    time_period="weekday_morning_commute",
                ),
            )
            if values is not None
        ]
        if property_sets and not set.intersection(*property_sets):
            return ListingPage(rows=[], total=0)
        request = self._client.table(self._relation).select(
            SUMMARY_COLUMNS, count="exact"
        )
        request = self._apply_query_filters(request, query)
        if query.roommates_wanted:
            request = request.in_(
                "housing_type", ["house_to_share", "apartment_to_share"]
            )
        if property_sets:
            request = request.in_("property_id", sorted(set.intersection(*property_sets)))
        sort_column, _ = SORT_COLUMNS[query.sort]
        response = (
            request.order(
                sort_column,
                desc=query.order == "desc",
                nullsfirst=False,
            )
            .order("listing_id")
            .range(query.offset, query.offset + query.limit - 1)
            .execute()
        )
        return ListingPage(
            rows=[dict(row) for row in response.data],
            total=int(response.count or 0),
        )

    def query_map_markers(self, query: ListingQuery) -> list[dict[str, Any]]:
        """Return compact filtered markers without sidebar range pagination."""
        property_sets = [
            values
            for values in (
                self._commute_property_ids(query, mode="walking", time_period=None),
                self._commute_property_ids(
                    query,
                    mode="transit",
                    time_period="weekday_morning_commute",
                ),
            )
            if values is not None
        ]
        if property_sets and not set.intersection(*property_sets):
            return []
        request = self._client.table(self._relation).select(MAP_MARKER_COLUMNS)
        request = self._apply_query_filters(request, query).eq(
            "location_map_visible", True
        )
        if query.roommates_wanted:
            request = request.in_(
                "housing_type", ["house_to_share", "apartment_to_share"]
            )
        if property_sets:
            request = request.in_("property_id", sorted(set.intersection(*property_sets)))
        rows = request.order("listing_id").limit(3000).execute().data
        return [dict(row) for row in rows]
