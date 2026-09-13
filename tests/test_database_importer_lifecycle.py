from dataclasses import replace

import pytest

from pipeline.database_importer import (
    InMemoryImportDatabase,
    ImportValidationError,
    determine_lifecycle_eligibility,
    load_and_validate_run,
)


def load(make_stage4_run, rows, **kwargs):
    return load_and_validate_run(
        make_stage4_run(rows, **kwargs),
        allow_noncanonical_run=True,
        override_reason="Synthetic lifecycle test override",
    )


def test_valid_run_import_and_same_run_are_idempotent(make_stage4_run) -> None:
    run = load(make_stage4_run, [make_stage4_run.listing_row("100")])
    database = InMemoryImportDatabase()
    first = database.import_run(run)
    second = database.import_run(run)
    assert first.summary["new_listings"] == 1
    assert len(database.state.listings_by_source_id) == 1
    assert second.already_imported
    assert second.observations == ()


def test_same_run_id_with_changed_content_is_rejected(make_stage4_run) -> None:
    run = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        run_id="same-run",
    )
    database = InMemoryImportDatabase()
    database.import_run(run)
    changed = replace(run, canonical_sha256="0" * 64)
    with pytest.raises(ImportValidationError, match="different content"):
        database.import_run(changed)


def test_second_run_with_unchanged_listing_adds_immutable_observation(
    make_stage4_run,
) -> None:
    database = InMemoryImportDatabase()
    first = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        timestamp="2026-07-01T12:00:00Z",
    )
    second = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        timestamp="2026-07-02T12:00:00Z",
    )
    database.import_run(first)
    plan = database.import_run(second)
    assert plan.summary["unchanged_listings"] == 1
    assert plan.summary["observations_inserted"] == 1
    assert plan.observations[0].classification == "unchanged"


def test_price_change_is_updated_and_names_changed_fields(make_stage4_run) -> None:
    database = InMemoryImportDatabase()
    first = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        timestamp="2026-07-01T12:00:00Z",
    )
    second = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100", price_numeric="900")],
        timestamp="2026-07-02T12:00:00Z",
    )
    database.import_run(first)
    plan = database.import_run(second)
    assert plan.summary["updated_listings"] == 1
    assert "price_numeric" in plan.observations[0].changed_fields
    assert "price_monthly" in plan.observations[0].changed_fields


def test_new_listing_classification_and_summary(make_stage4_run) -> None:
    database = InMemoryImportDatabase()
    first = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        timestamp="2026-07-01T12:00:00Z",
    )
    second = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100"), make_stage4_run.listing_row("200")],
        timestamp="2026-07-02T12:00:00Z",
    )
    database.import_run(first)
    plan = database.import_run(second)
    assert plan.summary["new_listings"] == 1
    assert len(database.state.listings_by_source_id) == 2


def test_missing_listing_becomes_possible_then_removed_at_threshold(
    make_stage4_run,
) -> None:
    database = InMemoryImportDatabase()
    run1 = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        timestamp="2026-07-01T12:00:00Z",
    )
    run2 = load(
        make_stage4_run,
        [make_stage4_run.listing_row("200")],
        timestamp="2026-07-02T12:00:00Z",
    )
    run3 = load(
        make_stage4_run,
        [make_stage4_run.listing_row("200")],
        timestamp="2026-07-03T12:00:00Z",
    )
    database.import_run(run1)
    possible = database.import_run(run2)
    assert possible.summary["possibly_removed_listings"] == 1
    assert database.state.listings_by_source_id["100"].status == "possibly_removed"
    removed = database.import_run(run3)
    assert removed.summary["removed_listings"] == 1
    assert database.state.listings_by_source_id["100"].status == "removed"


