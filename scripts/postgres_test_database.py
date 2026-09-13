"""Narrow reset/apply helpers for the disposable Stage 4 test database."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Iterable
from typing import Any

from scripts.postgres_test_safety import (
    TestDatabaseTarget,
    verify_connected_test_database,
)


STAGE4_DROP_STATEMENTS = (
    "drop schema if exists reference_data cascade",
    "drop view if exists public.product_housing_listings",
    "drop view if exists public.ranked_housing_listings",
    "drop view if exists public.active_housing_listings",
    "drop table if exists public.housing_coordinate_promotion_items",
    "drop table if exists public.housing_coordinate_promotion_runs",
    "drop table if exists public.housing_listing_scores",
    "drop table if exists public.housing_ranking_runs",
    "drop table if exists public.housing_property_location_visibility",
    "drop table if exists public.housing_walk_time_surfaces",
    "drop table if exists public.housing_accessibility_reuse_history",
    "drop table if exists public.housing_accessibility_samples",
    "drop table if exists public.housing_accessibility_profiles",
    "drop table if exists public.housing_review_items",
    "drop table if exists public.housing_listing_observations",
    "drop table if exists public.housing_listings",
    "drop table if exists public.housing_properties",
    "drop table if exists public.housing_geocode_results",
    "drop table if exists public.housing_pipeline_runs",
)


def reset_stage4_objects(
    connection: Any, target: TestDatabaseTarget
) -> None:
    """Drop only the known Stage 4 relations after re-verifying the target."""

    verify_connected_test_database(connection, target)
    for statement in STAGE4_DROP_STATEMENTS:
        connection.execute(statement)


def apply_stage4_migration(
    connection: Any,
    target: TestDatabaseTarget,
    migration_paths: Path | Iterable[Path],
) -> None:
    """Execute the ordered repository migrations against a disposable database."""

    verify_connected_test_database(connection, target)
    paths = (migration_paths,) if isinstance(migration_paths, Path) else migration_paths
    for migration_path in paths:
        connection.execute(migration_path.read_text(encoding="utf-8"))
