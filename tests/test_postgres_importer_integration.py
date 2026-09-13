from __future__ import annotations

from decimal import Decimal

import pytest

from backend.listing_history import project_listing_history
from backend.repository import PostgresListingRepository
from pipeline.database_importer import ImportValidationError, load_and_validate_run
from pipeline.postgres_repository import import_validated_run
from pipeline.run_context import RunContext


pytestmark = pytest.mark.postgres


def _import(
    run_dir,
    postgres_target,
    *,
    missing_run_threshold: int = 2,
    skip_lifecycle_updates: bool = False,
    failure_injector=None,
    override_reason: str | None = "PostgreSQL integration test fixture",
):
    run = load_and_validate_run(
        run_dir,
        allow_noncanonical_run=True,
        override_reason=override_reason,
    )
    return import_validated_run(
        run,
        database_url=postgres_target.url,
        missing_run_threshold=missing_run_threshold,
        skip_lifecycle_updates=skip_lifecycle_updates,
        _test_failure_injector=failure_injector,
    )


def _counts(connection) -> dict[str, int]:
    names = (
        "housing_pipeline_runs",
        "housing_geocode_results",
        "housing_properties",
        "housing_listings",
        "housing_listing_observations",
        "housing_review_items",
    )
    return {
        name: connection.execute(f"select count(*) from public.{name}").fetchone()[0]
        for name in names
    }


def test_real_initial_import_idempotency_reviews_and_view(
    postgres_database, postgres_target, make_stage4_run
) -> None:
    duplicate_description = "Same exact advertisement fixture"
    rows = [
        make_stage4_run.listing_row("100", description=duplicate_description),
        make_stage4_run.listing_row("101", description=duplicate_description),
        make_stage4_run.listing_row(
            "200",
            address="Richmond Street",
            price_text="$25 per month",
            price_numeric="25",
            needs_manual_review="true",
            review_flags='["consensus_disagreement_is_sublet"]',
            furnished_ai_evidence_blocked="true",
            consensus_disagreement_lease_type="true",
            latitude="",
            longitude="",
            map_ready="false",
            geocode_status="not_found",
            geocode_confidence="0.4",
            geocode_city="",
            geocode_country_code="",
            geocode_quality_issue="low_confidence;city_mismatch",
        ),
        make_stage4_run.listing_row(
            "300",
            address="789 Wellington Street",
            price_text="$500",
            price_numeric="500",
            price_period="",
        ),
    ]
    run_dir = make_stage4_run(
        rows, run_id="postgres-initial", timestamp="2026-07-01T12:00:00Z"
    )

    first = _import(run_dir, postgres_target)
    assert first.summary["new_listings"] == 4
    assert first.summary["observations_inserted"] == 4
    assert first.summary["properties_created"] == 3
    assert first.summary["properties_reused"] == 1
    assert first.summary["review_items_created"] > 0

    run_record = postgres_database.execute(
        """
        select import_status, change_summary, import_configuration,
               import_completed_at, manifest_json
        from public.housing_pipeline_runs where run_id = 'postgres-initial'
        """
    ).fetchone()
    assert run_record[0] == "completed"
    assert run_record[1] == first.summary
    assert run_record[2] == {
        "missing_run_threshold": 2,
        "skip_lifecycle_updates": False,
    }
    assert run_record[3].tzinfo is not None
    assert isinstance(run_record[4], dict)

    observations = list(
        postgres_database.execute(
            "select raw_data, provenance_data, confidence_data, amenities "
            "from public.housing_listing_observations"
        )
    )
    assert all(isinstance(row[0], dict) for row in observations)
    assert all(isinstance(row[1], dict) for row in observations)
    assert all(isinstance(row[2], dict) for row in observations)
    assert all(isinstance(row[3], list) for row in observations)

    reasons = {
        row[0]
        for row in postgres_database.execute(
            "select reason from public.housing_review_items"
        )
    }
    assert {
        "needs_manual_review",
        "consensus_disagreement_is_sublet",
        "consensus_disagreement_lease_type",
        "furnished_ai_evidence_blocked",
        "geocode_status_not_found",
        "missing_coordinates",
        "low_confidence",
        "suspicious_monthly_price",
        "monthly_price_unresolved",
        "possible_duplicate_advertisement",
    } <= reasons

    view_rows = list(
        postgres_database.execute(
            """
            select listing_id, source_listing_id, listing_url, title, description,
                   address, price_numeric, price_monthly, price_text, price_period,
                   bedrooms, housing_type, utilities_included, lease_type,
                   lease_term_months, is_sublet, furnished, bathrooms,
                   bathroom_type, amenities, latitude, longitude, map_ready,
                   distance_to_western_km, listing_status, first_seen_at, last_seen_at
            from public.active_housing_listings order by listing_id
            """
        )
    )
    assert [row[0] for row in view_rows] == ["100", "101", "200", "300"]
    assert all(row[0] == row[1] for row in view_rows)
    assert isinstance(view_rows[0][6], Decimal)
    assert isinstance(view_rows[0][7], Decimal)
    assert view_rows[0][19] == ["Dishwasher", "Laundry"]
    assert view_rows[0][25].tzinfo is not None
    assert view_rows[0][26].tzinfo is not None
    assert view_rows[2][20] is None and view_rows[2][21] is None
    assert view_rows[3][7] is None

    before_repeat = _counts(postgres_database)
    repeated = _import(run_dir, postgres_target)
    assert repeated.already_imported
    assert _counts(postgres_database) == before_repeat

    with pytest.raises(ImportValidationError, match="different Stage 4 configuration"):
        _import(run_dir, postgres_target, missing_run_threshold=3)
    assert _counts(postgres_database) == before_repeat

    canonical = run_dir / "stage3" / "canonical.csv"
    canonical.write_text(
        canonical.read_text(encoding="utf-8").replace(
            "$800 per month", "$801 per month", 1
        ),
        encoding="utf-8",
    )
    with pytest.raises(ImportValidationError, match="different manifest or canonical"):
        _import(run_dir, postgres_target)
    assert _counts(postgres_database) == before_repeat


