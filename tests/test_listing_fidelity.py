from __future__ import annotations

import pandas as pd
import pytest

from pipeline.ai_enricher import merge_ai_row, promote_rule_fields, validate_row
from pipeline.review_workflow import _explicit_text_value
from pipeline.uwo_listing_enricher import UWOListingScraper, build_website_ready
from scripts.audit_listing_fidelity import _audit_listing, select_cohort
from scripts.rebuild_listing_fidelity import (
    FidelityRebuildError,
    compare_canonical,
    geocode_recovered_addresses,
    rebuild_stage1,
    rebuild_stage2_cached,
    reuse_geocodes,
)


@pytest.mark.parametrize(
    ("structured", "description", "expected"),
    [
        ("Male", None, "male_preferred"),
        ("Female", None, "female_preferred"),
        ("Male", "Male only household.", "male_only"),
        ("Female", "Female only household.", "female_only"),
        (None, "No gender preference.", "any"),
        (None, None, "not_specified"),
    ],
)
def test_gender_parser_preserves_preference_and_restriction_semantics(
    structured: str | None, description: str | None, expected: str
) -> None:
    assert UWOListingScraper._normalize_gender(structured, description) == expected


def test_structured_gender_preference_wins_over_ai() -> None:
    row = pd.Series(
        {
            "preferred_gender_raw": "Male",
            "preferred_gender_rule": "male_preferred",
            "description": "Female only household.",
        }
    )
    promoted = promote_rule_fields(row)

    merged = merge_ai_row(
        row,
        promoted,
        {
            "ai_preferred_gender": "female_only",
            "ai_evidence_preferred_gender": "Female only household",
        },
    )

    assert merged["preferred_gender"] == "male_preferred"
    assert merged["preferred_gender_source"] == "rule"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Female preferred for this room.", "female_preferred"),
        ("We prefer male applicants.", "male_preferred"),
        ("Female only household.", "female_only"),
        ("Male only household.", "male_only"),
    ],
)
def test_review_parser_does_not_conflate_preference_with_only(
    text: str, expected: str
) -> None:
    value, _, conflicts = _explicit_text_value("preferred_gender", text)

    assert value == expected
    assert conflicts == []


def test_nonsemantic_ai_furnishing_evidence_is_blocked_and_reviewed() -> None:
    row = pd.Series(
        {
            "description": "Recently renovated with a dining table.",
            "furnished_rule": None,
        }
    )

    merged = merge_ai_row(
        row,
        promote_rule_fields(row),
        {
            "ai_furnished": True,
            "ai_evidence_furnished": "Recently renovated with a dining table",
        },
    )
    flags, _ = validate_row(merged)

    assert merged.get("furnished") is None
    assert merged["furnished_ai_evidence_blocked"] is True
    assert "furnished_ai_evidence_blocked" in flags


def test_cached_zero_month_ai_lease_is_downgraded_to_unknown() -> None:
    row = pd.Series(
        {
            "description": "Lease term listed as 0 months.",
            "lease_term_months_rule": None,
        }
    )

    merged = merge_ai_row(
        row,
        promote_rule_fields(row),
        {
            "ai_lease_term_months": 0,
            "ai_evidence_lease_term": "0 months",
        },
    )

    assert merged.get("lease_term_months") is None
    assert merged["lease_term_months_ai_evidence_blocked"] is True


@pytest.mark.parametrize(
    ("raw", "description", "expected_status", "expected_included"),
    [
        ("Included", "Tenant pays hydro.", "all_included", True),
        ("Extra", "Internet included.", "not_included", False),
        (None, "Internet included.", "partially_included", None),
        (None, None, None, None),
    ],
)
def test_utilities_preserve_structured_and_unknown_semantics(
    raw: str | None,
    description: str | None,
    expected_status: str | None,
    expected_included: bool | None,
) -> None:
    status = UWOListingScraper._parse_utilities_status([], raw, description)

    assert status == expected_status
    assert UWOListingScraper._utilities_included_from_status(status) is expected_included


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("House to Share", "house_to_share"),
        ("Apt to Share", "apartment_to_share"),
        ("Bachelor Apt", "bachelor_apartment"),
        ("Sublets", "sublet"),
        ("Rooms", "room"),
    ],
)
def test_housing_type_normalization_preserves_source_categories(
    source: str, expected: str
) -> None:
    assert UWOListingScraper._normalize_housing_type(source) == expected


