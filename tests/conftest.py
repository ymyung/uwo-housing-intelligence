import csv
from datetime import datetime
import os
from pathlib import Path
from typing import Any

import pytest

from pipeline.run_context import RunContext
from pipeline.run_approval import approve_run, evaluate_run_approval
from scripts.postgres_test_database import (
    apply_stage4_migration,
    reset_stage4_objects,
)
from scripts.postgres_test_safety import (
    UnsafeTestDatabaseError,
    approve_test_database_environment,
    verify_connected_test_database,
)


STAGE4_MIGRATIONS = tuple(
    sorted(
        (Path(__file__).resolve().parents[1] / "supabase" / "migrations").glob(
            "*.sql"
        )
    )
)


def listing_row(listing_id: str = "1001", **updates: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "listing_id": listing_id,
        "listing_url": f"https://offcampus.uwo.ca/Listings/Details/{listing_id}",
        "title": f"Listing {listing_id}",
        "description": f"Description for {listing_id}",
        "address": "123 Richmond Street",
        "price_text": "$800 per month",
        "price_numeric": "800",
        "price_period": "month",
        "bedrooms": "1",
        "housing_type": "room",
        "utilities_included": "true",
        "lease_type": "standard",
        "lease_term_months": "12",
        "is_sublet": "false",
        "furnished": "true",
        "parking_available": "false",
        "parking_spaces": "0",
        "laundry": "true",
        "air_conditioning": "false",
        "dishwasher": "true",
        "bathrooms": "1",
        "bathroom_type": "shared",
        "amenities_list": '["Laundry", "Dishwasher"]',
        "latitude": "43.01",
        "longitude": "-81.27",
        "geocode_status": "ok",
        "geocode_confidence": "0.95",
        "geocode_match_type": "full_match",
        "geocode_result_type": "building",
        "geocode_formatted": "123 Richmond Street, London, ON, Canada",
        "geocode_city": "London",
        "geocode_postcode": "N6A 1A1",
        "geocode_country_code": "ca",
        "geocode_query": "123 Richmond Street, London, ON, Canada",
        "map_ready": "true",
        "distance_to_western_km": "2.5",
        "needs_manual_review": "false",
        "review_flags": "[]",
        "scraped_ok": "true",
    }
    row.update(updates)
    return row


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def make_stage4_run(tmp_path: Path):
    sequence = 0

    def make(
        rows: list[dict[str, Any]],
        *,
        run_id: str | None = None,
        canonical_approved: bool = True,
        root_status: str = "completed",
        timestamp: str | None = None,
        stage0_ids: list[str] | None = None,
        stage0_urls: list[str] | None = None,
        recorded_rows: int | None = None,
        stage1_limit: int | None = None,
        max_pages: int = 100,
        maximum_page_limit_reached: bool = False,
        stage0_warning: str | None = None,
    ) -> Path:
        nonlocal sequence
        sequence += 1
        selected_id = run_id or f"stage4-run-{sequence}"
        observed_at = timestamp or f"2026-07-{sequence:02d}T12:00:00Z"
        context = RunContext.create(tmp_path / "runs", run_id=selected_id)
        write_rows(context.paths.stage3_canonical, rows)

        if stage0_urls is None:
            ids = stage0_ids or [str(row["listing_id"]) for row in rows]
            stage0_urls = [
                f"https://offcampus.uwo.ca/Listings/Details/{listing_id}"
                for listing_id in ids
            ]
        write_rows(
            context.paths.stage0_listing_links,
            [{"item_page_link": url} for url in stage0_urls],
        )

        count = len(rows) if recorded_rows is None else recorded_rows
        warnings = []
        stage0_status = "completed"
        if stage0_warning:
            warnings.append({"stage": "stage0", "message": stage0_warning})
            stage0_status = "completed_with_warnings"
            if root_status == "completed":
                root_status = "completed_with_warnings"

        def stage(
            status: str = "completed", *, input_rows: int | None = count
        ) -> dict[str, Any]:
            return {
                "status": status,
                "started_at_utc": observed_at,
                "completed_at_utc": observed_at,
                "input_paths": [],
                "output_paths": [],
                "input_rows": input_rows,
                "output_rows": count,
                "error_count": 0,
                "warning_count": 1 if status == "completed_with_warnings" else 0,
                "duration_seconds": 1.0,
            }

        context.manifest.update(
            {
                "status": root_status,
                "canonical_for_import": False,
                "created_at_utc": observed_at,
                "updated_at_utc": observed_at,
                "configuration": {
                    "stage0": {"max_pages": max_pages},
                    "stage1": {"limit": stage1_limit},
                },
                "warnings": warnings,
                "errors": [],
                "stages": {
                    "stage0": {
                        **stage(stage0_status, input_rows=None),
                        "output_rows": len(stage0_urls),
                        "metrics": {
                            "pages_requested": min(max_pages, 3),
                            "repeated_page_detected": False,
                            "maximum_page_limit_reached": maximum_page_limit_reached,
                            "discovered_listing_count": len(stage0_urls),
                        },
                    },
                    "stage1": stage(),
                    "stage2": stage(),
                    "manual_fixes": stage(),
                    "stage3": stage(),
                    "stage3_qc": stage(),
                },
            }
        )
        context.save()
        if canonical_approved:
            evaluation = evaluate_run_approval(context.paths.root)
            if not evaluation.blocking_conditions:
                approve_run(
                    context.paths.root,
                    approved_by="synthetic-test-reviewer",
                    note="Synthetic fixture reviewed for deterministic tests",
                    acknowledge_warnings=bool(
                        evaluation.material_warning_conditions
                    ),
                    now=datetime.fromisoformat(observed_at.replace("Z", "+00:00")),
                )
        return context.paths.root

    make.listing_row = listing_row
    return make


@pytest.fixture(scope="session")
def postgres_target():
    """Approve TEST_DATABASE_URL without ever falling back to DATABASE_URL."""

    if not os.environ.get("TEST_DATABASE_URL", "").strip():
        pytest.skip("PostgreSQL tests require explicit TEST_DATABASE_URL")
    try:
        target = approve_test_database_environment(os.environ)
    except UnsafeTestDatabaseError as error:
        pytest.fail(str(error), pytrace=False)
    try:
        import psycopg
    except ImportError:
        pytest.fail(
            "PostgreSQL tests require requirements-database.txt", pytrace=False
        )
    try:
        with psycopg.connect(target.url) as connection:
            verify_connected_test_database(connection, target)
    except UnsafeTestDatabaseError as error:
        pytest.fail(str(error), pytrace=False)
    except Exception:
        pytest.fail(
            "Could not connect to the approved TEST_DATABASE_URL", pytrace=False
        )
    return target


@pytest.fixture
def postgres_database(postgres_target):
    """Provide a freshly migrated database and clean only Stage 4 objects."""

    import psycopg

    connection = None
    try:
        connection = psycopg.connect(postgres_target.url, autocommit=True)
        verify_connected_test_database(connection, postgres_target)
        reset_stage4_objects(connection, postgres_target)
        apply_stage4_migration(connection, postgres_target, STAGE4_MIGRATIONS)
        yield connection
    finally:
        if connection is not None:
            connection.close()
        try:
            with psycopg.connect(postgres_target.url, autocommit=True) as cleanup:
                verify_connected_test_database(cleanup, postgres_target)
                reset_stage4_objects(cleanup, postgres_target)
        except Exception:
            pytest.fail(
                "Could not clean the approved Stage 4 test objects", pytrace=False
            )
