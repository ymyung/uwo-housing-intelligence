from pathlib import Path

from scripts.check_migrations import check_migrations


MIGRATIONS = Path("supabase/migrations")
MIGRATION = MIGRATIONS / "20260812000100_create_location_visibility_contract.sql"


def migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_location_visibility_migration_is_ordered_and_fail_closed() -> None:
    assert MIGRATION in check_migrations(MIGRATIONS)
    sql = migration_sql()
    assert "create table public.housing_property_location_visibility" in sql
    assert "create view public.product_housing_listings" in sql
    assert "coalesce(visibility.location_status, 'unavailable')" in sql
    assert "coalesce(visibility.map_visible, false)" in sql
    assert "coalesce(visibility.route_available, false)" in sql


def test_location_visibility_is_versioned_historical_and_credential_free() -> None:
    sql = migration_sql()
    assert "policy_version text not null" in sql
    assert "source_fingerprint char(64) not null" in sql
    assert "is_current boolean not null default true" in sql
    assert "housing_property_location_visibility_current_uidx" in sql
    assert "housing_property_location_visibility_history_idx" in sql
    assert "enable row level security" in sql
    assert "postgresql://" not in sql