def test_missing_furnishing_information_remains_unknown() -> None:
    assert UWOListingScraper._parse_furnished_rule(
        [], "Bright room with renovated floors."
    ) is None


def test_explicit_description_address_is_recovered_without_guessing() -> None:
    assert UWOListingScraper._recover_address_from_description(
        "Welcome to 156 Paperbirch Crescent. This home is near campus."
    ) == "156 Paperbirch Crescent"
    assert UWOListingScraper._recover_address_from_description(
        "Steps from Western's Richmond Street gates."
    ) is None


def test_captured_stage1_rebuild_applies_generalized_address_and_bathroom_rules() -> None:
    details = pd.DataFrame(
        [
            {
                "listing_id": "61484",
                "source_url": "https://offcampus.uwo.ca/Listings/Details/61484",
                "title": "$800 per bdrm View Map",
                "address_raw": "per bdrm View Map",
                "address": "",
                "description": (
                    "Welcome to 156 Paperbirch Crescent. "
                    "This 5-bedroom, 2-bathroom home is available."
                ),
                "amenities_list": "[]",
            }
        ]
    )

    rebuilt, changed = rebuild_stage1(details)

    assert rebuilt.loc[0, "address"] == "156 Paperbirch Crescent"
    assert rebuilt.loc[0, "bathrooms_rule"] == 2.0
    assert changed["address"] == 1
    assert changed["bathrooms_rule"] == 1


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("This 3-bedroom, 1-bath home is available now.", 1.0),
        ("There are 2.5 bathrooms in the apartment.", 2.5),
        ("The house includes two full bathrooms.", 2.0),
        ("There is 1 bathroom on each floor.", None),
        ("Two tenants share a bathroom.", None),
    ],
)
def test_bathroom_total_rule_requires_explicit_whole_unit_wording(
    description: str, expected: float | None
) -> None:
    assert UWOListingScraper._parse_bathrooms_rule(description) == expected


def test_bathroom_rule_is_promoted_as_numeric_source_evidence() -> None:
    promoted = promote_rule_fields(pd.Series({"bathrooms_rule": 2.5}))

    assert promoted == {"bathrooms": 2.5, "bathrooms_source": "rule"}


def test_monthly_cleaning_does_not_change_a_twelve_month_lease_type() -> None:
    assert UWOListingScraper._parse_lease_type_rule(
        None, 12, "Monthly cleaning is provided."
    ) == "standard"


def test_ai_sublet_lease_type_requires_explicit_sublet_evidence() -> None:
    row = pd.Series({"description": "Available May 1 for a flexible period."})
    merged = merge_ai_row(
        row,
        promote_rule_fields(row),
        {
            "ai_lease_type": "sublet",
            "ai_evidence_lease_term": "Available May 1 for a flexible period",
        },
    )

    assert merged["lease_type"] == "unknown"
    assert merged["lease_type_ai_evidence_blocked"] is True


def test_negative_furnished_phrase_is_not_a_positive_contradiction() -> None:
    flags, _ = validate_row(
        {"description": "The rental is not furnished.", "furnished": False}
    )
    conflict_flags, _ = validate_row(
        {
            "description": "Common areas are furnished; the room is not furnished.",
            "furnished": False,
        }
    )

    assert "furnished_false_but_description_mentions_furnished" not in flags
    assert "furnished_false_but_description_mentions_furnished" in conflict_flags


def test_website_ready_preserves_critical_raw_fields() -> None:
    source = pd.DataFrame(
        [
            {
                "listing_id": "123",
                "source_url": "https://offcampus.uwo.ca/Listings/Details/123",
                "address_raw": "123 Test St",
                "housing_type_raw": "House to Share",
                "bedrooms_raw": "2",
                "utilities_raw": "Extra",
                "date_available_raw": "May 1",
                "location_area_raw": "Near Western",
                "distance_to_campus_raw": "1.2 Km",
                "preferred_gender_raw": "Male",
                "tenant_type_raw": "Student",
                "smoking_raw": "No",
            }
        ]
    )

    result = build_website_ready(source)

    for field in (
        "address_raw",
        "housing_type_raw",
        "bedrooms_raw",
        "utilities_raw",
        "date_available_raw",
        "location_area_raw",
        "distance_to_campus_raw",
        "preferred_gender_raw",
        "tenant_type_raw",
        "smoking_raw",
    ):
        assert result.loc[0, field] == source.loc[0, field]


