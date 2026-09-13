from datetime import datetime, timezone
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline import database_importer
from pipeline.database_importer import (
    ExistingState,
    ImportValidationError,
    build_import_plan,
    calculate_monthly_price,
    canonical_source_url,
    changed_fields,
    load_and_validate_run,
    normalize_property_address,
    prepare_canonical_listing,
    sanitize_error,
    StoredListing,
)
from pipeline.run_context import RunContext
from pipeline.run_approval import (
    approval_validation_errors,
    approve_run,
    sha256_file,
)


def test_committed_postgres_fixture_run_is_valid() -> None:
    run_dir = Path(__file__).parent / "fixtures" / "runs" / "postgres_valid"
    canonical = run_dir / "stage3" / "canonical.csv"
    manifest = json.loads(
        (run_dir / "manifest.json").read_text(encoding="utf-8")
    )

    run = load_and_validate_run(run_dir)

    assert run.run_id == "postgres_valid"
    assert len(run.listings) == 1
    assert run.listings[0].source_listing_id == "900001"
    assert manifest["approval"]["canonical_csv_fingerprint"] == sha256_file(
        canonical
    )


def test_committed_fixture_line_endings_are_pinned_to_lf() -> None:
    repository = Path(__file__).resolve().parents[1]
    attributes = (repository / ".gitattributes").read_text(encoding="utf-8")
    fixtures = repository / "tests" / "fixtures"

    assert "tests/fixtures/**/*.csv text eol=lf" in attributes.splitlines()
    assert "tests/fixtures/**/*.json text eol=lf" in attributes.splitlines()
    for pattern in ("*.csv", "*.json"):
        for fixture_path in fixtures.rglob(pattern):
            assert b"\r\n" not in fixture_path.read_bytes(), fixture_path


def test_checkout_normalization_regenerates_valid_fixture_approval(
    tmp_path: Path,
) -> None:
    source = Path(__file__).parent / "fixtures" / "runs" / "postgres_valid"
    run_dir = tmp_path / "postgres_valid"
    shutil.copytree(source, run_dir)
    for pattern in ("*.csv", "*.json"):
        for fixture_path in run_dir.rglob(pattern):
            fixture_path.write_bytes(
                fixture_path.read_bytes().replace(b"\r\n", b"\n")
            )

    committed_manifest = json.loads(
        (run_dir / "manifest.json").read_text(encoding="utf-8")
    )
    expected_manifest_fingerprint = committed_manifest["approval"][
        "manifest_fingerprint"
    ]
    expected_canonical_fingerprint = committed_manifest["approval"][
        "canonical_csv_fingerprint"
    ]
    context = RunContext.resume(run_dir)
    context.manifest.pop("approval")
    context.manifest["canonical_for_import"] = False
    context.save()

    regenerated = approve_run(
        run_dir,
        approved_by="synthetic-fixture-reviewer",
        note="Reviewed synthetic PostgreSQL fixture",
        now=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )

    assert regenerated["approval"]["manifest_fingerprint"] == (
        expected_manifest_fingerprint
    )
    assert regenerated["approval"]["canonical_csv_fingerprint"] == (
        expected_canonical_fingerprint
    )
    assert regenerated["approval"]["canonical_csv_fingerprint"] == sha256_file(
        run_dir / "stage3" / "canonical.csv"
    )
    assert approval_validation_errors(
        regenerated, run_dir / "stage3" / "canonical.csv"
    ) == ()
    assert load_and_validate_run(run_dir).override_used is False


def test_normalized_committed_fixture_still_detects_csv_content_change(
    tmp_path: Path,
) -> None:
    source = Path(__file__).parent / "fixtures" / "runs" / "postgres_valid"
    run_dir = tmp_path / "postgres_valid"
    shutil.copytree(source, run_dir)
    canonical = run_dir / "stage3" / "canonical.csv"
    canonical.write_bytes(canonical.read_bytes().replace(b"\r\n", b"\n"))
    canonical.write_text(
        canonical.read_text(encoding="utf-8").replace(
            "$800 per month", "$801 per month"
        ),
        encoding="utf-8",
        newline="\n",
    )
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert "approved canonical CSV fingerprint does not match" in (
        approval_validation_errors(manifest, canonical)
    )


