from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.postgres_test_database import (
    apply_stage4_migration,
    reset_stage4_objects,
)
from backend.accessibility import AccessibilityResolver
from backend.accessibility_repository import PostgresAccessibilityRepository
from backend.domain import (
    AccessibilityOrigin,
    AccessibilityProfile,
    AccessibilityRequest,
    AccessibilityResultType,
    Coordinates,
    DayType,
    OriginType,
    TimePeriod,
    TravelMode,
)
from backend.hotspots import DEFAULT_HOTSPOTS


STAGE4_MIGRATIONS = tuple(
    sorted(
        (Path(__file__).resolve().parents[1] / "supabase" / "migrations").glob(
            "*.sql"
        )
    )
)


pytestmark = pytest.mark.postgres

TABLES = {
    "housing_pipeline_runs",
    "housing_geocode_results",
    "housing_properties",
    "housing_listings",
    "housing_listing_observations",
    "housing_review_items",
    "housing_accessibility_profiles",
    "housing_accessibility_samples",
    "housing_accessibility_reuse_history",
    "housing_ranking_runs",
    "housing_listing_scores",
    "housing_walk_time_surfaces",
    "housing_property_location_visibility",
    "housing_coordinate_promotion_runs",
    "housing_coordinate_promotion_items",
}

REQUIRED_INDEXES = {
    "housing_geocode_provider_result_uidx",
    "housing_properties_match_key_uidx",
    "housing_pipeline_runs_lifecycle_latest_idx",
    "housing_listings_status_idx",
    "housing_listings_active_idx",
    "housing_listings_property_idx",
    "housing_observations_run_idx",
    "housing_observations_listing_latest_idx",
    "housing_observations_monthly_price_idx",
    "housing_observations_bedrooms_idx",
    "housing_observations_housing_type_idx",
    "housing_observations_map_ready_idx",
    "housing_properties_normalized_address_idx",
    "housing_review_items_open_idx",
    "housing_review_items_type_severity_idx",
    "housing_accessibility_current_cache_uidx",
    "housing_accessibility_property_lookup_idx",
    "housing_accessibility_origin_lookup_idx",
    "housing_accessibility_zone_lookup_idx",
    "housing_accessibility_hotspot_mode_idx",
    "housing_accessibility_expiry_idx",
    "housing_accessibility_provider_version_idx",
    "housing_accessibility_stop_reuse_idx",
    "housing_accessibility_samples_profile_idx",
    "housing_accessibility_reuse_property_idx",
    "housing_accessibility_reuse_source_idx",
    "housing_listing_scores_current_uidx",
    "housing_listing_scores_ranked_sort_idx",
    "housing_listing_scores_status_idx",
    "housing_listing_scores_history_idx",
    "housing_ranking_runs_version_idx",
    "housing_walk_surface_property_current_idx",
    "housing_walk_surface_status_idx",
    "housing_walk_surface_routing_idx",
    "housing_property_location_visibility_current_uidx",
    "housing_property_location_visibility_status_idx",
    "housing_property_location_visibility_history_idx",
    "housing_coordinate_promotion_items_property_idx",
    "housing_coordinate_promotion_runs_status_idx",
}


def test_real_migration_creates_relations_constraints_and_indexes(
    postgres_database,
) -> None:
    connection = postgres_database
    table_names = {
        row[0]
        for row in connection.execute(
            """
            select tablename from pg_catalog.pg_tables
            where schemaname = 'public' and tablename like 'housing_%'
            """
        )
    }
    assert table_names == TABLES
    assert connection.execute(
        "select to_regclass('public.active_housing_listings')::text"
    ).fetchone()[0] == "active_housing_listings"
    assert connection.execute(
        "select to_regclass('public.ranked_housing_listings')::text"
    ).fetchone()[0] == "ranked_housing_listings"
    assert connection.execute(
        "select to_regclass('public.product_housing_listings')::text"
    ).fetchone()[0] == "product_housing_listings"

    constraints = list(
        connection.execute(
            """
            select c.contype, t.relname, pg_get_constraintdef(c.oid)
            from pg_catalog.pg_constraint c
            join pg_catalog.pg_class t on t.oid = c.conrelid
            join pg_catalog.pg_namespace n on n.oid = t.relnamespace
            where n.nspname = 'public' and t.relname = any(%s)
            """,
            (list(TABLES),),
        )
    )
    assert {table for kind, table, _ in constraints if kind == "p"} == TABLES
    assert sum(kind == "f" for kind, _, _ in constraints) == 24
    assert sum(kind == "u" for kind, _, _ in constraints) == 10
    assert all(any(kind == "c" and table == name for kind, table, _ in constraints)
               for name in TABLES)

    index_names = {
        row[0]
        for row in connection.execute(
            """
            select indexname from pg_catalog.pg_indexes
            where schemaname = 'public'
            """
        )
    }
    assert REQUIRED_INDEXES <= index_names