def test_audit_cohort_selection_is_deterministic_and_includes_review_cases() -> None:
    details = [
        {
            "listing_id": str(index),
            "address": f"{index} Test Street",
            "housing_type_raw": "House" if index % 2 else "Apartment",
            "bedrooms": str(index % 5 + 1),
            "preferred_gender_raw": "Male" if index == 7 else "",
            "utilities_raw": "Included" if index % 2 else "Extra",
            "furnished_rule": "True" if index % 3 == 0 else "",
        }
        for index in range(45)
    ]
    canonical = [
        {
            "listing_id": str(index),
            "price_monthly": str(500 + index * 50),
            "lease_type": "standard" if index % 2 else "fixed_term",
        }
        for index in range(45)
    ]
    reviews = [{"listing_id": "7"}]

    first = select_cohort(details, canonical, reviews, cohort_size=40)
    second = select_cohort(details, canonical, reviews, cohort_size=40)

    assert first == second
    assert len(first) == 40
    assert "7" in {row["listing_id"] for row in first}


def _captured_detail(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "listing_id": "123",
        "source_url": "https://offcampus.uwo.ca/Listings/Details/123",
        "title": "123 Test Street $800 per month View Map",
        "address_raw": "123 Test Street View Map",
        "address": "123 Test Street",
        "housing_type_raw": "House to Share",
        "bedrooms_raw": "2",
        "utilities_raw": "Extra",
        "date_available_raw": "May 1",
        "lease_term_raw": "0",
        "location_area_raw": "Near Western",
        "distance_to_campus_raw": "1.2 Km",
        "preferred_gender_raw": "Male",
        "smoking_raw": "No",
        "tenant_type_raw": "Student",
        "description": "Summer rental available May through August.",
        "amenities": "Laundry",
        "amenities_list": '["Laundry"]',
        "scraped_ok": True,
    }
    row.update(updates)
    return row


def test_captured_stage1_rebuild_applies_current_semantics_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        UWOListingScraper,
        "fetch_html",
        lambda *_args, **_kwargs: pytest.fail("captured rebuild must not fetch"),
    )

    rebuilt, changes = rebuild_stage1(pd.DataFrame([_captured_detail()]))
    row = rebuilt.iloc[0]

    assert row["preferred_gender_rule"] == "male_preferred"
    assert row["housing_type"] == "house_to_share"
    assert row["utilities_status"] == "not_included"
    assert bool(row["utilities_included_rule"]) is False
    assert row["availability_category"] == "summer_available"
    assert pd.isna(row["is_sublet"])
    assert pd.isna(row["lease_term_months_rule"])
    assert row["price_text"] == "$800 per month"
    assert row["price_monthly"] == 800
    assert changes["housing_type"] == 1


def test_cached_stage2_rebuild_rejects_unsupported_ai_and_preserves_rules() -> None:
    details, _ = rebuild_stage1(pd.DataFrame([_captured_detail()]))
    website = build_website_ready(details)
    cached = pd.DataFrame(
        [
            {
                "listing_id": "123",
                "ai_preferred_gender": "female_only",
                "ai_evidence_preferred_gender": "Summer rental",
                "ai_furnished": True,
                "ai_evidence_furnished": "Summer rental",
            }
        ]
    )

    _enriched, reviewed, metrics = rebuild_stage2_cached(website, cached)
    row = reviewed.iloc[0]

    assert row["preferred_gender"] == "male_preferred"
    assert row["preferred_gender_source"] == "rule"
    assert pd.isna(row.get("furnished"))
    assert bool(row["furnished_ai_evidence_blocked"]) is True
    assert metrics["new_ai_call_count"] == 0
    assert metrics["cached_ai_values_rejected"]["furnished"] == 1


def test_cached_ai_cannot_override_deterministic_non_summer_interval() -> None:
    detail = _captured_detail()
    detail["description"] = (
        "Sublet/assign this room from September 2026 to April 2027."
    )
    detail["date_available_raw"] = "Available September 1, 2026"
    details, _ = rebuild_stage1(pd.DataFrame([detail]))
    website = build_website_ready(details)
    cached = pd.DataFrame(
        [{"listing_id": "123", "ai_availability_category": "summer_available"}]
    )

    _enriched, reviewed, metrics = rebuild_stage2_cached(website, cached)

    assert reviewed.iloc[0]["availability_category"] == "non_summer"
    assert (
        reviewed.iloc[0]["availability_category_source"]
        == "deterministic_description_date_range"
    )
    assert metrics["new_ai_call_count"] == 0


