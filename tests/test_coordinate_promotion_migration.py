from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/20260825000100_create_coordinate_promotion_store.sql"
)


def test_coordinate_promotion_store_is_versioned_auditable_and_fail_closed() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").casefold()

    assert "create table public.housing_coordinate_promotion_runs" in sql
    assert "create table public.housing_coordinate_promotion_items" in sql
    assert "before_state_fingerprint char(64) not null" in sql
    assert "backup_sha256 char(64) not null" in sql
    assert "previous_property_state jsonb not null" in sql
    assert "previous_map_projections jsonb not null" in sql
    assert "previous_visibility_state jsonb" in sql
    assert "rollback_of_run_id" in sql
    assert "superseded_by_run_id" in sql
    assert "'cutover_completed'" in sql
    assert "'recomputing'" in sql
    assert "'completed'" in sql
    assert "movement_meters >= 0 and movement_meters <= 100" in sql
    assert "enable row level security" in sql


def test_coordinate_promotion_migration_contains_no_external_credentials() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").casefold()

    assert "postgresql://" not in sql
    assert "password" not in sql
    assert "api_key" not in sql