def test_real_migration_uses_expected_types_rls_and_security_invoker_view(
    postgres_database,
) -> None:
    connection = postgres_database
    jsonb_columns = {
        (row[0], row[1])
        for row in connection.execute(
            """
            select table_name, column_name
            from information_schema.columns
            where table_schema = 'public' and data_type = 'jsonb'
              and table_name = any(%s)
            """,
            (list(TABLES),),
        )
    }
    expected_jsonb = {
        ("housing_pipeline_runs", "manifest_json"),
        ("housing_pipeline_runs", "change_summary"),
        ("housing_pipeline_runs", "import_configuration"),
        ("housing_geocode_results", "raw_provider_data"),
        ("housing_listing_observations", "amenities"),
        ("housing_listing_observations", "raw_data"),
        ("housing_listing_observations", "provenance_data"),
        ("housing_listing_observations", "confidence_data"),
        ("housing_listing_observations", "comparison_data"),
        ("housing_listing_observations", "changed_fields"),
        ("housing_review_items", "payload"),
        ("housing_accessibility_profiles", "provider_metadata"),
        ("housing_accessibility_profiles", "route_itinerary"),
        ("housing_accessibility_samples", "provider_metadata"),
        ("housing_accessibility_samples", "route_itinerary"),
        ("housing_accessibility_reuse_history", "request_metadata"),
        ("housing_ranking_runs", "summary"),
        ("housing_listing_scores", "explanation"),
        ("housing_property_location_visibility", "reason_codes"),
        ("housing_coordinate_promotion_runs", "summary"),
        ("housing_coordinate_promotion_runs", "recomputation_summary"),
        ("housing_coordinate_promotion_runs", "validation_summary"),
        ("housing_coordinate_promotion_items", "previous_property_state"),
        ("housing_coordinate_promotion_items", "previous_map_projections"),
        ("housing_coordinate_promotion_items", "previous_visibility_state"),
        ("housing_coordinate_promotion_items", "new_coordinate"),
    }
    assert expected_jsonb <= jsonb_columns

    numeric_types = {
        (row[0], row[1]): row[2]
        for row in connection.execute(
            """
            select table_name, column_name, data_type
            from information_schema.columns
            where table_schema = 'public'
              and table_name = any(%s)
              and column_name = any(%s)
            """,
            (
                list(TABLES),
                [
                    "latitude", "longitude", "confidence", "geocode_confidence",
                    "price_numeric", "price_monthly", "bathrooms",
                ],
            ),
        )
    }
    assert numeric_types[("housing_geocode_results", "latitude")] == "double precision"
    assert numeric_types[("housing_geocode_results", "longitude")] == "double precision"
    assert numeric_types[("housing_geocode_results", "confidence")] == "double precision"
    assert numeric_types[("housing_properties", "geocode_confidence")] == "double precision"
    assert numeric_types[("housing_listing_observations", "price_numeric")] == "numeric"
    assert numeric_types[("housing_listing_observations", "price_monthly")] == "numeric"
    assert numeric_types[("housing_listing_observations", "bathrooms")] == "numeric"

    rls_tables = {
        row[0]
        for row in connection.execute(
            """
            select c.relname
            from pg_catalog.pg_class c
            join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public' and c.relname = any(%s)
              and c.relrowsecurity
            """,
            (list(TABLES),),
        )
    }
    assert rls_tables == TABLES
    view_options = connection.execute(
        """
        select reloptions from pg_catalog.pg_class
        where oid = 'public.active_housing_listings'::regclass
        """
    ).fetchone()[0]
    assert "security_invoker=true" in view_options


def test_compatibility_view_is_queryable_before_fixture_import(
    postgres_database,
) -> None:
    cursor = postgres_database.execute(
        "select * from public.active_housing_listings"
    )
    assert cursor.fetchall() == []
    column_names = {description.name for description in cursor.description}
    assert {
        "listing_id", "source_listing_id", "listing_url", "title", "description",
        "address", "price_numeric", "price_monthly", "price_text", "price_period",
        "bedrooms", "housing_type", "utilities_included", "lease_type",
        "lease_term_months", "is_sublet", "furnished", "bathrooms",
        "bathroom_type", "amenities", "latitude", "longitude", "map_ready",
        "distance_to_western_km", "listing_status", "first_seen_at", "last_seen_at",
        "property_id", "property_match_key",
    } <= column_names