@pytest.mark.parametrize("mutation", ["append_newline", "remove_final_newline"])
def test_exact_byte_fingerprint_detects_line_ending_changes(
    tmp_path: Path, mutation: str
) -> None:
    source = Path(__file__).parent / "fixtures" / "runs" / "postgres_valid"
    run_dir = tmp_path / "postgres_valid"
    shutil.copytree(source, run_dir)
    canonical = run_dir / "stage3" / "canonical.csv"
    contents = canonical.read_bytes()
    assert contents.endswith(b"\n")

    if mutation == "append_newline":
        canonical.write_bytes(contents + b"\n")
    else:
        canonical.write_bytes(contents[:-1])

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "approved canonical CSV fingerprint does not match" in (
        approval_validation_errors(manifest, canonical)
    )


def test_valid_run_loads_and_uses_stable_source_identity(make_stage4_run) -> None:
    run_dir = make_stage4_run([make_stage4_run.listing_row("101")])
    run = load_and_validate_run(run_dir)
    assert run.source == "uwo_offcampus"
    assert run.listings[0].source_listing_id == "101"
    assert run.listings[0].source_url.endswith("/Listings/Details/101")


def test_noncanonical_run_is_rejected_by_default(make_stage4_run) -> None:
    run_dir = make_stage4_run(
        [make_stage4_run.listing_row()], canonical_approved=False
    )
    with pytest.raises(ImportValidationError, match="not approved"):
        load_and_validate_run(run_dir)


def test_noncanonical_override_is_recorded(make_stage4_run) -> None:
    run_dir = make_stage4_run(
        [make_stage4_run.listing_row()], canonical_approved=False
    )
    run = load_and_validate_run(
        run_dir,
        allow_noncanonical_run=True,
        override_reason="Synthetic validation override",
    )
    assert run.override_used is True


def test_missing_canonical_csv_fails(make_stage4_run) -> None:
    run_dir = make_stage4_run([make_stage4_run.listing_row()])
    (run_dir / "stage3" / "canonical.csv").unlink()
    with pytest.raises(ImportValidationError, match="(?i)canonical.*missing"):
        load_and_validate_run(run_dir)


def test_failed_or_incomplete_manifest_status_fails(make_stage4_run) -> None:
    run_dir = make_stage4_run(
        [make_stage4_run.listing_row()], root_status="failed"
    )
    with pytest.raises(ImportValidationError, match="not importable"):
        load_and_validate_run(run_dir)


def test_inconsistent_actual_row_count_fails(make_stage4_run) -> None:
    run_dir = make_stage4_run(
        [make_stage4_run.listing_row()], recorded_rows=2
    )
    with pytest.raises(ImportValidationError, match="(?i)row.?count"):
        load_and_validate_run(run_dir)


def test_invalid_listing_id_fails_safely(make_stage4_run) -> None:
    row = make_stage4_run.listing_row("100")
    row["listing_id"] = "100.0"
    run_dir = make_stage4_run([row], stage0_ids=["100"])
    with pytest.raises(ImportValidationError, match="Invalid Western source listing ID"):
        load_and_validate_run(run_dir)


def test_url_id_mismatch_fails_safely(make_stage4_run) -> None:
    row = make_stage4_run.listing_row("100")
    row["listing_url"] = "https://offcampus.uwo.ca/Listings/Details/999"
    run_dir = make_stage4_run([row], stage0_ids=["100"])
    with pytest.raises(ImportValidationError, match="does not match"):
        load_and_validate_run(run_dir)


def test_duplicate_source_ids_in_one_csv_fail(make_stage4_run) -> None:
    first = make_stage4_run.listing_row("100")
    duplicate = make_stage4_run.listing_row("100", title="Duplicate")
    run_dir = make_stage4_run(
        [first, duplicate], stage0_ids=["100", "200"]
    )
    with pytest.raises(ImportValidationError, match="Duplicate source listing ID"):
        load_and_validate_run(run_dir)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://offcampus.uwo.ca/Listings/Details/123", "123"),
        ("https://www.offcampus.uwo.ca/listings/details/456/", "456"),
    ],
)
def test_western_url_identity_is_canonicalized(raw: str, expected: str) -> None:
    source_id, url = canonical_source_url(raw)
    assert source_id == expected
    assert url == f"https://offcampus.uwo.ca/Listings/Details/{expected}"


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/Listings/Details/1",
        "https://offcampus.uwo.ca/listings/1",
        "https://offcampus.uwo.ca/Listings/Details/not-a-number",
    ],
)
def test_malformed_or_foreign_listing_urls_fail(raw: str) -> None:
    with pytest.raises(ImportValidationError):
        canonical_source_url(raw)


