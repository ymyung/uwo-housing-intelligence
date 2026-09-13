from __future__ import annotations

from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.repository import InMemoryListingRepository


def explanation(
    *,
    status: str,
    reason_codes: list[str] | None = None,
    review_required: bool = False,
) -> dict[str, object]:
    return {
        "ranking_version": "ranking-v1",
        "ranking_status": status,
        "component_weights": {
            "value": 0.45,
            "campus_access": 0.35,
            "transit": 0.20,
            "amenities": 0.0,
        },
        "key_signals": {
            "value": {
                "monthly_price": 800,
                "market_median": 900,
                "comparable_count": 12,
                "price_delta_percent": -11.11,
                "private_internal_statistic": "not-public",
            },
            "campus_access": {
                "walking_minutes": 18,
                "cycling_minutes": 7,
            },
            "transit_periods": [
                {
                    "time_period": "saturday_daytime",
                    "score": 5,
                    "representative_minutes": 24,
                    "walking_better_samples": 3,
                    "reason_codes": ["walking_better_than_transit"],
                    "raw_provider_payload": {"secret": "not-public"},
                }
            ],
        },
        "confidence": {
            "accessibility_warning_reason_codes": reason_codes or [],
            "accessibility_review_required": review_required,
            "listing_review_flags": ["manual-review"] if review_required else [],
        },
        "eligibility_reasons": (
            [] if status == "ranked" else ["missing_monthly_price"]
        ),
        "system_unavailable_components": ["amenities"],
        "amenity_status": "not_implemented",
        "data_quality_component_status": "not_scored",
        "computed_at": "2026-08-09T00:00:00+00:00",
        "input_fingerprint": "a" * 64,
        "config_fingerprint": "b" * 64,
        "database_url": "postgresql://not-public",
    }


def listing(
    listing_id: str,
    *,
    status: str | None,
    overall: Decimal | None,
    value: Decimal | None,
    campus: Decimal | None,
    transit: Decimal | None,
    bedrooms: int = 4,
    price: int = 800,
    reason_codes: list[str] | None = None,
    review_required: bool = False,
) -> dict[str, object]:
    row: dict[str, object] = {
        "listing_id": listing_id,
        "listing_url": f"https://offcampus.uwo.ca/Listings/Details/{listing_id}",
        "title": f"Listing {listing_id}",
        "description": f"Description {listing_id}",
        "address": f"{listing_id} Test Street",
        "price_monthly": price,
        "bedrooms": bedrooms,
        "housing_type": "house",
        "lease_type": "standard",
        "is_sublet": False,
        "latitude": 43.01,
        "longitude": -81.27,
        "map_ready": True,
        "last_seen_at": "2026-08-08T00:00:00Z",
    }
    if status is not None:
        row.update(
            {
                "ranking_version": "ranking-v1",
                "ranking_status": status,
                "ranking_overall_score": overall,
                "ranking_value_score": value,
                "ranking_campus_access_score": campus,
                "ranking_transit_score": transit,
                "ranking_amenity_score": None,
                "ranking_data_quality_score": None,
                "ranking_explanation": explanation(
                    status=status,
                    reason_codes=reason_codes,
                    review_required=review_required,
                ),
                "ranking_input_fingerprint": "a" * 64,
                "ranking_computed_at": "2026-08-09T00:00:00+00:00",
            }
        )
    return row


@pytest.fixture
def ranking_rows() -> list[dict[str, object]]:
    return [
        listing(
            "a",
            status="ranked",
            overall=Decimal("80.25"),
            value=Decimal("90.50"),
            campus=Decimal("70.25"),
            transit=Decimal("60.00"),
            reason_codes=["high_walking_share", "walking_better_than_transit"],
            review_required=True,
        ),
        listing(
            "b",
            status="ranked",
            overall=Decimal("60.00"),
            value=Decimal("70.00"),
            campus=Decimal("90.00"),
            transit=Decimal("50.00"),
            price=900,
        ),
        listing(
            "c",
            status="partial",
            overall=None,
            value=None,
            campus=Decimal("80.00"),
            transit=Decimal("40.00"),
            price=1000,
        ),
        listing(
            "d",
            status="excluded",
            overall=None,
            value=None,
            campus=None,
            transit=None,
            bedrooms=2,
            price=1100,
        ),
        listing(
            "e",
            status=None,
            overall=None,
            value=None,
            campus=None,
            transit=None,
            price=1200,
        ),
    ]


@pytest.fixture
def client(ranking_rows: list[dict[str, object]]) -> TestClient:
    return TestClient(create_app(repository=InMemoryListingRepository(ranking_rows)))