def test_relisted_listing_resets_missing_count(make_stage4_run) -> None:
    database = InMemoryImportDatabase()
    runs = [
        load(
            make_stage4_run,
            [make_stage4_run.listing_row("100")],
            timestamp="2026-07-01T12:00:00Z",
        ),
        load(
            make_stage4_run,
            [make_stage4_run.listing_row("200")],
            timestamp="2026-07-02T12:00:00Z",
        ),
        load(
            make_stage4_run,
            [make_stage4_run.listing_row("200")],
            timestamp="2026-07-03T12:00:00Z",
        ),
        load(
            make_stage4_run,
            [make_stage4_run.listing_row("100"), make_stage4_run.listing_row("200")],
            timestamp="2026-07-04T12:00:00Z",
        ),
    ]
    for run in runs[:3]:
        database.import_run(run)
    relisted = database.import_run(runs[3])
    listing = database.state.listings_by_source_id["100"]
    assert relisted.summary["relisted_listings"] == 1
    assert listing.status == "relisted"
    assert listing.missing_run_count == 0


def test_smoke_run_cannot_apply_absence_transitions(make_stage4_run) -> None:
    database = InMemoryImportDatabase()
    full = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        timestamp="2026-07-01T12:00:00Z",
    )
    smoke = load(
        make_stage4_run,
        [make_stage4_run.listing_row("200")],
        max_pages=1,
        timestamp="2026-07-02T12:00:00Z",
    )
    database.import_run(full)
    plan = database.import_run(smoke)
    assert not plan.lifecycle_applied
    assert database.state.listings_by_source_id["100"].status == "active"


def test_limited_stage1_run_cannot_apply_absence_transitions(
    make_stage4_run,
) -> None:
    database = InMemoryImportDatabase()
    full = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        timestamp="2026-07-01T12:00:00Z",
    )
    limited = load(
        make_stage4_run,
        [make_stage4_run.listing_row("200")],
        stage1_limit=5,
        timestamp="2026-07-02T12:00:00Z",
    )
    database.import_run(full)
    plan = database.import_run(limited)
    assert not plan.lifecycle_applied
    assert database.state.listings_by_source_id["100"].missing_run_count == 0


@pytest.mark.parametrize(
    ("warning", "max_reached"),
    [
        ("Discovery returned zero listings.", False),
        ("Discovery count fell substantially compared with the supplied count.", False),
        ("Discovery reached the configured maximum page limit.", True),
    ],
)
def test_incomplete_discovery_warnings_block_removals(
    make_stage4_run, warning: str, max_reached: bool
) -> None:
    database = InMemoryImportDatabase()
    full = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        timestamp="2026-07-01T12:00:00Z",
    )
    incomplete = load(
        make_stage4_run,
        [make_stage4_run.listing_row("200")],
        stage0_warning=warning,
        maximum_page_limit_reached=max_reached,
        timestamp="2026-07-02T12:00:00Z",
    )
    database.import_run(full)
    plan = database.import_run(incomplete)
    assert not plan.lifecycle_applied
    assert database.state.listings_by_source_id["100"].status == "active"


def test_repeated_page_warning_alone_does_not_block_lifecycle(make_stage4_run) -> None:
    run = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        stage0_warning="Discovery stopped after detecting a repeated result page.",
    )
    assert run.lifecycle_eligible


def test_missing_stage0_completion_metrics_block_lifecycle(make_stage4_run) -> None:
    run = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
    )
    del run.manifest["stages"]["stage0"]["metrics"][
        "maximum_page_limit_reached"
    ]
    eligible, reasons = determine_lifecycle_eligibility(
        run.manifest, stage0_actual_rows=1
    )
    assert not eligible
    assert any("completion metric" in reason for reason in reasons)


def test_explicit_skip_lifecycle_blocks_missing_transitions(make_stage4_run) -> None:
    database = InMemoryImportDatabase()
    first = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        timestamp="2026-07-01T12:00:00Z",
    )
    second = load(
        make_stage4_run,
        [make_stage4_run.listing_row("200")],
        timestamp="2026-07-02T12:00:00Z",
    )
    database.import_run(first)
    plan = database.import_run(second, skip_lifecycle_updates=True)
    assert not plan.lifecycle_applied
    assert database.state.listings_by_source_id["100"].missing_run_count == 0