@pytest.mark.parametrize(
    ("amount", "period", "expected"),
    [(700, "month", 700), (200, "week", 866.67), (30, "day", 912.5)],
)
def test_historical_monthly_price_conversion(
    amount: float, period: str, expected: float
) -> None:
    assert calculate_monthly_price(amount, period) == expected


def test_unknown_historical_price_period_stays_null_and_creates_review(
    make_stage4_run,
) -> None:
    listing = prepare_canonical_listing(
        make_stage4_run.listing_row(price_period="semester", price_monthly="")
    )
    assert listing.values["price_monthly"] is None
    assert any(issue.reason == "monthly_price_unresolved" for issue in listing.issues)


def test_missing_historical_monthly_price_is_calculated(make_stage4_run) -> None:
    listing = prepare_canonical_listing(
        make_stage4_run.listing_row(
            price_numeric="200", price_period="week", price_monthly=""
        )
    )
    assert listing.values["price_numeric"] == 200
    assert listing.values["price_period"] == "week"
    assert listing.values["price_monthly"] == 866.67


def test_per_bedroom_month_period_is_preserved(make_stage4_run) -> None:
    listing = prepare_canonical_listing(
        make_stage4_run.listing_row(
            price_numeric="850",
            price_period="month_per_bedroom",
            price_monthly="",
        )
    )
    assert listing.values["price_period"] == "month_per_bedroom"
    assert listing.values["price_monthly"] == 850


def test_missing_price_period_stays_reviewable(make_stage4_run) -> None:
    listing = prepare_canonical_listing(
        make_stage4_run.listing_row(price_period="", price_monthly="")
    )
    assert listing.values["price_monthly"] is None
    reasons = {issue.reason for issue in listing.issues}
    assert {"missing_price_period", "monthly_price_unresolved"} <= reasons


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("False", False), ("1", True), ("0", False), ("", None)],
)
def test_historical_boolean_forms_are_normalized(
    make_stage4_run, raw: str, expected: bool | None
) -> None:
    listing = prepare_canonical_listing(
        make_stage4_run.listing_row(furnished=raw)
    )
    assert listing.values["furnished"] is expected


def test_invalid_optional_values_create_reviews_instead_of_fake_values(
    make_stage4_run,
) -> None:
    listing = prepare_canonical_listing(
        make_stage4_run.listing_row(
            furnished="sometimes", latitude="999", bedrooms="one"
        )
    )
    assert listing.values["furnished"] is None
    assert listing.values["latitude"] is None
    assert listing.values["bedrooms"] is None
    reasons = {issue.reason for issue in listing.issues}
    assert {"invalid_boolean", "latitude_out_of_range", "invalid_number"} <= reasons


def test_exact_property_normalization_reuses_safe_suffixes() -> None:
    first = normalize_property_address(
        "123 Richmond Street", geocode_city="London", geocode_country_code="ca",
        geocode_status="ok", geocode_confidence=0.95, latitude=43.01,
        longitude=-81.27,
    )
    second = normalize_property_address(
        " 123  RICHMOND st. ", geocode_city="London", geocode_country_code="CA",
        geocode_status="ok", geocode_confidence=0.95, latitude=43.01,
        longitude=-81.27,
    )
    assert first.match_key == second.match_key
    assert first.address_complete


def test_unit_identifier_is_part_of_property_identity() -> None:
    first = normalize_property_address(
        "123 Richmond Street Unit 1", geocode_city="London", geocode_country_code="ca",
        geocode_status="ok", geocode_confidence=0.95, latitude=43.01,
        longitude=-81.27,
    )
    second = normalize_property_address(
        "123 Richmond Street Unit 2", geocode_city="London", geocode_country_code="ca",
        geocode_status="ok", geocode_confidence=0.95, latitude=43.01,
        longitude=-81.27,
    )
    assert first.unit_identifier != second.unit_identifier
    assert first.match_key != second.match_key