def test_availability_evidence_conflict_is_reviewable() -> None:
    flags, score = validate_row({"availability_category_conflict": True})

    assert "availability_category_evidence_conflict" in flags
    assert score == 1


def test_no_availability_conflict_rebuilds_as_null_not_false() -> None:
    detail = _captured_detail(
        description="Available September-April for $1000/month."
    )

    rebuilt, _changes = rebuild_stage1(pd.DataFrame([detail]))

    assert rebuilt.iloc[0]["availability_category"] == "non_summer"
    assert pd.isna(rebuilt.iloc[0]["availability_category_conflict"])


def test_pricing_only_range_does_not_downgrade_existing_availability() -> None:
    detail = _captured_detail(
        description=(
            "Rent is $1325 due to promotion from September to April and "
            "$1375 from May to August."
        ),
        availability_category="summer_available",
        availability_category_source=None,
        availability_category_evidence=None,
    )

    rebuilt, _changes = rebuild_stage1(pd.DataFrame([detail]))
    row = rebuilt.iloc[0]

    assert row["availability_category"] == "summer_available"
    assert pd.isna(row["availability_category_source"])
    assert pd.isna(row["availability_category_evidence"])
    assert pd.isna(row["availability_category_conflict"])


def test_geocode_reuse_requires_stable_normalized_address_and_url() -> None:
    reviewed = pd.DataFrame(
        [
            {
                "listing_id": "123",
                "listing_url": "https://offcampus.uwo.ca/Listings/Details/123",
                "address": "123 Changed Street",
            }
        ]
    )
    cached = pd.DataFrame(
        [
            {
                "listing_id": "123",
                "listing_url": "https://offcampus.uwo.ca/Listings/Details/123",
                "geocode_query": "123 Test Street, London, ON, Canada",
            }
        ]
    )

    with pytest.raises(FidelityRebuildError, match="normalized address"):
        reuse_geocodes(reviewed, cached)