def test_explicit_skip_does_not_relist_or_reset_seen_removed_listing(
    make_stage4_run,
) -> None:
    database = InMemoryImportDatabase()
    runs = [
        load(
            make_stage4_run,
            [make_stage4_run.listing_row("100")],
            timestamp="2026-07-01T12:00:00Z",
        ),
        load(
            make_stage4_run,
            [make_stage4_run.listing_row("200")],
            timestamp="2026-07-02T12:00:00Z",
        ),
        load(
            make_stage4_run,
            [make_stage4_run.listing_row("200")],
            timestamp="2026-07-03T12:00:00Z",
        ),
        load(
            make_stage4_run,
            [make_stage4_run.listing_row("100"), make_stage4_run.listing_row("200")],
            timestamp="2026-07-04T12:00:00Z",
        ),
    ]
    for run in runs[:3]:
        database.import_run(run)
    plan = database.import_run(runs[3], skip_lifecycle_updates=True)
    listing = database.state.listings_by_source_id["100"]
    assert not plan.lifecycle_applied
    assert plan.summary["relisted_listings"] == 0
    assert listing.status == "removed"
    assert listing.missing_run_count == 2


def test_out_of_order_run_is_rejected_without_rewinding_current_state(
    make_stage4_run,
) -> None:
    database = InMemoryImportDatabase()
    newer = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        timestamp="2026-07-10T12:00:00Z",
    )
    older = load(
        make_stage4_run,
        [make_stage4_run.listing_row("200")],
        timestamp="2026-07-09T12:00:00Z",
    )
    database.import_run(newer)
    with pytest.raises(ImportValidationError, match="Out-of-order imports"):
        database.import_run(older)
    assert set(database.state.listings_by_source_id) == {"100"}


def test_stage0_discovery_prevents_false_removal_when_scrape_failed(
    make_stage4_run,
) -> None:
    database = InMemoryImportDatabase()
    first = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        timestamp="2026-07-01T12:00:00Z",
    )
    failed_detail = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100", scraped_ok="false", scrape_error="fixture")],
        timestamp="2026-07-02T12:00:00Z",
    )
    database.import_run(first)
    database.import_run(failed_detail)
    assert database.state.listings_by_source_id["100"].missing_run_count == 0
    assert database.state.listings_by_source_id["100"].status == "active"


def test_shared_address_never_merges_distinct_listing_identities(
    make_stage4_run,
) -> None:
    rows = [
        make_stage4_run.listing_row("100"),
        make_stage4_run.listing_row("200"),
    ]
    run = load(make_stage4_run, rows)
    database = InMemoryImportDatabase()
    plan = database.import_run(run)
    assert len(database.state.listings_by_source_id) == 2
    assert plan.summary["properties_created"] == 1
    assert plan.summary["properties_reused"] == 1


def test_strong_duplicate_ad_signal_creates_review_but_does_not_merge(
    make_stage4_run,
) -> None:
    rows = [
        make_stage4_run.listing_row("100", description="Same advertisement text"),
        make_stage4_run.listing_row("200", description="Same advertisement text"),
    ]
    database = InMemoryImportDatabase()
    plan = database.import_run(load(make_stage4_run, rows))
    assert len(database.state.listings_by_source_id) == 2
    assert any(
        review.review_type == "identity"
        and review.reason == "possible_duplicate_advertisement"
        for review in plan.reviews
    )


def test_duplicate_ad_signal_is_compared_with_prior_runs(make_stage4_run) -> None:
    database = InMemoryImportDatabase()
    first = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100", description="Same advertisement text")],
        timestamp="2026-07-01T12:00:00Z",
    )
    second = load(
        make_stage4_run,
        [
            make_stage4_run.listing_row("100", description="Same advertisement text"),
            make_stage4_run.listing_row("200", description="Same advertisement text"),
        ],
        timestamp="2026-07-02T12:00:00Z",
    )
    database.import_run(first)
    plan = database.import_run(second)
    assert any(
        review.source_listing_id == "200"
        and review.reason == "possible_duplicate_advertisement"
        for review in plan.reviews
    )


def test_exact_property_match_reuses_across_runs(make_stage4_run) -> None:
    database = InMemoryImportDatabase()
    first = load(make_stage4_run, [make_stage4_run.listing_row("100")])
    second = load(
        make_stage4_run,
        [make_stage4_run.listing_row("200", address="123 Richmond St.")],
        timestamp="2026-07-20T12:00:00Z",
    )
    database.import_run(first)
    plan = database.import_run(second)
    assert plan.summary["properties_created"] == 0
    assert plan.summary["properties_reused"] == 1