def test_ranked_collection_and_detail_share_stable_projected_contract(
    client: TestClient,
) -> None:
    collection = client.get("/api/listings?ranking_status=ranked").json()
    ranked = collection["listings"][0]["ranking"]
    detail = client.get("/api/listings/a").json()["ranking"]

    assert ranked == detail
    assert ranked["version"] == "ranking-v1"
    assert ranked["status"] == "ranked"
    assert ranked["overall_score"] == 80.25
    assert ranked["components"] == {
        "value": 90.5,
        "campus_access": 70.25,
        "transit": 60.0,
        "amenities": None,
        "data_quality": None,
    }
    assert ranked["weights"]["amenities"] == 0.0
    assert ranked["amenity_status"] == "not_implemented"
    assert ranked["unavailable_components"] == ["amenities"]
    assert ranked["reasons"] == [
        {
            "kind": "value",
            "title": "Good value",
            "detail": "11% below the comparison median",
        },
        {
            "kind": "campus_access",
            "title": "Campus commute",
            "detail": "18 min walk to Western",
        },
    ]


def test_partial_excluded_and_missing_rows_preserve_null_semantics(
    client: TestClient,
) -> None:
    rows = {
        row["listing_id"]: row
        for row in client.get("/api/listings?page_size=20").json()["listings"]
    }
    assert rows["c"]["ranking"]["status"] == "partial"
    assert rows["c"]["ranking"]["overall_score"] is None
    assert rows["d"]["ranking"]["status"] == "excluded"
    assert rows["d"]["ranking"]["overall_score"] is None
    assert rows["e"]["ranking"] is None


def test_warning_alternatives_and_review_evidence_survive_without_raw_payload(
    client: TestClient,
) -> None:
    ranking = client.get("/api/listings/a").json()["ranking"]
    assert ranking["warnings"] == {
        "accessibility_reason_codes": [
            "high_walking_share",
            "walking_better_than_transit",
        ],
        "accessibility_review_required": True,
        "listing_review_flags": ["manual-review"],
    }
    period = ranking["signals"]["transit_periods"][0]
    assert period["walking_better_samples"] == 3
    assert period["reason_codes"] == ["walking_better_than_transit"]
    serialized = json.dumps(client.get("/api/listings/a").json())
    assert "raw_provider_payload" not in serialized
    assert "database_url" not in serialized
    assert "config_fingerprint" not in serialized
    assert "not-public" not in serialized


def test_ranking_reasons_do_not_fabricate_market_claims() -> None:
    row = listing(
        "no-market-claim",
        status="partial",
        overall=None,
        value=Decimal("88.00"),
        campus=None,
        transit=None,
    )
    row["ranking_explanation"]["key_signals"]["value"] = {
        "monthly_price": 800,
        "market_median": 900,
        "comparable_count": 12,
    }
    client = TestClient(create_app(repository=InMemoryListingRepository([row])))

    ranking = client.get("/api/listings/no-market-claim").json()["ranking"]

    assert ranking["status"] == "partial"
    assert ranking["overall_score"] is None
    assert all(reason["kind"] != "value" for reason in ranking["reasons"])
    assert "below" not in json.dumps(ranking["reasons"]).lower()


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("overall_score", ["a", "b", "c", "d", "e"]),
        ("value_score", ["a", "b", "c", "d", "e"]),
        ("campus_access_score", ["b", "c", "a", "d", "e"]),
        ("transit_score", ["a", "b", "c", "d", "e"]),
    ],
)
def test_ranking_sorts_are_deterministic_and_nulls_are_last(
    client: TestClient, sort: str, expected: list[str]
) -> None:
    rows = client.get(f"/api/listings?sort={sort}&order=desc&page_size=20").json()[
        "listings"
    ]
    assert [row["listing_id"] for row in rows] == expected


def test_ascending_sort_pagination_is_global_and_ties_use_listing_id(
    client: TestClient,
) -> None:
    first = client.get(
        "/api/listings?sort=overall_score&order=asc&page=1&page_size=1"
    ).json()
    second = client.get(
        "/api/listings?sort=overall_score&order=asc&page=2&page_size=1"
    ).json()
    assert first["listings"][0]["listing_id"] == "b"
    assert second["listings"][0]["listing_id"] == "a"
    assert first["total"] == second["total"] == 5


def test_ranking_filters_compose_with_listing_filters(client: TestClient) -> None:
    ranked = client.get(
        "/api/listings?ranking_status=ranked&min_score=70&bedrooms=4"
    ).json()
    assert [row["listing_id"] for row in ranked["listings"]] == ["a"]
    transit = client.get(
        "/api/listings?housing_type=house&min_transit_score=45&sort=transit_score"
    ).json()
    assert [row["listing_id"] for row in transit["listings"]] == ["a", "b"]
    priced = client.get(
        "/api/listings?min_price=850&max_price=950&sort=value_score"
    ).json()
    assert [row["listing_id"] for row in priced["listings"]] == ["b"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/listings?sort=unknown_score",
        "/api/listings?ranking_status=unknown",
        "/api/listings?min_score=80&max_score=20",
        "/api/listings?min_transit_score=101",
    ],
)
def test_invalid_ranking_queries_are_rejected(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 422
