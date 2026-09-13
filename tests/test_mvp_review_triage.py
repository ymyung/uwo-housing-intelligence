from __future__ import annotations

import pytest

from scripts.triage_mvp_reviews import (
    _database_rows,
    _sample,
    classify_geocode,
    classify_price,
    city_distance_band,
)


def test_city_evidence_uses_observed_distribution_boundary() -> None:
    row = {"address": "123 Test Street", "geocode_status": "ok"}
    city = {"address_match_method": "EXACT_CIVIC_MATCH", "distance_meters": "75"}

    assert classify_geocode(row, city, city_p95_meters=102.5) == (
        "SUPPORTED_WITH_MINOR_OFFSET",
        "GEOCODE_CONFIDENCE_REVIEW",
        "P2",
    )
    city["distance_meters"] = "250"
    assert classify_geocode(row, city, city_p95_meters=102.5) == (
        "SIGNIFICANT_CITY_DISAGREEMENT",
        "GEOCODE_CONFIDENCE_REVIEW",
        "P1",
    )


def test_geocoder_address_conflict_is_not_silently_supported() -> None:
    row = {
        "address": "341 Blueforest Drive",
        "geocode_status": "ok",
        "geocode_result_type": "building",
        "geocode_formatted": "341 Southcrest Drive, London, ON, Canada",
    }

    assert classify_geocode(row, None, city_p95_meters=102.5) == (
        "POSSIBLE_GEOCODER_ERROR",
        "GEOCODE_CONFIDENCE_REVIEW",
        "P1",
    )


def test_price_triage_distinguishes_recoverable_and_legitimate_unknown() -> None:
    recoverable = {
        "title": "$1070",
        "description": "Rent is $1070.00 a month.",
        "price_numeric": "1070",
        "price_period": "",
    }
    unknown = {
        "title": "$950",
        "description": "Parking is $60 a month.",
        "price_numeric": "950",
        "price_period": "",
    }

    assert classify_price(recoverable) == (
        "PERIOD_RECOVERABLE_EXPLICIT", "PIPELINE_DEFECT", "P1", "month"
    )
    assert classify_price(unknown) == (
        "PERIOD_TRULY_UNSPECIFIED", "LEGITIMATE_UNKNOWN", "P2", None
    )


def test_price_triage_marks_existing_unsupported_period_as_blocking() -> None:
    row = {
        "title": "$1495",
        "description": "Hydro is approximately $60/month.",
        "price_numeric": "1495",
        "price_period": "month",
    }

    assert classify_price(row) == (
        "PIPELINE_DEFECT", "MVP_BLOCKING", "P0", None
    )


def test_review_sample_is_stable_and_covers_categories_first() -> None:
    rows = [
        {"listing_id": "1", "sample_tags": ["numeric"]},
        {"listing_id": "2", "sample_tags": ["numeric", "shared"]},
        {"listing_id": "3", "sample_tags": ["private"]},
        {"listing_id": "4", "sample_tags": ["ambiguous"]},
    ]

    first = _sample(rows, 4, "test")
    second = _sample(list(reversed(rows)), 4, "test")

    assert [row["listing_id"] for row in first] == [row["listing_id"] for row in second]
    assert {tag for row in first for tag in row["sample_tags"]} >= {
        "numeric", "shared", "private", "ambiguous"
    }


def test_distance_bands_have_required_boundaries() -> None:
    assert [city_distance_band(value) for value in (25, 50, 100, 200, 400, 401)] == [
        "0-25 m", "25-50 m", "50-100 m", "100-200 m", "200-400 m", ">400 m"
    ]


def test_triage_rejects_remote_database_urls_before_connecting() -> None:
    with pytest.raises(ValueError, match="loopback PostgreSQL"):
        _database_rows("postgresql://user:secret@example.com/housing")
