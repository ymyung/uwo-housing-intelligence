from pathlib import Path

from scripts.check_migrations import check_migrations


MIGRATIONS = Path("supabase/migrations")
MIGRATION = MIGRATIONS / "20260816000100_create_london_mobility_reference_data.sql"


def test_mobility_reference_migration_precedes_coordinate_promotion() -> None:
    migrations = check_migrations(MIGRATIONS)
    promotion = (
        MIGRATIONS / "20260825000100_create_coordinate_promotion_store.sql"
    )

    assert MIGRATION in migrations
    assert promotion in migrations
    assert migrations.index(MIGRATION) < migrations.index(promotion)


def test_mobility_reference_schema_is_versioned_spatial_and_shadow_only() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for table in (
        "london_bicycle_routes", "london_recreation_paths", "london_sidewalks",
        "london_walkways", "london_parks", "london_pedestrian_crossovers",
        "london_signalized_intersections",
    ):
        assert f"reference_data.{table}" in sql
        assert f"{table}_geometry_gix" in sql
        assert f"{table}_run_source_idx" in sql
    assert "reference_data.property_reference_matches" not in sql
    assert "housing_listing_scores" not in sql
    assert "shadow reference evidence" in sql
