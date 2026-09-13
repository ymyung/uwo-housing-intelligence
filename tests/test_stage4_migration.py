import ast
import re
from pathlib import Path

from scripts.check_migrations import check_migrations


MIGRATIONS = Path("supabase/migrations")


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.sql"))


def migration_sql() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in migration_files()).lower()


def test_migration_files_exist_are_timestamped_and_ordered() -> None:
    files = migration_files()
    assert files
    assert files == sorted(files)
    assert all(re.fullmatch(r"\d{14}_[a-z0-9_]+\.sql", path.name) for path in files)
    assert len({path.name[:14] for path in files}) == len(files)


def test_offline_migration_structural_checker_passes() -> None:
    assert check_migrations(MIGRATIONS) == migration_files()


def test_migration_creates_all_normalized_tables_and_compatibility_view() -> None:
    sql = migration_sql()
    for relation in (
        "housing_pipeline_runs",
        "housing_properties",
        "housing_listings",
        "housing_listing_observations",
        "housing_geocode_results",
        "housing_review_items",
    ):
        assert f"create table public.{relation}" in sql
    assert "view public.active_housing_listings" in sql


def test_migration_preserves_legacy_flat_listings_table() -> None:
    sql = migration_sql()
    forbidden = (
        "drop table public.listings",
        "alter table public.listings",
        "truncate table public.listings",
        "create table public.listings",
    )
    assert not any(statement in sql for statement in forbidden)


def test_listing_identity_and_observation_uniqueness_constraints_exist() -> None:
    sql = migration_sql()
    assert "unique (source, source_listing_id)" in sql
    assert "unique (listing_id, pipeline_run_id)" in sql
    assert "unique (normalized_query, provider)" in sql
    assert "unique" in sql and "match_key" in sql
    assert "dedupe_key char(64) not null unique" in sql


def test_required_foreign_keys_and_lifecycle_checks_exist() -> None:
    sql = migration_sql()
    assert sql.count("references public.housing_pipeline_runs(id)") >= 4
    assert sql.count("references public.housing_properties(id)") >= 3
    assert "references public.housing_listings(id)" in sql
    for status in ("active", "possibly_removed", "removed", "relisted"):
        assert f"'{status}'" in sql


def test_base_tables_are_service_role_only_until_rls_policies_are_added() -> None:
    sql = migration_sql()
    for relation in (
        "housing_pipeline_runs",
        "housing_properties",
        "housing_listings",
        "housing_listing_observations",
        "housing_geocode_results",
        "housing_review_items",
    ):
        assert f"alter table public.{relation} enable row level security" in sql


def test_database_checks_reject_inconsistent_map_and_geocode_rows() -> None:
    sql = migration_sql()
    assert "check (status <> 'ok' or latitude is not null)" in sql
    assert "not map_ready" in sql
    assert "geocode_status is not distinct from 'ok'" in sql
    assert "price_monthly is null or price_monthly >= 0" in sql
    assert "jsonb_typeof(import_configuration) = 'object'" in sql


def test_migration_has_targeted_query_indexes() -> None:
    sql = migration_sql()
    expected = (
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
    )
    assert all(index in sql for index in expected)


def test_compatibility_view_excludes_removed_and_uses_latest_observation() -> None:
    sql = migration_sql()
    assert "order by observation.observed_at desc, observation.id desc" in sql
    assert "where l.status in ('active', 'possibly_removed', 'relisted')" in sql
    assert "and l.source = 'uwo_offcampus'" in sql
    assert "l.source_listing_id as listing_id" in sql
    assert "o.price_monthly" in sql
    assert "security_invoker = true" in sql


def test_compatibility_view_covers_backend_column_contract() -> None:
    from backend.repository import BASE_SUMMARY_COLUMNS, RANKING_API_COLUMNS

    base_columns = {column.strip() for column in BASE_SUMMARY_COLUMNS.split(",")}
    ranking_columns = {column.strip() for column in RANKING_API_COLUMNS.split(",")}

    sql = migration_sql()
    view_start = sql.rindex("view public.active_housing_listings")
    view_start = sql.rfind("create", 0, view_start)
    view_end = sql.index("comment on view public.active_housing_listings", view_start)
    view_sql = sql[view_start:view_end]
    missing_base = {
        column
        for column in base_columns
        if not re.search(rf"(?:\.{re.escape(column)}\b|\bas {re.escape(column)}\b)", view_sql)
    }
    ranked_start = sql.index("create view public.ranked_housing_listings")
    ranked_end = sql.index("comment on view public.ranked_housing_listings", ranked_start)
    ranked_sql = sql[ranked_start:ranked_end]
    missing_ranking = {
        column
        for column in ranking_columns
        if not re.search(
            rf"(?:\.{re.escape(column)}\b|\bas {re.escape(column)}\b)",
            ranked_sql,
        )
    }
    assert missing_base == set()
    assert missing_ranking == set()
    assert "end as scraped_ok" in view_sql
    assert "null::double precision as transit_time_to_western_min" in view_sql
    assert "null::integer as transit_transfers" in view_sql


def test_migration_allows_only_the_reference_data_postgis_extension_and_no_credentials() -> None:
    sql = migration_sql()
    assert sql.count("create extension if not exists postgis") == 1
    assert "geometry(point, 26917)" in sql
    assert "postgresql://" not in sql
    assert "supabase_service_role_key" not in sql


def test_backend_uses_compatibility_view_and_monthly_price() -> None:
    api_source = Path("backend/main.py").read_text(encoding="utf-8")
    repository_source = Path("backend/repository.py").read_text(encoding="utf-8")
    assert '"public.product_housing_listings"' in repository_source
    assert '"product_housing_listings"' in repository_source
    assert 'row.get("price_monthly")' in api_source
    assert "price_monthly," in repository_source
    assert "PAGE_SIZE = 500" in repository_source
    assert ".range(" in repository_source


def test_environment_example_contains_blank_database_values_only() -> None:
    values = {}
    for line in Path(".env.example").read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        values[key] = value
    assert values["DATABASE_URL"] == ""
    assert values["HOUSING_LISTINGS_RELATION"] == ""
    assert all(value == "" for value in values.values())


def test_only_one_new_postgres_driver_is_declared() -> None:
    requirements = Path("requirements-database.txt").read_text(encoding="utf-8")
    assert "psycopg[binary]" in requirements
    assert "sqlalchemy" not in requirements.casefold()
    assert "asyncpg" not in requirements.casefold()


def test_repository_parameter_tuples_match_sql_placeholders() -> None:
    source = Path("pipeline/postgres_repository.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "execute":
            continue
        sql_argument, values_argument = node.args[:2]
        if not isinstance(sql_argument, ast.Constant) or not isinstance(
            sql_argument.value, str
        ):
            continue
        if not isinstance(values_argument, ast.Tuple):
            continue
        assert sql_argument.value.count("%s") == len(values_argument.elts)
        checked += 1
    assert checked >= 15


def test_repository_declares_transaction_and_advisory_lock_boundaries() -> None:
    source = Path("pipeline/postgres_repository.py").read_text(encoding="utf-8")
    assert "with connection.transaction():" in source
    assert "pg_advisory_xact_lock" in source
    assert "set import_status = 'completed'" in source
    assert "where run_id = %s and import_status <> 'completed'" in source
    assert "excluded.latitude is not null" in source
    assert "excluded.longitude is not null" in source
    assert "coalesce(excluded.confidence, -1)" in source