def test_real_updated_unchanged_and_new_listing_history(
    postgres_database, postgres_target, make_stage4_run
) -> None:
    first_dir = make_stage4_run(
        [make_stage4_run.listing_row("100")],
        run_id="postgres-history-1",
        timestamp="2026-07-01T12:00:00Z",
    )
    updated_row = make_stage4_run.listing_row(
        "100",
        description="Updated fixture description",
        price_text="$925 per month",
        price_numeric="925",
        furnished="false",
        lease_type="month-to-month",
    )
    second_dir = make_stage4_run(
        [updated_row, make_stage4_run.listing_row("200")],
        run_id="postgres-history-2",
        timestamp="2026-07-02T12:00:00Z",
    )
    third_dir = make_stage4_run(
        [updated_row, make_stage4_run.listing_row("200")],
        run_id="postgres-history-3",
        timestamp="2026-07-03T12:00:00Z",
    )

    _import(first_dir, postgres_target)
    second = _import(second_dir, postgres_target)
    assert second.summary["updated_listings"] == 1
    assert second.summary["new_listings"] == 1
    changed = postgres_database.execute(
        """
        select o.change_type, o.changed_fields
        from public.housing_listing_observations o
        join public.housing_listings l on l.id = o.listing_id
        join public.housing_pipeline_runs r on r.id = o.pipeline_run_id
        where l.source_listing_id = '100' and r.run_id = 'postgres-history-2'
        """
    ).fetchone()
    assert changed[0] == "updated"
    assert {"price_monthly", "furnished", "description", "lease_type"} <= set(changed[1])
    latest = postgres_database.execute(
        """
        select price_monthly, furnished, description, listing_status
        from public.active_housing_listings where listing_id = '100'
        """
    ).fetchone()
    assert latest == (
        Decimal("925.00"),
        False,
        "Updated fixture description",
        "active",
    )

    third = _import(third_dir, postgres_target)
    assert third.summary["unchanged_listings"] == 2
    assert third.summary["observations_inserted"] == 2
    history = list(
        postgres_database.execute(
            """
            select o.change_type
            from public.housing_listing_observations o
            join public.housing_listings l on l.id = o.listing_id
            where l.source_listing_id = '100'
            order by o.observed_at
            """
        )
    )
    assert history == [("new",), ("updated",), ("unchanged",)]
    assert postgres_database.execute(
        "select count(*) from public.active_housing_listings where listing_id = '100'"
    ).fetchone()[0] == 1