def test_recovered_address_uses_bounded_cache_aware_stage3(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    reviewed = pd.DataFrame(
        [
            {
                "listing_id": "123",
                "listing_url": "https://offcampus.uwo.ca/Listings/Details/123",
                "address": "156 Paperbirch Crescent",
            }
        ]
    )
    cached = pd.DataFrame(
        [
            {
                "listing_id": "123",
                "listing_url": "https://offcampus.uwo.ca/Listings/Details/123",
                "geocode_query": "",
                "geocode_status": "missing_address",
            }
        ]
    )
    calls = []

    def fake_apply(**kwargs):
        calls.append(kwargs)
        frame = pd.read_csv(kwargs["input_csv"])
        frame["geocode_query"] = "156 Paperbirch Crescent, London, ON, Canada"
        frame["latitude"] = 42.995
        frame["longitude"] = -81.293
        frame["geocode_status"] = "ok"
        for field in (
            "geocode_confidence",
            "geocode_match_type",
            "geocode_result_type",
            "geocode_formatted",
            "geocode_city",
            "geocode_postcode",
            "geocode_country_code",
            "geocode_error",
            "distance_to_western_km",
        ):
            frame[field] = ""
        kwargs["stats"].update(
            {"new_api_call_count": 1, "cache_hit_count": 0, "failed_geocode_count": 0}
        )
        return frame

    monkeypatch.setattr("scripts.rebuild_listing_fidelity.apply_geocoding", fake_apply)
    output, metrics, recovered_ids = geocode_recovered_addresses(
        reviewed,
        cached,
        cache_csv=tmp_path / "cache.csv",
        max_new_geocodes=1,
    )

    assert len(calls) == 1
    assert output.iloc[0]["geocode_status"] == "ok"
    assert metrics["new_api_call_count"] == 1
    assert recovered_ids == {"123"}


def test_recovered_address_cannot_exceed_stage3_call_cap(tmp_path) -> None:
    reviewed = pd.DataFrame(
        [
            {
                "listing_id": "123",
                "listing_url": "https://offcampus.uwo.ca/Listings/Details/123",
                "address": "156 Paperbirch Crescent",
            }
        ]
    )
    cached = pd.DataFrame(
        [
            {
                "listing_id": "123",
                "listing_url": "https://offcampus.uwo.ca/Listings/Details/123",
                "geocode_query": "",
                "geocode_status": "missing_address",
            }
        ]
    )

    with pytest.raises(FidelityRebuildError, match="exceeding the external geocode cap"):
        geocode_recovered_addresses(
            reviewed,
            cached,
            cache_csv=tmp_path / "cache.csv",
            max_new_geocodes=0,
        )


def test_field_diff_classifies_expected_changes_and_keeps_unchanged_listings() -> None:
    shared = {
        "listing_url": "https://offcampus.uwo.ca/Listings/Details/",
        "address": "123 Test Street",
        "geocode_query": "123 Test Street, London, ON, Canada",
        "latitude": "42.99",
        "longitude": "-81.25",
    }
    old = pd.DataFrame(
        [
            {**shared, "listing_id": "1", "listing_url": shared["listing_url"] + "1", "housing_type": "house"},
            {**shared, "listing_id": "2", "listing_url": shared["listing_url"] + "2", "housing_type": "apartment"},
        ]
    )
    new = old.copy()
    new.loc[new["listing_id"] == "1", "housing_type"] = "house_to_share"

    diff, listings, summary = compare_canonical(old, new)

    assert diff.iloc[0]["classification"] == "EXPECTED_CATEGORY_RESTORATION"
    assert summary["changed_listings"] == 1
    assert summary["unchanged_listings"] == 1
    assert set(listings["status"]) == {"changed", "unchanged"}


def test_field_diff_allows_only_recovered_address_identity_and_geocode() -> None:
    old = pd.DataFrame(
        [
            {
                "listing_id": "123",
                "listing_url": "https://offcampus.uwo.ca/Listings/Details/123",
                "address": "",
                "geocode_query": "",
                "geocode_status": "missing_address",
                "latitude": "",
                "longitude": "",
            }
        ]
    )
    new = old.copy()
    new.loc[0, "address"] = "156 Paperbirch Crescent"
    new.loc[0, "geocode_query"] = "156 Paperbirch Crescent, London, ON, Canada"
    new.loc[0, "geocode_status"] = "ok"
    new.loc[0, "latitude"] = "42.995"
    new.loc[0, "longitude"] = "-81.293"

    diff, _, summary = compare_canonical(old, new)

    assert summary["unexpected_change_count"] == 0
    assert set(diff["classification"]) == {
        "EXPECTED_ADDRESS_RECOVERY",
        "EXPECTED_GEOCODE_FOR_RECOVERED_ADDRESS",
    }


def test_monthly_normalization_change_requires_stable_numeric_rent_and_period() -> None:
    shared = {
        "listing_id": "123",
        "listing_url": "https://offcampus.uwo.ca/Listings/Details/123",
        "address": "123 Test Street, London, ON, Canada",
    }
    old = pd.DataFrame(
        [{**shared, "price_numeric": "800", "price_period": "", "price_monthly": ""}]
    )
    new = pd.DataFrame(
        [
            {
                **shared,
                "price_numeric": "800",
                "price_period": "month",
                "price_monthly": "800",
            }
        ]
    )

    diff, _, summary = compare_canonical(old, new)

    assert summary["unexpected_change_count"] == 0
    assert set(diff["classification"]) == {"EXPECTED_PRICE_PERIOD_RECOVERY"}


def test_audit_ignores_rejected_cached_bathroom_evidence() -> None:
    details = _captured_detail(bathroom_type_rule=None)
    canonical = {
        "listing_id": "123",
        "listing_url": details["source_url"],
        "address": details["address"],
        "price_text": "$800 per month",
        "price_numeric": 800,
        "price_period": "month",
        "price_monthly": 800,
        "bedrooms": 2,
        "housing_type": "house_to_share",
        "bathrooms": None,
        "bathrooms_source": None,
        "bathroom_type": "unknown",
        "bathroom_type_source": "evidence_blocked_sentinel",
        "ai_evidence_bathrooms": "fabricated bathroom statement",
        "lease_type": "unknown",
        "availability_text": "May 1",
        "availability_category": "summer_available",
        "is_sublet": None,
        "preferred_gender": "male_preferred",
        "furnished": None,
        "utilities_status": "not_included",
        "utilities_included": False,
    }

    bathroom = next(
        item for item in _audit_listing(details, canonical) if item["field"] == "bathrooms"
    )

    assert bathroom["classification"] == "SOURCE_AMBIGUOUS"
