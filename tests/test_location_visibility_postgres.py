from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipeline.location_visibility import (
    LocationVisibilityRow,
    sync_location_visibility,
)


pytestmark = pytest.mark.postgres


def _seed_active_property(connection) -> int:
    now = datetime.now(timezone.utc)
    pipeline_run_id = connection.execute(
        """
        insert into public.housing_pipeline_runs (
            run_id, source, status, canonical_for_import, started_at,
            completed_at, manifest_json, import_status, canonical_sha256,
            manifest_sha256
        ) values (
            'location-visibility-fixture', 'uwo_offcampus', 'completed', true,
            %s, %s, '{}'::jsonb, 'completed', repeat('a', 64), repeat('b', 64)
        ) returning id
        """,
        (now, now),
    ).fetchone()[0]
    property_id = connection.execute(
        """
        insert into public.housing_properties (
            normalized_address, display_address, latitude, longitude,
            geocode_status, address_complete, match_key
        ) values (
            '1 visibility test st', '1 Visibility Test St', 43.01, -81.27,
            'ok', true, 'location-visibility-property'
        ) returning id
        """
    ).fetchone()[0]
    listing_id = connection.execute(
        """
        insert into public.housing_listings (
            source, source_listing_id, source_url, property_id,
            first_seen_pipeline_run_id, last_seen_pipeline_run_id, status
        ) values (
            'uwo_offcampus', 'visibility-1',
            'https://offcampus.uwo.ca/Listings/Details/visibility-1',
            %s, %s, %s, 'active'
        ) returning id
        """,
        (property_id, pipeline_run_id, pipeline_run_id),
    ).fetchone()[0]
    connection.execute(
        """
        insert into public.housing_listing_observations (
            listing_id, pipeline_run_id, property_id, observed_at, change_type,
            title, address, price_text, price_numeric, price_period,
            price_monthly, bedrooms, housing_type, lease_type, is_sublet,
            latitude, longitude, map_ready, geocode_status,
            geocode_confidence, distance_to_western_km, raw_data,
            provenance_data, confidence_data, comparison_data, review_flags,
            observation_hash
        ) values (
            %s,%s,%s,%s,'new','Visibility fixture','1 Visibility Test St',
            '$800 per month',800,'month',800,1,'room','standard',false,
            43.01,-81.27,true,'ok',0.95,1.2,'{}'::jsonb,'{}'::jsonb,
            '{}'::jsonb,'{}'::jsonb,'[]'::jsonb,repeat('c',64)
        )
        """,
        (listing_id, pipeline_run_id, property_id, now),
    )
    return int(property_id)


def _decision(property_id: int, *, status: str = "available") -> LocationVisibilityRow:
    flags = {
        "available": (True, True),
        "limited": (True, False),
        "unavailable": (False, False),
    }
    map_visible, route_available = flags[status]
    return LocationVisibilityRow(
        property_id=property_id,
        policy_version="location-visibility-v1",
        location_status=status,
        map_visible=map_visible,
        route_available=route_available,
        reason_codes=() if status == "available" else ("fixture_reason",),
        source_run_id="location-visibility-fixture",
        source_fingerprint="d" * 64,
    )


def test_sync_is_complete_idempotent_and_retains_superseded_history(
    postgres_database, postgres_target
) -> None:
    property_id = _seed_active_property(postgres_database)
    before = postgres_database.execute(
        """
        select location_status, location_map_visible, location_route_available
        from public.product_housing_listings
        """
    ).fetchone()
    assert before == ("unavailable", False, False)

    first = sync_location_visibility(postgres_target.url, [_decision(property_id)])
    repeated = sync_location_visibility(postgres_target.url, [_decision(property_id)])
    changed = sync_location_visibility(
        postgres_target.url, [_decision(property_id, status="limited")]
    )

    assert (first.inserted_rows, first.unchanged_rows) == (1, 0)
    assert (repeated.inserted_rows, repeated.unchanged_rows) == (0, 1)
    assert (changed.inserted_rows, changed.superseded_rows) == (1, 1)
    assert postgres_database.execute(
        """
        select count(1), count(1) filter (where is_current),
               max(location_status) filter (where is_current)
        from public.housing_property_location_visibility
        """
    ).fetchone() == (2, 1, "limited")