def test_real_migration_does_not_require_or_destroy_legacy_listings(
    postgres_database, postgres_target
) -> None:
    connection = postgres_database
    legacy_oid = connection.execute(
        "select to_regclass('public.listings')::oid"
    ).fetchone()[0]
    created_fixture = legacy_oid is None
    if created_fixture:
        connection.execute(
            "create table public.listings (id integer primary key, marker text not null)"
        )
        connection.execute(
            "insert into public.listings (id, marker) values (1, 'legacy fixture')"
        )
        legacy_oid = connection.execute(
            "select 'public.listings'::regclass::oid"
        ).fetchone()[0]
    try:
        reset_stage4_objects(connection, postgres_target)
        apply_stage4_migration(connection, postgres_target, STAGE4_MIGRATIONS)
        assert connection.execute(
            "select 'public.listings'::regclass::oid"
        ).fetchone()[0] == legacy_oid
        if created_fixture:
            assert connection.execute(
                "select marker from public.listings where id = 1"
            ).fetchone()[0] == "legacy fixture"
    finally:
        if created_fixture:
            connection.execute("drop table public.listings")


def test_postgresql_numeric_driver_type_is_decimal(postgres_database) -> None:
    value = postgres_database.execute("select 800.25::numeric(12, 2)").fetchone()[0]
    assert value == Decimal("800.25")
    assert isinstance(value, Decimal)


def test_property_accessibility_profiles_restrict_property_deletion(
    postgres_database,
) -> None:
    import psycopg

    property_id = postgres_database.execute(
        "insert into public.housing_properties (address_complete) values (false) returning id"
    ).fetchone()[0]
    profile_id = postgres_database.execute(
        """
        insert into public.housing_accessibility_profiles (
            cache_identity, origin_key, origin_type, origin_property_id,
            origin_latitude, origin_longitude, hotspot_id, travel_mode,
            representative_duration_seconds, provider, provider_profile,
            result_type, confidence, sample_count, calculated_at, expires_at
        ) values (%s,%s,'property',%s,43.0,-81.25,'western-main-campus',
                  'walking',1800,'fixture','v1','exact_route',0.95,1,now(),
                  now() + interval '30 days')
        returning id
        """,
        ("a" * 64, f"property:{property_id}", property_id),
    ).fetchone()[0]

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        postgres_database.execute(
            "delete from public.housing_properties where id = %s", (property_id,)
        )

    postgres_database.execute(
        "update public.housing_accessibility_profiles set is_stale = true where id = %s",
        (profile_id,),
    )
    assert postgres_database.execute(
        "select is_stale from public.housing_accessibility_profiles where id = %s",
        (profile_id,),
    ).fetchone()[0] is True


def _insert_pipeline_run(connection, run_id: str) -> int:
    return connection.execute(
        """
        insert into public.housing_pipeline_runs (
            run_id, source, status, started_at, manifest_json,
            canonical_sha256, manifest_sha256
        ) values (%s, 'uwo_offcampus', 'completed', now(), '{}'::jsonb, %s, %s)
        returning id
        """,
        (run_id, "c" * 64, "d" * 64),
    ).fetchone()[0]


def _insert_property(connection, match_key: str, latitude: float, longitude: float) -> int:
    return connection.execute(
        """
        insert into public.housing_properties (
            normalized_address, display_address, latitude, longitude,
            geocode_status, address_complete, match_key
        ) values (%s, %s, %s, %s, 'ok', true, %s)
        returning id
        """,
        (match_key, match_key, latitude, longitude, match_key),
    ).fetchone()[0]