def test_real_lifecycle_guards_removal_and_relisting(
    postgres_database, postgres_target, make_stage4_run
) -> None:
    base = make_stage4_run(
        [make_stage4_run.listing_row("100")],
        run_id="postgres-lifecycle-1",
        timestamp="2026-07-01T12:00:00Z",
    )
    _import(base, postgres_target)

    guarded = [
        make_stage4_run(
            [make_stage4_run.listing_row("200")],
            run_id="postgres-lifecycle-smoke",
            timestamp="2026-07-02T12:00:00Z",
            max_pages=1,
        ),
        make_stage4_run(
            [make_stage4_run.listing_row("200")],
            run_id="postgres-lifecycle-limit",
            timestamp="2026-07-03T12:00:00Z",
            stage1_limit=1,
        ),
        make_stage4_run(
            [make_stage4_run.listing_row("200")],
            run_id="postgres-lifecycle-incomplete",
            timestamp="2026-07-04T12:00:00Z",
            stage0_warning="Discovery count fell substantially compared with fixture.",
        ),
    ]
    for run_dir in guarded:
        plan = _import(run_dir, postgres_target)
        assert not plan.lifecycle_applied
        assert postgres_database.execute(
            "select status, missing_run_count from public.housing_listings "
            "where source_listing_id = '100'"
        ).fetchone() == ("active", 0)

    skipped = make_stage4_run(
        [make_stage4_run.listing_row("200")],
        run_id="postgres-lifecycle-skipped",
        timestamp="2026-07-05T12:00:00Z",
    )
    skip_plan = _import(skipped, postgres_target, skip_lifecycle_updates=True)
    assert not skip_plan.lifecycle_applied
    assert postgres_database.execute(
        "select status, missing_run_count from public.housing_listings "
        "where source_listing_id = '100'"
    ).fetchone() == ("active", 0)

    stage0_seen = make_stage4_run(
        [make_stage4_run.listing_row("200")],
        stage0_ids=["100", "200"],
        run_id="postgres-lifecycle-stage0-seen",
        timestamp="2026-07-06T12:00:00Z",
    )
    context = RunContext.resume(stage0_seen)
    context.manifest["stages"]["stage1"]["input_rows"] = 2
    context.save()
    seen_plan = _import(stage0_seen, postgres_target)
    assert seen_plan.lifecycle_applied
    assert postgres_database.execute(
        "select status, missing_run_count from public.housing_listings "
        "where source_listing_id = '100'"
    ).fetchone() == ("active", 0)

    first_absence = make_stage4_run(
        [make_stage4_run.listing_row("200")],
        run_id="postgres-lifecycle-absence-1",
        timestamp="2026-07-07T12:00:00Z",
    )
    second_absence = make_stage4_run(
        [make_stage4_run.listing_row("200")],
        run_id="postgres-lifecycle-absence-2",
        timestamp="2026-07-08T12:00:00Z",
    )
    first_plan = _import(first_absence, postgres_target)
    assert first_plan.summary["possibly_removed_listings"] == 1
    assert postgres_database.execute(
        "select status, missing_run_count from public.housing_listings "
        "where source_listing_id = '100'"
    ).fetchone() == ("possibly_removed", 1)
    assert postgres_database.execute(
        "select count(*) from public.active_housing_listings where listing_id = '100'"
    ).fetchone()[0] == 1

    second_plan = _import(second_absence, postgres_target)
    assert second_plan.summary["removed_listings"] == 1
    assert postgres_database.execute(
        "select status, missing_run_count from public.housing_listings "
        "where source_listing_id = '100'"
    ).fetchone() == ("removed", 2)
    assert postgres_database.execute(
        "select count(*) from public.active_housing_listings where listing_id = '100'"
    ).fetchone()[0] == 0

    relist = make_stage4_run(
        [make_stage4_run.listing_row("100"), make_stage4_run.listing_row("200")],
        run_id="postgres-lifecycle-relist",
        timestamp="2026-07-09T12:00:00Z",
    )
    relist_plan = _import(relist, postgres_target)
    assert relist_plan.summary["relisted_listings"] == 1
    assert postgres_database.execute(
        "select status, missing_run_count from public.housing_listings "
        "where source_listing_id = '100'"
    ).fetchone() == ("relisted", 0)
    assert postgres_database.execute(
        "select listing_status from public.active_housing_listings "
        "where listing_id = '100'"
    ).fetchone()[0] == "relisted"

    pipeline_count = postgres_database.execute(
        "select count(*) from public.housing_pipeline_runs"
    ).fetchone()[0]
    for status, recorded_rows in (("failed", None), ("running", None), ("completed", 2)):
        invalid = make_stage4_run(
            [make_stage4_run.listing_row("999")],
            run_id=f"postgres-invalid-{status}-{recorded_rows}",
            root_status=status,
            recorded_rows=recorded_rows,
        )
        with pytest.raises(ImportValidationError):
            load_and_validate_run(invalid)
    assert postgres_database.execute(
        "select count(*) from public.housing_pipeline_runs"
    ).fetchone()[0] == pipeline_count

    out_of_order = make_stage4_run(
        [make_stage4_run.listing_row("999")],
        run_id="postgres-lifecycle-out-of-order",
        timestamp="2026-07-08T18:00:00Z",
    )
    with pytest.raises(ImportValidationError, match="Out-of-order imports"):
        _import(out_of_order, postgres_target)
    assert postgres_database.execute(
        "select count(*) from public.housing_listings where source_listing_id = '999'"
    ).fetchone()[0] == 0
    assert postgres_database.execute(
        "select import_status from public.housing_pipeline_runs "
        "where run_id = 'postgres-lifecycle-out-of-order'"
    ).fetchone()[0] == "failed"