def test_incomplete_address_has_no_global_property_match_key() -> None:
    candidate = normalize_property_address("Richmond Street")
    assert candidate.normalized_address == "richmond st"
    assert candidate.match_key is None
    assert not candidate.address_complete


def test_low_confidence_geocode_cannot_create_global_property_key() -> None:
    candidate = normalize_property_address(
        "123 Richmond Street",
        geocode_city="London",
        geocode_country_code="ca",
        geocode_status="ok",
        geocode_confidence=0.4,
        latitude=43.01,
        longitude=-81.27,
    )
    assert candidate.match_key is None
    assert not candidate.address_complete


@pytest.mark.parametrize(
    "address",
    [
        "Unit 2, 123 Main Street, London, ON, Canada",
        "2-123 Main Street, London, ON, Canada",
    ],
)
def test_leading_and_canadian_unit_forms_are_preserved(address: str) -> None:
    candidate = normalize_property_address(address)
    assert candidate.unit_identifier == "unit:2"
    assert candidate.normalized_address == "123 main st"
    assert candidate.match_key is not None


def test_observation_hash_ignores_raw_timestamps_but_detects_price(
    make_stage4_run,
) -> None:
    first = prepare_canonical_listing(
        make_stage4_run.listing_row(scraped_at="2026-01-01T00:00:00Z")
    )
    second = prepare_canonical_listing(
        make_stage4_run.listing_row(scraped_at="2026-02-01T00:00:00Z")
    )
    changed = prepare_canonical_listing(
        make_stage4_run.listing_row(price_numeric="900")
    )
    assert first.observation_hash == second.observation_hash
    assert first.observation_hash != changed.observation_hash
    assert "price_numeric" in changed_fields(first.comparison_data, changed.comparison_data)


def test_observation_hash_is_deterministic_across_json_key_order(
    make_stage4_run,
) -> None:
    first = prepare_canonical_listing(
        make_stage4_run.listing_row(amenities_list='["Laundry", "Dishwasher"]')
    )
    second = prepare_canonical_listing(
        make_stage4_run.listing_row(amenities_list='["Dishwasher", "Laundry"]')
    )
    assert first.observation_hash == second.observation_hash


def test_observation_hash_ignores_text_and_address_formatting(
    make_stage4_run,
) -> None:
    first = prepare_canonical_listing(
        make_stage4_run.listing_row(description="Quiet  room near campus")
    )
    second = prepare_canonical_listing(
        make_stage4_run.listing_row(
            description="  QUIET room   near campus ",
            address="123 RICHMOND st.",
        )
    )
    assert first.observation_hash == second.observation_hash


def test_stage1_rule_fields_survive_stage2_skip_shape(make_stage4_run) -> None:
    row = make_stage4_run.listing_row(
        utilities_included="",
        furnished="",
        parking_available="",
        parking_spaces="",
        laundry="",
        air_conditioning="",
        dishwasher="",
        bathroom_type="",
        lease_type="",
        lease_term_months="",
        preferred_gender="",
        tenant_type="",
        utilities_included_rule="true",
        furnished_rule="false",
        parking_available_rule="true",
        parking_spaces_rule="2",
        laundry_rule="true",
        air_conditioning_rule="false",
        dishwasher_rule="true",
        bathroom_type_rule="private",
        lease_type_rule="fixed_term",
        lease_term_months_rule="8",
        preferred_gender_rule="women",
        tenant_type_rule="student",
    )
    values = prepare_canonical_listing(row).values
    assert values["utilities_included"] is True
    assert values["furnished"] is False
    assert values["parking_available"] is True
    assert values["parking_spaces"] == 2
    assert values["laundry"] is True
    assert values["air_conditioning"] is False
    assert values["dishwasher"] is True
    assert values["bathroom_type"] == "private"
    assert values["lease_type"] == "fixed_term"
    assert values["lease_term_months"] == 8
    assert values["preferred_gender"] == "women"
    assert values["tenant_type"] == "student"


def test_zero_lease_term_is_unknown_not_a_zero_month_lease(make_stage4_run) -> None:
    listing = prepare_canonical_listing(
        make_stage4_run.listing_row(lease_term_months="0")
    )

    assert listing.values["lease_term_months"] is None
    assert any(
        issue.reason == "zero_lease_term_is_unknown"
        for issue in listing.issues
    )