def _profile(
    property_id: int,
    *,
    latitude: float = 43.0,
    longitude: float = -81.25,
    mode: TravelMode = TravelMode.WALKING,
    nearest_stop_id: str | None = None,
    walking_to_stop_seconds: int | None = None,
) -> AccessibilityProfile:
    transit = mode is TravelMode.TRANSIT
    now = datetime.now(timezone.utc)
    return AccessibilityProfile(
        profile_id=None,
        origin=AccessibilityOrigin(
            coordinates=Coordinates(latitude, longitude),
            property_id=property_id,
            origin_type=OriginType.PROPERTY,
            nearest_stop_id=nearest_stop_id,
            walking_to_stop_seconds=walking_to_stop_seconds,
        ),
        hotspot_id="western-main-campus",
        travel_mode=mode,
        day_type=DayType.WEEKDAY if transit else None,
        time_period=TimePeriod.WEEKDAY_MORNING_COMMUTE if transit else None,
        representative_duration_seconds=1500,
        minimum_duration_seconds=1260,
        maximum_duration_seconds=1740,
        distance_meters=5200 if transit else 2300,
        walking_duration_seconds=walking_to_stop_seconds if transit else 1680,
        transfer_count=1 if transit else None,
        nearest_stop_id=nearest_stop_id,
        stop_to_destination_seconds=1140 if transit else None,
        provider="postgres-fixture",
        provider_profile="v1",
        result_type=AccessibilityResultType.EXACT_ROUTE,
        confidence=0.95,
        sample_count=3 if transit else 1,
        calculated_at=now,
        schedule_version="fixture-schedule-v1" if transit else None,
        network_version="fixture-network-v1",
        expires_at=now + timedelta(days=30),
    )


def _request(
    property_id: int,
    *,
    latitude: float,
    longitude: float,
    mode: TravelMode,
    nearest_stop_id: str | None = None,
    walking_to_stop_seconds: int | None = None,
    schedule_version: str | None = None,
) -> AccessibilityRequest:
    return AccessibilityRequest(
        origin=AccessibilityOrigin(
            coordinates=Coordinates(latitude, longitude),
            property_id=property_id,
            origin_type=OriginType.PROPERTY,
            nearest_stop_id=nearest_stop_id,
            walking_to_stop_seconds=walking_to_stop_seconds,
        ),
        hotspot=DEFAULT_HOTSPOTS[0],
        travel_mode=mode,
        time_period=(
            TimePeriod.WEEKDAY_MORNING_COMMUTE
            if mode is TravelMode.TRANSIT
            else None
        ),
        provider="postgres-fixture",
        provider_profile="v1",
        requested_at=datetime.now(timezone.utc),
        schedule_version=schedule_version,
        network_version="fixture-network-v1",
    )


def test_inactive_listing_retains_property_profile_and_later_listing_reuses_it(
    postgres_database,
    postgres_target,
) -> None:
    connection = postgres_database
    pipeline_run_id = _insert_pipeline_run(connection, "accessibility-lifecycle")
    property_id = _insert_property(connection, "fixture-property-retained", 43.0, -81.25)
    first_listing_id = connection.execute(
        """
        insert into public.housing_listings (
            source, source_listing_id, source_url, property_id,
            first_seen_pipeline_run_id, last_seen_pipeline_run_id
        ) values ('uwo_offcampus', 'fixture-old', 'https://example.invalid/old',
                  %s, %s, %s)
        returning id
        """,
        (property_id, pipeline_run_id, pipeline_run_id),
    ).fetchone()[0]

    repository = PostgresAccessibilityRepository(postgres_target.url)
    first_profile = repository.save_profile(_profile(property_id))
    repeated_profile = repository.save_profile(_profile(property_id))
    assert repeated_profile.profile_id == first_profile.profile_id
    assert connection.execute(
        "select count(*) from public.housing_accessibility_profiles where not is_stale"
    ).fetchone()[0] == 1

    connection.execute(
        "update public.housing_listings set status = 'removed', removed_at = now() where id = %s",
        (first_listing_id,),
    )
    assert connection.execute(
        "select count(*) from public.housing_properties where id = %s", (property_id,)
    ).fetchone()[0] == 1
    assert connection.execute(
        "select count(*) from public.housing_accessibility_profiles where id = %s",
        (first_profile.profile_id,),
    ).fetchone()[0] == 1

    connection.execute(
        """
        insert into public.housing_listings (
            source, source_listing_id, source_url, property_id,
            first_seen_pipeline_run_id, last_seen_pipeline_run_id
        ) values ('uwo_offcampus', 'fixture-later', 'https://example.invalid/later',
                  %s, %s, %s)
        """,
        (property_id, pipeline_run_id, pipeline_run_id),
    )
    decision = AccessibilityResolver(repository).resolve(
        _request(
            property_id,
            latitude=43.0,
            longitude=-81.25,
            mode=TravelMode.WALKING,
        ),
        listing_id="fixture-later",
    )
    assert decision.result_type is AccessibilityResultType.CACHED_EXACT_PROPERTY
    audit = connection.execute(
        """
        select requested_source_listing_id, requested_property_id, result_type,
               source_profile_id, resolved_profile_id, reuse_reason
        from public.housing_accessibility_reuse_history
        """
    ).fetchone()
    assert audit[:3] == (
        "fixture-later",
        property_id,
        "cached_exact_property",
    )
    assert audit[4] == first_profile.profile_id
    assert audit[5]

    assert repository.invalidate_stale(datetime.now(timezone.utc) + timedelta(days=31)) == 1
    stale_decision = AccessibilityResolver(repository).resolve(
        _request(
            property_id,
            latitude=43.0,
            longitude=-81.25,
            mode=TravelMode.WALKING,
        ),
        record_history=False,
    )
    assert stale_decision.result_type is AccessibilityResultType.STALE
    assert connection.execute(
        "select count(*) from public.housing_accessibility_profiles where is_stale"
    ).fetchone()[0] == 1


