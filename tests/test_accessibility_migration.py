from pathlib import Path

from scripts.check_migrations import check_migrations

MIGRATIONS = Path("supabase/migrations")
ACCESSIBILITY_MIGRATION = MIGRATIONS / "20260804000100_create_accessibility_profile_store.sql"
RANKING_MIGRATION = MIGRATIONS / "20260809000100_create_listing_ranking_store.sql"
ROUTE_MIGRATION = MIGRATIONS / "20260809000200_add_accessibility_route_itineraries.sql"


def migration_sql() -> str:
    return ACCESSIBILITY_MIGRATION.read_text(encoding="utf-8").lower()


def test_accessibility_migration_is_ordered_and_structurally_valid() -> None:
    checked = check_migrations(MIGRATIONS)
    assert ACCESSIBILITY_MIGRATION in checked
    assert checked[checked.index(RANKING_MIGRATION):checked.index(ROUTE_MIGRATION) + 1] == [RANKING_MIGRATION, ROUTE_MIGRATION]


def test_route_itinerary_migration_is_normalized_and_does_not_store_raw_otp() -> None:
    sql = ROUTE_MIGRATION.read_text(encoding="utf-8").lower()
    assert "representative_sample_departure_at timestamptz" in sql
    assert sql.count("add column route_itinerary jsonb") == 2
    assert "jsonb_typeof(route_itinerary) = 'object'" in sql
    assert "raw_otp" not in sql


def test_accessibility_schema_is_property_origin_based_and_retained() -> None:
    sql = migration_sql()
    assert "create table public.housing_accessibility_profiles" in sql
    assert "origin_property_id bigint references public.housing_properties(id)" in sql
    assert "listing_id bigint not null" not in sql
    assert "on delete cascade" not in sql
    assert "source_profile_id bigint references public.housing_accessibility_profiles(id)" in sql
    assert "is_stale boolean not null default false" in sql


def test_accessibility_samples_and_audit_history_are_normalized() -> None:
    sql = migration_sql()
    assert "create table public.housing_accessibility_samples" in sql
    assert "unique (profile_id, departure_at)" in sql
    assert "create table public.housing_accessibility_reuse_history" in sql
    assert "reuse_reason text not null" in sql
    assert "request_metadata jsonb" in sql


def test_cache_identity_and_targeted_indexes_exist_without_postgis() -> None:
    sql = migration_sql()
    assert "housing_accessibility_current_cache_uidx" in sql
    assert "housing_accessibility_property_lookup_idx" in sql
    assert "housing_accessibility_origin_lookup_idx" in sql
    assert "housing_accessibility_expiry_idx" in sql
    assert "housing_accessibility_provider_version_idx" in sql
    assert "housing_accessibility_stop_reuse_idx" in sql
    assert "create extension" not in sql
    assert "geometry(" not in sql
    assert "geography(" not in sql


def test_profile_constraints_preserve_nulls_and_result_types() -> None:
    sql = migration_sql()
    for result_type in (
        "exact_route",
        "cached_exact_property",
        "cached_exact_origin",
        "same_stop_reuse",
        "nearby_origin_estimate",
        "straight_line_fallback",
        "pending_provider",
        "unavailable",
        "stale",
    ):
        assert f"'{result_type}'" in sql
    assert "duration_seconds integer" in sql
    assert "duration_seconds integer not null default 0" not in sql


def test_product_view_exposes_durable_property_identity() -> None:
    sql = migration_sql()
    assert "create or replace view public.active_housing_listings" in sql
    assert "l.property_id" in sql
    assert "p.match_key as property_match_key" in sql
