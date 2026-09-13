from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.accessibility_repository import PostgresAccessibilityRepository
from backend.repository import PostgresListingRepository


pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)


def ranking_explanation(status: str) -> dict[str, object]:
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
            "value": {"monthly_price": 800, "market_median": 900},
            "campus_access": {"walking_minutes": 20, "cycling_minutes": 8},
            "transit_periods": [],
        },
        "confidence": {
            "accessibility_warning_reason_codes": [],
            "accessibility_review_required": False,
            "listing_review_flags": [],
        },
        "eligibility_reasons": [] if status == "ranked" else ["fixture"],
        "system_unavailable_components": ["amenities"],
        "amenity_status": "not_implemented",
        "data_quality_component_status": "not_scored",
        "input_fingerprint": "a" * 64,
        "computed_at": NOW.isoformat(),
    }


def seed_api_rows(connection) -> None:
    pipeline_run_id = connection.execute(
        """
        insert into public.housing_pipeline_runs (
            run_id, source, status, canonical_for_import, started_at,
            completed_at, manifest_json, import_status, canonical_sha256,
            manifest_sha256
        ) values (
            'ranking-api-fixture', 'uwo_offcampus', 'completed', true, %s, %s,
            '{}'::jsonb, 'completed', repeat('a', 64), repeat('b', 64)
        ) returning id
        """,
        (NOW, NOW),
    ).fetchone()[0]
    property_id = connection.execute(
        """
        insert into public.housing_properties (
            normalized_address, display_address, latitude, longitude,
            geocode_status, address_complete, match_key
        ) values (
            '1 api test st', '1 API Test St', 43.01, -81.27,
            'ok', true, 'ranking-api-property'
        ) returning id
        """
    ).fetchone()[0]

    listing_ids: dict[str, int] = {}
    for index, source_id in enumerate(("api-a", "api-b", "api-c", "api-d", "api-e")):
        listing_id = connection.execute(
            """
            insert into public.housing_listings (
                source, source_listing_id, source_url, property_id,
                first_seen_pipeline_run_id, last_seen_pipeline_run_id, status
            ) values (
                'uwo_offcampus', %s, %s, %s, %s, %s, 'active'
            ) returning id
            """,
            (
                source_id,
                f"https://offcampus.uwo.ca/Listings/Details/{source_id}",
                property_id,
                pipeline_run_id,
                pipeline_run_id,
            ),
        ).fetchone()[0]
        listing_ids[source_id] = int(listing_id)
        connection.execute(
            """
            insert into public.housing_listing_observations (
                listing_id, pipeline_run_id, property_id, observed_at,
                change_type, title, address, price_text, price_numeric,
                price_period, price_monthly, bedrooms, housing_type,
                lease_type, is_sublet, latitude, longitude, map_ready,
                geocode_status, geocode_confidence, distance_to_western_km,
                raw_data, provenance_data, confidence_data, comparison_data,
                review_flags, observation_hash
            ) values (
                %s,%s,%s,%s,'new',%s,'1 API Test St',%s,%s,'month',%s,
                %s,'house','standard',false,43.01,-81.27,true,'ok',0.95,1.2,
                '{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,
                '[]'::jsonb,%s
            )
            """,
            (
                listing_id,
                pipeline_run_id,
                property_id,
                NOW,
                f"API listing {source_id}",
                f"${800 + index * 100}",
                800 + index * 100,
                800 + index * 100,
                4 if source_id != "api-d" else 2,
                format(index + 1, "064x"),
            ),
        )

    run_ids: dict[str, int] = {}
    for version, suffix in (("ranking-v1", "v1"), ("ranking-v2", "v2")):
        run_ids[version] = int(
            connection.execute(
                """
                insert into public.housing_ranking_runs (
                    run_id, ranking_version, status, config_fingerprint,
                    input_fingerprint, output_fingerprint, listing_count,
                    ranked_count, started_at, completed_at
                ) values (%s,%s,'completed',repeat('c',64),repeat('d',64),
                    repeat('e',64),5,2,%s,%s) returning id
                """,
                (f"ranking-api-{suffix}", version, NOW, NOW),
            ).fetchone()[0]
        )

    current_values = {
        "api-a": ("ranked", 80, 90, 70, 60),
        "api-b": ("ranked", 80, 70, 90, 50),
        "api-c": ("partial", None, None, 80, 40),
        "api-d": ("excluded", None, None, None, None),
    }
    for source_id, values in current_values.items():
        status, overall, value, campus, transit = values
        connection.execute(
            """
            insert into public.housing_listing_scores (
                listing_id, ranking_run_id, ranking_version, ranking_status,
                overall_score, value_score, campus_access_score, transit_score,
                amenity_score, data_quality_score, explanation,
                input_fingerprint, is_current, computed_at
            ) values (%s,%s,'ranking-v1',%s,%s,%s,%s,%s,null,null,%s::jsonb,
                repeat('a',64),true,%s)
            """,
            (
                listing_ids[source_id],
                run_ids["ranking-v1"],
                status,
                overall,
                value,
                campus,
                transit,
                json.dumps(ranking_explanation(status)),
                NOW,
            ),
        )

    # Historical v1 and a current v2 score must never affect the v1 API join.
    connection.execute(
        """
        insert into public.housing_listing_scores (
            listing_id, ranking_run_id, ranking_version, ranking_status,
            overall_score, value_score, campus_access_score, transit_score,
            explanation, input_fingerprint, is_current, computed_at,
            superseded_at
        ) values (%s,%s,'ranking-v1','ranked',10,10,10,10,%s::jsonb,
            repeat('f',64),false,%s,%s)
        """,
        (
            listing_ids["api-a"],
            run_ids["ranking-v1"],
            json.dumps(ranking_explanation("ranked")),
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        insert into public.housing_listing_scores (
            listing_id, ranking_run_id, ranking_version, ranking_status,
            overall_score, value_score, campus_access_score, transit_score,
            explanation, input_fingerprint, is_current, computed_at
        ) values (%s,%s,'ranking-v2','ranked',99,99,99,99,%s::jsonb,
            repeat('9',64),true,%s)
        """,
        (
            listing_ids["api-a"],
            run_ids["ranking-v2"],
            json.dumps(ranking_explanation("ranked")),
            NOW,
        ),
    )


def test_postgres_api_selects_exact_current_v1_without_duplicate_rows(
    postgres_database, postgres_target
) -> None:
    seed_api_rows(postgres_database)
    client = TestClient(
        create_app(repository=PostgresListingRepository(postgres_target.url))
    )

    collection = client.get("/api/listings?page_size=20").json()
    rows = {row["listing_id"]: row for row in collection["listings"]}
    assert collection["total"] == 5
    assert len(collection["listings"]) == 5
    assert rows["api-a"]["ranking"]["version"] == "ranking-v1"
    assert rows["api-a"]["ranking"]["overall_score"] == 80.0
    assert rows["api-c"]["ranking"]["status"] == "partial"
    assert rows["api-c"]["ranking"]["overall_score"] is None
    assert rows["api-d"]["ranking"]["status"] == "excluded"
    assert rows["api-d"]["ranking"]["overall_score"] is None
    assert rows["api-e"]["ranking"] is None
    assert client.get("/api/listings/api-a").json()["ranking"] == rows["api-a"][
        "ranking"
    ]


def test_postgres_ranking_filter_sort_and_pagination_happen_before_slicing(
    postgres_database, postgres_target
) -> None:
    seed_api_rows(postgres_database)
    client = TestClient(
        create_app(repository=PostgresListingRepository(postgres_target.url))
    )

    page_one = client.get(
        "/api/listings?ranking_status=ranked&sort=overall_score&order=desc&page_size=1"
    ).json()
    page_two = client.get(
        "/api/listings?ranking_status=ranked&sort=overall_score&order=desc&page=2&page_size=1"
    ).json()
    assert page_one["total"] == page_two["total"] == 2
    assert page_one["listings"][0]["listing_id"] == "api-a"
    assert page_two["listings"][0]["listing_id"] == "api-b"

    composed = client.get(
        "/api/listings?bedrooms=4&min_score=70&min_value_score=80"
    ).json()
    assert [row["listing_id"] for row in composed["listings"]] == ["api-a"]


def test_ranked_view_join_plan_uses_current_identity_and_sort_indexes(
    postgres_database,
) -> None:
    seed_api_rows(postgres_database)
    definition = postgres_database.execute(
        "select pg_get_viewdef('public.ranked_housing_listings'::regclass, true)"
    ).fetchone()[0]
    assert "score.ranking_version = 'ranking-v1'" in definition
    assert "score.is_current" in definition
    assert postgres_database.execute(
        """
        select count(*) from public.ranked_housing_listings
        where ranking_status = 'ranked' and ranking_overall_score >= 70
        """
    ).fetchone()[0] == 2


def test_postgres_student_discovery_filters_use_persisted_profiles(
    postgres_database, postgres_target
) -> None:
    seed_api_rows(postgres_database)
    property_ids = []
    for suffix in ("slow", "missing"):
        property_ids.append(
            postgres_database.execute(
                """
                insert into public.housing_properties (
                    normalized_address, display_address, latitude, longitude,
                    geocode_status, address_complete, match_key
                ) values (%s,%s,43.01,-81.27,'ok',true,%s) returning id
                """,
                (f"{suffix} api test st", f"{suffix} API Test St", f"api-{suffix}"),
            ).fetchone()[0]
        )
    original_property = postgres_database.execute(
        """
        select property_id from public.housing_listings
        where source_listing_id = 'api-a'
        """
    ).fetchone()[0]
    postgres_database.execute(
        """
        update public.housing_listings
        set property_id = case
            when source_listing_id = 'api-b' then %s
            when source_listing_id in ('api-c','api-d','api-e') then %s
            else property_id
        end
        """,
        tuple(property_ids),
    )
    postgres_database.execute(
        """
        update public.housing_listing_observations observation
        set housing_type = case listing.source_listing_id
            when 'api-a' then 'house_to_share'
            when 'api-b' then 'apartment_to_share'
            else 'house'
        end,
        property_id = listing.property_id
        from public.housing_listings listing
        where listing.id = observation.listing_id
        """
    )
    for index, (property_id, mode, period, duration) in enumerate(
        (
            (original_property, "walking", None, 900),
            (
                original_property,
                "transit",
                "weekday_morning_commute",
                1200,
            ),
            (property_ids[0], "walking", None, 1800),
            (
                property_ids[0],
                "transit",
                "weekday_morning_commute",
                2400,
            ),
        ),
        start=1,
    ):
        postgres_database.execute(
            """
            insert into public.housing_accessibility_profiles (
                cache_identity, origin_key, origin_type, origin_property_id,
                origin_latitude, origin_longitude, hotspot_id, travel_mode,
                day_type, time_period, representative_duration_seconds,
                provider, provider_profile, result_type, confidence,
                sample_count, calculated_at, schedule_version,
                network_version, expires_at, provider_metadata
            ) values (
                %s,%s,'property',%s,43.01,-81.27,'western-main-campus',%s,
                %s,%s,%s,'opentripplanner','transport-v2','exact_route',
                0.95,3,%s,%s,'osm-v1',%s,
                '{"quality_status":"complete"}'::jsonb
            )
            """,
            (
                format(index, "064x"),
                f"property:{property_id}",
                property_id,
                mode,
                "weekday" if mode == "transit" else None,
                period,
                duration,
                NOW,
                "gtfs-v1" if mode == "transit" else None,
                datetime(2027, 8, 9, tzinfo=timezone.utc),
            ),
        )

    postgres_database.execute(
        """
        insert into public.housing_property_location_visibility (
            property_id, policy_version, location_status, map_visible,
            route_available, reason_codes, source_run_id, source_fingerprint
        )
        select distinct property_id, 'location-visibility-v1', 'available',
               true, true, '[]'::jsonb, 'ranking-api-fixture', repeat('f', 64)
        from public.housing_listings
        where status = 'active'
        """
    )

    client = TestClient(
        create_app(
            repository=PostgresListingRepository(postgres_target.url),
            accessibility_repository=PostgresAccessibilityRepository(
                postgres_target.url
            ),
            accessibility_provider_name="opentripplanner",
            accessibility_provider_profile="transport-v2",
            schedule_version="gtfs-v1",
            network_version="osm-v1",
        )
    )

    roommates = client.get("/api/listings?roommates_wanted=true").json()
    assert {row["listing_id"] for row in roommates["listings"]} == {
        "api-a",
        "api-b",
    }
    walk = client.get("/api/listings?max_walk_minutes=20").json()
    assert [row["listing_id"] for row in walk["listings"]] == ["api-a"]
    transit = client.get("/api/listings?max_transit_minutes=30").json()
    assert [row["listing_id"] for row in transit["listings"]] == ["api-a"]
    composed = client.get(
        "/api/listings?roommates_wanted=true&housing_type=apartment_to_share"
        "&max_price=950&bedrooms=4&max_transit_minutes=45"
    ).json()
    assert [row["listing_id"] for row in composed["listings"]] == ["api-b"]
    assert client.get("/api/listings").json()["total"] == 5