def test_incomplete_address_does_not_replace_reliable_existing_property(
    make_stage4_run,
) -> None:
    database = InMemoryImportDatabase()
    first = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100")],
        timestamp="2026-07-01T12:00:00Z",
    )
    second = load(
        make_stage4_run,
        [
            make_stage4_run.listing_row(
                "100",
                address="Richmond Street",
                geocode_status="error",
                geocode_confidence="",
                latitude="",
                longitude="",
                map_ready="false",
                geocode_city="",
                geocode_country_code="",
            )
        ],
        timestamp="2026-07-02T12:00:00Z",
    )
    database.import_run(first)
    original_property = database.state.listings_by_source_id["100"].property_id
    plan = database.import_run(second)
    assert database.state.listings_by_source_id["100"].property_id == original_property
    assert plan.summary["properties_created"] == 0
    assert any(
        review.reason == "incomplete_address_did_not_replace_existing_property"
        for review in plan.reviews
    )


def test_recovered_complete_address_enriches_linked_addressless_property(
    make_stage4_run,
) -> None:
    database = InMemoryImportDatabase()
    first = load(
        make_stage4_run,
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
        timestamp="2026-07-01T12:00:00Z",
    )
    second = load(
        make_stage4_run,
        [make_stage4_run.listing_row("100", address="156 Paperbirch Crescent")],
        timestamp="2026-07-02T12:00:00Z",
    )

    database.import_run(first)
    original_property_id = database.state.listings_by_source_id["100"].property_id
    plan = database.import_run(second)

    assert database.state.listings_by_source_id["100"].property_id == original_property_id
    assert len(database.state.properties_by_id) == 1
    enriched = database.state.properties_by_id[original_property_id]
    assert enriched.normalized_address == "156 paperbirch crescent"
    assert enriched.match_key is not None
    assert plan.summary["properties_created"] == 0
    assert plan.summary["properties_reused"] == 1
    assert any(
        review.reason == "existing_incomplete_property_enriched"
        for review in plan.reviews
    )


def test_addressless_property_is_not_enriched_over_competing_exact_identity(
    make_stage4_run,
) -> None:
    database = InMemoryImportDatabase()
    first = load(
        make_stage4_run,
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
            ),
            make_stage4_run.listing_row("200", address="156 Paperbirch Crescent"),
        ],
        timestamp="2026-07-01T12:00:00Z",
    )
    second = load(
        make_stage4_run,
        [
            make_stage4_run.listing_row("100", address="156 Paperbirch Crescent"),
            make_stage4_run.listing_row("200", address="156 Paperbirch Crescent"),
        ],
        timestamp="2026-07-02T12:00:00Z",
    )

    database.import_run(first)
    original_addressless_id = database.state.listings_by_source_id["100"].property_id
    competing_id = database.state.listings_by_source_id["200"].property_id
    plan = database.import_run(second)

    assert original_addressless_id != competing_id
    assert database.state.listings_by_source_id["100"].property_id == competing_id
    assert database.state.properties_by_id[original_addressless_id].match_key is None
    assert not any(decision.enrich_identity for decision in plan.properties)


def test_units_prevent_incorrect_property_merge(make_stage4_run) -> None:
    run = load(
        make_stage4_run,
        [
            make_stage4_run.listing_row("100", address="123 Richmond St Unit 1"),
            make_stage4_run.listing_row("200", address="123 Richmond St Unit 2"),
        ],
    )
    plan = InMemoryImportDatabase().import_run(run)
    assert plan.summary["properties_created"] == 2


def test_ambiguous_incomplete_address_creates_review_without_merge(
    make_stage4_run,
) -> None:
    rows = [
        make_stage4_run.listing_row(
            "100", address="Richmond Street", geocode_status="error", geocode_city="", geocode_country_code=""
        ),
        make_stage4_run.listing_row(
            "200", address="Richmond Street", geocode_status="error", geocode_city="", geocode_country_code=""
        ),
    ]
    plan = InMemoryImportDatabase().import_run(load(make_stage4_run, rows))
    assert plan.summary["properties_created"] == 2
    assert plan.summary["possible_property_duplicates"] >= 1
    assert any(review.review_type == "property_match" for review in plan.reviews)