def test_postgres_reuse_requires_safe_mode_specific_evidence(
    postgres_database,
    postgres_target,
) -> None:
    connection = postgres_database
    source_property = _insert_property(connection, "fixture-source", 43.0, -81.25)
    nearby_property = _insert_property(connection, "fixture-nearby", 43.00045, -81.25)
    transit_property = _insert_property(connection, "fixture-transit", 43.0015, -81.25)
    repository = PostgresAccessibilityRepository(postgres_target.url)
    walking = repository.save_profile(_profile(source_property))
    transit = repository.save_profile(
        _profile(
            source_property,
            mode=TravelMode.TRANSIT,
            nearest_stop_id="fixture-stop",
            walking_to_stop_seconds=360,
        )
    )

    nearby = AccessibilityResolver(repository).resolve(
        _request(
            nearby_property,
            latitude=43.00045,
            longitude=-81.25,
            mode=TravelMode.WALKING,
        )
    )
    assert nearby.result_type is AccessibilityResultType.NEARBY_ORIGIN_ESTIMATE
    assert nearby.result.source_profile_id == walking.profile_id

    same_stop = AccessibilityResolver(repository).resolve(
        _request(
            transit_property,
            latitude=43.0015,
            longitude=-81.25,
            mode=TravelMode.TRANSIT,
            nearest_stop_id="fixture-stop",
            walking_to_stop_seconds=480,
            schedule_version="fixture-schedule-v1",
        )
    )
    assert same_stop.result_type is AccessibilityResultType.SAME_STOP_REUSE
    assert same_stop.result.source_profile_id == transit.profile_id

    close_but_different_stop = AccessibilityResolver(repository).resolve(
        _request(
            transit_property,
            latitude=43.00045,
            longitude=-81.25,
            mode=TravelMode.TRANSIT,
            nearest_stop_id="different-stop",
            walking_to_stop_seconds=480,
            schedule_version="fixture-schedule-v1",
        ),
        record_history=False,
    )
    assert close_but_different_stop.result_type is AccessibilityResultType.PENDING_PROVIDER

    wrong_schedule = AccessibilityResolver(repository).resolve(
        _request(
            transit_property,
            latitude=43.0015,
            longitude=-81.25,
            mode=TravelMode.TRANSIT,
            nearest_stop_id="fixture-stop",
            walking_to_stop_seconds=480,
            schedule_version="different-schedule",
        ),
        record_history=False,
    )
    assert wrong_schedule.result_type is AccessibilityResultType.PENDING_PROVIDER


def test_accessibility_index_definitions_are_inspectable(postgres_database) -> None:
    rows = postgres_database.execute(
        """
        select tablename, indexname, indexdef
        from pg_indexes
        where tablename like 'housing_accessibility%'
        order by tablename, indexname
        """
    ).fetchall()
    definitions = {indexname: indexdef for _, indexname, indexdef in rows}
    assert REQUIRED_INDEXES & definitions.keys() == {
        name for name in REQUIRED_INDEXES if name.startswith("housing_accessibility")
    }
    assert "UNIQUE INDEX" in definitions["housing_accessibility_current_cache_uidx"]
    assert "WHERE (NOT is_stale)" in definitions["housing_accessibility_current_cache_uidx"]
    assert "origin_property_id" in definitions["housing_accessibility_property_lookup_idx"]
    assert "nearest_stop_id" in definitions["housing_accessibility_stop_reuse_idx"]
