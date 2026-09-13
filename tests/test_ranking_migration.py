from pathlib import Path

from scripts.check_migrations import check_migrations


MIGRATIONS = Path("supabase/migrations")
RANKING_MIGRATION = MIGRATIONS / "20260809000100_create_listing_ranking_store.sql"
ROUTE_MIGRATION = MIGRATIONS / "20260809000200_add_accessibility_route_itineraries.sql"


def migration_sql() -> str:
    return RANKING_MIGRATION.read_text(encoding="utf-8").lower()


def test_ranking_migration_remains_ordered_and_structurally_valid() -> None:
    checked = check_migrations(MIGRATIONS)
    assert RANKING_MIGRATION in checked
    assert checked.index(RANKING_MIGRATION) < checked.index(ROUTE_MIGRATION)


def test_ranking_schema_is_listing_level_versioned_and_historical() -> None:
    sql = migration_sql()
    assert "create table public.housing_ranking_runs" in sql
    assert "create table public.housing_listing_scores" in sql
    assert "listing_id bigint not null references public.housing_listings(id)" in sql
    assert "ranking_version text not null" in sql
    assert "input_fingerprint char(64) not null" in sql
    assert "is_current boolean not null default true" in sql
    assert "on delete cascade" not in sql
    assert "create view public.ranked_housing_listings" in sql
    assert "score.ranking_version = 'ranking-v1'" in sql
    assert "score.is_current" in sql


def test_ranking_scores_preserve_null_components_and_explanations() -> None:
    sql = migration_sql()
    assert "overall_score numeric(5, 2)" in sql
    assert "amenity_score numeric(5, 2)" in sql
    assert "amenity_score numeric(5, 2) not null" not in sql
    assert "explanation jsonb not null" in sql
    assert "ranking_status in ('ranked', 'partial', 'excluded')" in sql
    assert "ranking_status <> 'ranked' and overall_score is null" in sql


def test_ranking_indexes_support_idempotency_sorting_and_history() -> None:
    sql = migration_sql()
    assert "housing_listing_scores_current_uidx" in sql
    assert "where is_current" in sql
    assert "housing_listing_scores_ranked_sort_idx" in sql
    assert "overall_score desc" in sql
    assert "housing_listing_scores_status_idx" in sql
    assert "housing_listing_scores_history_idx" in sql


def test_ranking_tables_use_rls_without_credentials_or_extensions() -> None:
    sql = migration_sql()
    assert "alter table public.housing_ranking_runs enable row level security" in sql
    assert "alter table public.housing_listing_scores enable row level security" in sql
    assert "postgresql://" not in sql
    assert "create extension" not in sql