def test_incomplete_property_duplicate_is_reviewed_across_runs(
    make_stage4_run,
) -> None:
    def incomplete(listing_id: str):
        return make_stage4_run.listing_row(
            listing_id,
            address="Richmond Street",
            geocode_status="error",
            geocode_confidence="",
            geocode_city="",
            geocode_country_code="",
            latitude="",
            longitude="",
            map_ready="false",
        )

    database = InMemoryImportDatabase()
    database.import_run(
        load(
            make_stage4_run,
            [incomplete("100")],
            timestamp="2026-07-01T12:00:00Z",
        )
    )
    plan = database.import_run(
        load(
            make_stage4_run,
            [incomplete("200")],
            timestamp="2026-07-02T12:00:00Z",
        )
    )
    assert plan.summary["properties_created"] == 1
    assert any(
        review.source_listing_id == "200"
        and review.reason == "incomplete_exact_address_not_automatically_merged"
        for review in plan.reviews
    )


def test_equal_coordinates_alone_do_not_merge_different_addresses(
    make_stage4_run,
) -> None:
    rows = [
        make_stage4_run.listing_row("100", address="123 Richmond Street"),
        make_stage4_run.listing_row("200", address="456 Oxford Street"),
    ]
    plan = InMemoryImportDatabase().import_run(load(make_stage4_run, rows))
    assert plan.summary["properties_created"] == 2
    assert any(
        review.reason == "coordinates_match_different_normalized_address"
        for review in plan.reviews
    )


def test_ai_and_geocode_signals_create_review_items(make_stage4_run) -> None:
    row = make_stage4_run.listing_row(
        needs_manual_review="true",
        review_flags='["consensus_disagreement_is_sublet"]',
        map_ready="false",
        latitude="",
        longitude="",
        geocode_status="not_found",
        geocode_quality_issue="low_confidence;city_mismatch",
    )
    plan = InMemoryImportDatabase().import_run(load(make_stage4_run, [row]))
    review_types = {review.review_type for review in plan.reviews}
    reasons = {review.reason for review in plan.reviews}
    assert {"ai", "geocode"} <= review_types
    assert {"needs_manual_review", "low_confidence", "city_mismatch"} <= reasons


def test_review_items_are_not_duplicated_on_reimport(make_stage4_run) -> None:
    run = load(
        make_stage4_run,
        [make_stage4_run.listing_row(needs_manual_review="true")],
    )
    database = InMemoryImportDatabase()
    first = database.import_run(run)
    second = database.import_run(run)
    assert first.summary["review_items_created"] > 0
    assert second.already_imported
    assert len(database.state.review_keys) == first.summary["review_items_created"]


def test_reimport_with_different_lifecycle_configuration_is_rejected(
    make_stage4_run,
) -> None:
    run = load(make_stage4_run, [make_stage4_run.listing_row("100")])
    database = InMemoryImportDatabase()
    database.import_run(run, missing_run_threshold=2)
    with pytest.raises(ImportValidationError, match="different Stage 4 configuration"):
        database.import_run(run, missing_run_threshold=3)


def test_transaction_like_memory_store_rolls_back_injected_failure(
    make_stage4_run,
) -> None:
    run = load(make_stage4_run, [make_stage4_run.listing_row("100")])
    database = InMemoryImportDatabase()
    with pytest.raises(RuntimeError, match="injected"):
        database.import_run(run, fail_after_plan=True)
    assert database.state.listings_by_source_id == {}
    assert database.state.properties_by_id == {}
    assert database.imported_runs == {}


def test_change_summary_counts_are_internally_consistent(make_stage4_run) -> None:
    rows = [
        make_stage4_run.listing_row("100"),
        make_stage4_run.listing_row("200", address="456 Oxford Street"),
    ]
    plan = InMemoryImportDatabase().import_run(load(make_stage4_run, rows))
    assert plan.summary["new_listings"] == 2
    assert plan.summary["observations_inserted"] == 2
    assert plan.summary["properties_created"] == 2
    assert plan.summary["rows_rejected"] == 0