def test_disposable_listing_change_removal_and_reactivation_demo(
    postgres_database, postgres_target, make_stage4_run
) -> None:
    listing_a = make_stage4_run.listing_row(
        "900001",
        title="Demo room $850",
        description="Available September",
        price_text="$850 per month",
        price_numeric="850",
        availability_text="September",
    )
    listing_b = make_stage4_run.listing_row(
        "900001",
        title="Demo room $800",
        description="Available September",
        price_text="$800 per month",
        price_numeric="800",
        availability_text="September",
    )
    other = make_stage4_run.listing_row(
        "900002", address="456 Oxford Street"
    )
    snapshots = [
        make_stage4_run(
            [listing_a], run_id="freshness-demo-a", timestamp="2026-08-01T12:00:00Z"
        ),
        make_stage4_run(
            [listing_b], run_id="freshness-demo-b", timestamp="2026-08-02T12:00:00Z"
        ),
        make_stage4_run(
            [other], run_id="freshness-demo-c1", timestamp="2026-08-03T12:00:00Z"
        ),
        make_stage4_run(
            [other], run_id="freshness-demo-c2", timestamp="2026-08-04T12:00:00Z"
        ),
        make_stage4_run(
            [listing_b, other],
            run_id="freshness-demo-reactivated",
            timestamp="2026-08-05T12:00:00Z",
        ),
    ]

    _import(snapshots[0], postgres_target)
    original = postgres_database.execute(
        "select id, property_id from public.housing_listings "
        "where source_listing_id = '900001'"
    ).fetchone()

    updated = _import(snapshots[1], postgres_target)
    after_update = postgres_database.execute(
        "select id, property_id, status from public.housing_listings "
        "where source_listing_id = '900001'"
    ).fetchone()
    assert updated.summary["updated_listings"] == 1
    assert after_update == (*original, "active")
    assert postgres_database.execute(
        "select price_monthly from public.active_housing_listings "
        "where listing_id = '900001'"
    ).fetchone()[0] == Decimal("800.00")
    assert postgres_database.execute(
        "select count(*) from public.housing_listing_observations "
        "where listing_id = %s", (original[0],)
    ).fetchone()[0] == 2

    _import(snapshots[2], postgres_target)
    assert postgres_database.execute(
        "select status from public.housing_listings where id = %s", (original[0],)
    ).fetchone()[0] == "possibly_removed"
    _import(snapshots[3], postgres_target)
    assert postgres_database.execute(
        "select status from public.housing_listings where id = %s", (original[0],)
    ).fetchone()[0] == "removed"
    assert postgres_database.execute(
        "select count(*) from public.product_housing_listings "
        "where listing_id = '900001'"
    ).fetchone()[0] == 0
    assert postgres_database.execute(
        "select count(*) from public.housing_listing_observations "
        "where listing_id = %s", (original[0],)
    ).fetchone()[0] == 2
    assert postgres_database.execute(
        "select count(*) from public.housing_properties where id = %s",
        (original[1],),
    ).fetchone()[0] == 1

    reactivated = _import(snapshots[4], postgres_target)
    assert reactivated.summary["relisted_listings"] == 1
    assert postgres_database.execute(
        "select id, property_id, status from public.housing_listings "
        "where source_listing_id = '900001'"
    ).fetchone() == (*original, "relisted")
    assert postgres_database.execute(
        "select count(*) from public.housing_listing_observations "
        "where listing_id = %s", (original[0],)
    ).fetchone()[0] == 3

    repository = PostgresListingRepository(postgres_target.url)
    detail = repository.get_detail("900001")
    history = project_listing_history(
        detail, repository.list_observations("900001")
    )
    assert [event["type"] for event in history["events"]] == [
        "LISTING_REACTIVATED",
        "PRICE_CHANGED",
    ]