def test_deterministic_rules_beat_ai_but_not_manual_review(make_stage4_run) -> None:
    ai_row = make_stage4_run.listing_row(
        furnished="true",
        furnished_source="ai",
        furnished_rule="false",
    )
    manual_row = make_stage4_run.listing_row(
        furnished="true",
        furnished_source="manual_review",
        furnished_rule="false",
    )
    assert prepare_canonical_listing(ai_row).values["furnished"] is False
    assert prepare_canonical_listing(manual_row).values["furnished"] is True


def test_inconsistent_map_status_is_normalized_and_reviewed(make_stage4_run) -> None:
    listing = prepare_canonical_listing(
        make_stage4_run.listing_row(geocode_status="error", map_ready="true")
    )
    assert listing.values["map_ready"] is False
    assert any(
        issue.reason == "map_ready_inconsistent_with_geocode_status"
        for issue in listing.issues
    )


def test_raw_csv_values_are_preserved_and_invalid_complex_values_are_reviewed(
    make_stage4_run,
) -> None:
    listing = prepare_canonical_listing(
        make_stage4_run.listing_row(
            description="  preserve surrounding spaces  ",
            amenities_list='["Laundry"',
            date_available="2026-09-01garbage",
        )
    )
    assert listing.raw_data["description"] == "  preserve surrounding spaces  "
    assert listing.values["amenities"] == []
    assert listing.values["date_available"] is None
    reasons = {issue.reason for issue in listing.issues}
    assert {"invalid_json_array", "invalid_date"} <= reasons


def test_deterministic_availability_provenance_survives_import_preparation(
    make_stage4_run,
) -> None:
    listing = prepare_canonical_listing(
        make_stage4_run.listing_row(
            availability_category="non_summer",
            availability_category_source="deterministic_description_date_range",
            availability_category_evidence="from September 2026 to April 2027",
        )
    )

    assert listing.values["availability_category"] == "non_summer"
    assert (
        listing.provenance_data["availability_category_source"]
        == "deterministic_description_date_range"
    )
    assert (
        listing.raw_data["availability_category_evidence"]
        == "from September 2026 to April 2027"
    )


def test_manifest_observation_timestamp_must_be_timezone_aware(
    make_stage4_run,
) -> None:
    run_dir = make_stage4_run([make_stage4_run.listing_row()])
    context = RunContext.resume(run_dir)
    context.manifest["stages"]["stage3_qc"]["completed_at_utc"] = (
        "2026-07-18T12:00:00"
    )
    context.save()
    with pytest.raises(ImportValidationError, match="must include a timezone"):
        load_and_validate_run(run_dir)


def test_api_keys_and_database_passwords_are_sanitized() -> None:
    value = (
        "https://api.test/?apiKey=secret-value "
        + "postgresql://user:" + "placeholder-pass@db.example/test"
    )
    sanitized = sanitize_error(value)
    assert "secret-value" not in sanitized
    assert "placeholder-pass" not in sanitized
    assert "[REDACTED]" in sanitized


def test_offline_dry_run_never_requires_database_url(
    make_stage4_run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = make_stage4_run([make_stage4_run.listing_row()])
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["database_importer.py", "--run-dir", str(run_dir), "--dry-run"],
    )
    database_importer.main()
    output = capsys.readouterr().out
    assert "offline dry run" in output
    assert "new_listings: 1" in output


def test_basic_dry_run_uses_explicit_empty_baseline(make_stage4_run) -> None:
    run_dir = make_stage4_run([make_stage4_run.listing_row()])
    run = load_and_validate_run(run_dir)
    plan = build_import_plan(run, ExistingState.empty())
    assert plan.summary["new_listings"] == 1
    assert plan.summary["observations_inserted"] == 1


def test_invalid_existing_lifecycle_status_fails_planning(make_stage4_run) -> None:
    run = load_and_validate_run(
        make_stage4_run([make_stage4_run.listing_row("100")])
    )
    state = ExistingState(
        listings_by_source_id={
            "100": StoredListing(
                id=1,
                source_listing_id="100",
                property_id=None,
                source_url=run.listings[0].source_url,
                status="mystery",
                missing_run_count=0,
            )
        }
    )
    with pytest.raises(ImportValidationError, match="invalid listing status"):
        build_import_plan(run, state)