def test_real_property_matching_is_conservative(
    postgres_database, postgres_target, make_stage4_run
) -> None:
    def incomplete(listing_id: str):
        return make_stage4_run.listing_row(
            listing_id,
            address="Richmond Street",
            latitude="",
            longitude="",
            map_ready="false",
            geocode_status="not_found",
            geocode_confidence="",
            geocode_city="",
            geocode_country_code="",
        )

    rows = [
        make_stage4_run.listing_row("100", address="123 Richmond Street"),
        make_stage4_run.listing_row("101", address="123 Richmond St."),
        make_stage4_run.listing_row("102", address="123 Richmond Street Unit 1"),
        make_stage4_run.listing_row("103", address="123 Richmond Street Unit 2"),
        incomplete("104"),
        incomplete("105"),
        make_stage4_run.listing_row("106", address="456 Oxford Street"),
        make_stage4_run.listing_row("107", address="123 Richmond Road"),
    ]
    run_dir = make_stage4_run(
        rows, run_id="postgres-properties", timestamp="2026-07-01T12:00:00Z"
    )
    plan = _import(run_dir, postgres_target)
    assert plan.summary["properties_created"] == 7
    assert plan.summary["properties_reused"] == 1

    properties = {
        row[0]: row[1]
        for row in postgres_database.execute(
            "select source_listing_id, property_id from public.housing_listings"
        )
    }
    assert properties["100"] == properties["101"]
    assert properties["102"] != properties["103"]
    assert properties["104"] != properties["105"]
    assert properties["100"] != properties["106"]
    assert properties["100"] != properties["107"]
    assert len(set(properties.values())) == 7

    reasons = {
        row[0]
        for row in postgres_database.execute(
            "select reason from public.housing_review_items"
        )
    }
    assert "incomplete_exact_address_not_automatically_merged" in reasons
    assert "coordinates_match_different_normalized_address" in reasons
    assert "unit_presence_differs_from_possible_property_match" in reasons


def test_real_recovered_address_enriches_existing_property_identity(
    postgres_database, postgres_target, make_stage4_run
) -> None:
    missing = make_stage4_run(
        [
            make_stage4_run.listing_row(
                "100",
                address="",
                geocode_query="",
                geocode_status="missing_address",
                geocode_confidence="",
                geocode_city="",
                geocode_country_code="",
                latitude="",
                longitude="",
                map_ready="false",
            )
        ],
        run_id="postgres-property-enrichment-missing",
        timestamp="2026-07-01T12:00:00Z",
    )
    _import(missing, postgres_target)
    original_property_id = postgres_database.execute(
        "select property_id from public.housing_listings where source_listing_id = '100'"
    ).fetchone()[0]

    recovered = make_stage4_run(
        [make_stage4_run.listing_row("100", address="156 Paperbirch Crescent")],
        run_id="postgres-property-enrichment-recovered",
        timestamp="2026-07-02T12:00:00Z",
    )
    plan = _import(recovered, postgres_target)

    property_row = postgres_database.execute(
        """
        select id, normalized_address, match_key, latitude, longitude
        from public.housing_properties
        where id = %s
        """,
        (original_property_id,),
    ).fetchone()
    assert property_row[0] == original_property_id
    assert property_row[1] == "156 paperbirch crescent"
    assert property_row[2] is not None
    assert property_row[3:] == pytest.approx((43.01, -81.27))
    assert postgres_database.execute(
        "select count(*) from public.housing_properties"
    ).fetchone()[0] == 1
    assert postgres_database.execute(
        "select property_id from public.housing_listings where source_listing_id = '100'"
    ).fetchone()[0] == original_property_id
    assert plan.summary["properties_created"] == 0
    assert plan.summary["properties_reused"] == 1
    assert postgres_database.execute(
        """
        select count(*) from public.housing_review_items
        where reason = 'existing_incomplete_property_enriched'
        """
    ).fetchone()[0] == 1


def test_real_transaction_rolls_back_domain_writes_and_records_sanitized_failure(
    postgres_database, postgres_target, make_stage4_run
) -> None:
    base = make_stage4_run(
        [make_stage4_run.listing_row("100"), make_stage4_run.listing_row("200")],
        run_id="postgres-rollback-base",
        timestamp="2026-07-01T12:00:00Z",
    )
    _import(base, postgres_target, missing_run_threshold=1)
    before = _counts(postgres_database)
    failing = make_stage4_run(
        [
            make_stage4_run.listing_row("200"),
            make_stage4_run.listing_row(
                "300", address="999 Talbot Street", needs_manual_review="true"
            ),
        ],
        run_id="postgres-rollback-failure",
        timestamp="2026-07-02T12:00:00Z",
    )

    def inject(checkpoint: str) -> None:
        assert checkpoint == "after_domain_writes"
        raise RuntimeError(
            "injected password=topsecret "
            "postgresql://private_user:private_password@127.0.0.1/uwo_housing_test"
        )

    with pytest.raises(RuntimeError, match="injected"):
        _import(
            failing,
            postgres_target,
            missing_run_threshold=1,
            failure_injector=inject,
        )

    after = _counts(postgres_database)
    assert after["housing_pipeline_runs"] == before["housing_pipeline_runs"] + 1
    for table in (
        "housing_geocode_results",
        "housing_properties",
        "housing_listings",
        "housing_listing_observations",
        "housing_review_items",
    ):
        assert after[table] == before[table]
    assert postgres_database.execute(
        "select status, missing_run_count from public.housing_listings "
        "where source_listing_id = '100'"
    ).fetchone() == ("active", 0)
    assert postgres_database.execute(
        "select count(*) from public.housing_listings where source_listing_id = '300'"
    ).fetchone()[0] == 0

    status, error = postgres_database.execute(
        "select import_status, import_error from public.housing_pipeline_runs "
        "where run_id = 'postgres-rollback-failure'"
    ).fetchone()
    assert status == "failed"
    assert "[REDACTED]" in error
    assert "topsecret" not in error
    assert "private_user" not in error
    assert "private_password" not in error
    assert "postgresql://private_user" not in error


def test_source_advisory_transaction_lock_scope_and_release(
    postgres_database, postgres_target
) -> None:
    import psycopg

    first = psycopg.connect(postgres_target.url)
    second = psycopg.connect(postgres_target.url)
    third = psycopg.connect(postgres_target.url)
    try:
        assert first.execute(
            "select pg_try_advisory_xact_lock(hashtext(%s))", ("uwo_offcampus",)
        ).fetchone()[0]
        assert not second.execute(
            "select pg_try_advisory_xact_lock(hashtext(%s))", ("uwo_offcampus",)
        ).fetchone()[0]
        assert second.execute(
            "select pg_try_advisory_xact_lock(hashtext(%s))", ("another_source",)
        ).fetchone()[0]

        first.commit()
        assert second.execute(
            "select pg_try_advisory_xact_lock(hashtext(%s))", ("uwo_offcampus",)
        ).fetchone()[0]
        second.rollback()
        assert third.execute(
            "select pg_try_advisory_xact_lock(hashtext(%s))", ("uwo_offcampus",)
        ).fetchone()[0]
        third.rollback()

        assert first.execute(
            "select pg_try_advisory_xact_lock(hashtext(%s))", ("uwo_offcampus",)
        ).fetchone()[0]
        first.rollback()
        assert second.execute(
            "select pg_try_advisory_xact_lock(hashtext(%s))", ("uwo_offcampus",)
        ).fetchone()[0]
    finally:
        first.close()
        second.close()
        third.close()
