from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid

import psycopg
import pytest

from pipeline.reference_data.london.coordinate_selection import (
    ShadowDecision,
    load_coordinate_selection_policy,
)
from pipeline.reference_data.london.coordinate_selection_shadow import (
    critical_table_fingerprints,
    generate_shadow_candidate,
    write_shadow_artifacts,
)


pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)


def _dataset_run(connection, dataset_name: str, fingerprint_character: str) -> int:
    return int(
        connection.execute(
            """
            insert into reference_data.dataset_runs (
                dataset_name, source_url, source_dataset_id, downloaded_at,
                source_schema, source_schema_sha256, content_sha256,
                feature_count, crs_srid, importer_version, validation_status,
                import_status, is_current
            ) values (
                %s, 'https://city.invalid', 'fixture', %s, '{}'::jsonb,
                repeat('a',64), repeat(%s,64), 1, 26917, 'test', 'passed',
                'promoted', true
            ) returning id
            """,
            (dataset_name, NOW, fingerprint_character),
        ).fetchone()[0]
    )


def _seed_shadow_fixture(connection) -> int:
    pipeline_run_id = int(
        connection.execute(
            """
            insert into public.housing_pipeline_runs (
                run_id, source, status, canonical_for_import, started_at,
                completed_at, manifest_json, import_status, canonical_sha256,
                manifest_sha256
            ) values (
                'coordinate-selection-fixture', 'uwo_offcampus', 'completed',
                true, %s, %s, '{}'::jsonb, 'completed', repeat('a',64),
                repeat('b',64)
            ) returning id
            """,
            (NOW, NOW),
        ).fetchone()[0]
    )
    geocode_result_id = int(
        connection.execute(
            """
            insert into public.housing_geocode_results (
                normalized_query, provider, provider_result_id, status,
                latitude, longitude, confidence, raw_provider_data
            ) values (
                '1 shadow test st', 'geoapify', 'shadow-result', 'ok',
                43.0, -81.25, 0.91, '{}'::jsonb
            ) returning id
            """
        ).fetchone()[0]
    )
    property_id = int(
        connection.execute(
            """
            insert into public.housing_properties (
                normalized_address, display_address, latitude, longitude,
                geocode_confidence, geocode_provider, geocode_status,
                geocode_result_id, address_complete, match_key
            ) values (
                '1 SHADOW TEST ST', '1 Shadow Test St', 43.0, -81.25, 0.91,
                'geoapify', 'ok', %s, true, 'coordinate-selection-property'
            ) returning id
            """,
            (geocode_result_id,),
        ).fetchone()[0]
    )
    listing_id = int(
        connection.execute(
            """
            insert into public.housing_listings (
                source, source_listing_id, source_url, property_id,
                first_seen_pipeline_run_id, last_seen_pipeline_run_id, status
            ) values (
                'uwo_offcampus', 'coordinate-selection-1',
                'https://example.invalid/coordinate-selection-1', %s, %s, %s,
                'active'
            ) returning id
            """,
            (property_id, pipeline_run_id, pipeline_run_id),
        ).fetchone()[0]
    )
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
            %s,%s,%s,%s,'new','Shadow fixture','1 Shadow Test St','$900',900,
            'month',900,1,'room','standard',false,43.0,-81.25,true,'ok',0.91,
            1.0,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'[]'::jsonb,
            repeat('c',64)
        )
        """,
        (listing_id, pipeline_run_id, property_id, NOW),
    )

    address_run = _dataset_run(connection, "municipal_addresses", "d")
    building_run = _dataset_run(connection, "building_footprints", "e")
    parcel_run = _dataset_run(connection, "parcels", "f")
    city_point_sql = """
        st_transform(st_setsrid(st_makepoint(-81.25,43.0),4326),26917)
    """
    address_id = int(
        connection.execute(
            f"""
            insert into reference_data.municipal_addresses (
                dataset_run_id, source_object_id, full_address,
                normalized_address, normalized_civic_address, status,
                source_attributes, geometry
            ) values (
                %s, 'address-1', '1 Shadow Test St', '1 SHADOW TEST ST',
                '1 SHADOW TEST ST', 'IA', '{{}}'::jsonb, {city_point_sql}
            ) returning id
            """,
            (address_run,),
        ).fetchone()[0]
    )
    building_id = int(
        connection.execute(
            f"""
            insert into reference_data.building_footprints (
                dataset_run_id, source_object_id, source_attributes, geometry
            ) values (
                %s, 'building-1', '{{}}'::jsonb,
                st_buffer({city_point_sql},10)
            ) returning id
            """,
            (building_run,),
        ).fetchone()[0]
    )
    parcel_id = int(
        connection.execute(
            f"""
            insert into reference_data.parcels (
                dataset_run_id, source_object_id, source_attributes, geometry
            ) values (
                %s, 'parcel-1', '{{}}'::jsonb,
                st_buffer({city_point_sql},20)
            ) returning id
            """,
            (parcel_run,),
        ).fetchone()[0]
    )
    resolution_run_id = uuid.uuid4()
    connection.execute(
        """
        insert into reference_data.property_resolution_runs (
            id, address_dataset_run_id, building_dataset_run_id,
            parcel_dataset_run_id, property_count, completed_at, status
        ) values (%s,%s,%s,%s,1,%s,'completed')
        """,
        (resolution_run_id, address_run, building_run, parcel_run, NOW),
    )
    connection.execute(
        """
        insert into reference_data.property_reference_matches (
            resolution_run_id, property_id, municipal_address_id,
            building_footprint_id, parcel_id, address_match_method,
            address_match_confidence, building_match_method,
            building_match_confidence, parcel_match_method,
            parcel_match_confidence, review_required, reason_codes, is_current
        ) values (
            %s,%s,%s,%s,%s,'EXACT_CIVIC_MATCH',0.98,'EXACT_CONTAINMENT',1.0,
            'ADDRESS_CONTAINMENT',1.0,false,'[]'::jsonb,true
        )
        """,
        (resolution_run_id, property_id, address_id, building_id, parcel_id),
    )
    connection.execute(
        """
        insert into public.housing_property_location_visibility (
            property_id, policy_version, location_status, map_visible,
            route_available, reason_codes, source_run_id, source_fingerprint,
            is_current, assessed_at
        ) values (
            %s,'location-visibility-v1','limited',true,false,
            '["city_reference_minor_offset"]'::jsonb,
            'coordinate-selection-fixture',repeat('1',64),true,%s
        )
        """,
        (property_id, NOW),
    )
    profile_id = int(
        connection.execute(
            """
            insert into public.housing_accessibility_profiles (
                cache_identity, origin_key, origin_type, origin_property_id,
                origin_latitude, origin_longitude, hotspot_id, travel_mode,
                representative_duration_seconds, minimum_duration_seconds,
                maximum_duration_seconds, distance_meters,
                walking_duration_seconds, provider, provider_profile,
                result_type, confidence, sample_count, calculated_at
            ) values (
                repeat('2',64),'property:fixture','property',%s,43.0,-81.25,
                'western-main-campus','walking',900,900,900,1200,900,'otp',
                'foot','exact_route',1.0,1,%s
            ) returning id
            """,
            (property_id, NOW),
        ).fetchone()[0]
    )
    connection.execute(
        """
        insert into public.housing_accessibility_samples (
            profile_id, departure_at, duration_seconds, status
        ) values (%s,%s,900,'available')
        """,
        (profile_id, NOW),
    )
    connection.execute(
        """
        insert into public.housing_walk_time_surfaces (
            id, property_id, origin_latitude, origin_longitude, cache_identity,
            routing_fingerprint, grid_fingerprint, network_fingerprint,
            r5py_version, r5_version, status
        ) values (
            %s,%s,43.0,-81.25,repeat('3',64),repeat('4',64),repeat('5',64),
            repeat('6',64),'test','test','computing'
        )
        """,
        (uuid.uuid4(), property_id),
    )
    ranking_run_id = int(
        connection.execute(
            """
            insert into public.housing_ranking_runs (
                run_id, ranking_version, status, config_fingerprint,
                input_fingerprint, output_fingerprint, listing_count,
                ranked_count, started_at, completed_at
            ) values (
                'coordinate-selection-ranking','ranking-v1','completed',
                repeat('7',64),repeat('8',64),repeat('9',64),1,1,%s,%s
            ) returning id
            """,
            (NOW, NOW),
        ).fetchone()[0]
    )
    connection.execute(
        """
        insert into public.housing_listing_scores (
            listing_id, ranking_run_id, ranking_version, ranking_status,
            overall_score, value_score, campus_access_score, transit_score,
            amenity_score, data_quality_score, explanation,
            input_fingerprint, is_current, computed_at
        ) values (
            %s,%s,'ranking-v1','ranked',80,80,80,80,80,80,'{}'::jsonb,
            repeat('a',64),true,%s
        )
        """,
        (listing_id, ranking_run_id, NOW),
    )
    return property_id


def test_shadow_generation_is_read_only_and_emits_provenance(
    postgres_database, postgres_target, tmp_path: Path
) -> None:
    property_id = _seed_shadow_fixture(postgres_database)
    before = critical_table_fingerprints(postgres_database)
    property_before = postgres_database.execute(
        """
        select latitude,longitude,geocode_provider,geocode_result_id
        from public.housing_properties where id=%s
        """,
        (property_id,),
    ).fetchone()
    observation_before = postgres_database.execute(
        """
        select latitude,longitude,distance_to_western_km,map_ready
        from public.housing_listing_observations where property_id=%s
        """,
        (property_id,),
    ).fetchone()

    with psycopg.connect(postgres_target.url) as connection:
        with connection.transaction():
            connection.execute("set transaction read only")
            run = generate_shadow_candidate(
                connection,
                load_coordinate_selection_policy(),
                evaluated_at=NOW,
            )

    paths = write_shadow_artifacts(run, tmp_path / "shadow")
    after = critical_table_fingerprints(postgres_database)
    record = run.records[0]

    assert before == after == run.mutation_safety["before"]
    assert run.mutation_safety["after"] == before
    assert record["property_id"] == property_id
    assert record["decision"] == ShadowDecision.CITY_SELECTED_SHADOW.value
    assert record["policy_version"] == "coordinate-selection-v1"
    assert record["city_dataset_fingerprint"] == "d" * 64
    assert record["geoapify_result_id"] is not None
    assert 1200.0 < record["parcel_area_square_meters"] < 1300.0
    assert record["parcel_building_count"] == 1
    assert record["parcel_municipal_address_count"] == 1
    assert record["geoapify_inside_parcel"] is True
    assert not any("routing_snap" in key for key in record)
    assert record["active_listing_count"] == 1
    assert record["accessibility_profile_count"] == 1
    assert record["walking_surface_count"] == 1
    assert record["current_ranking_row_count"] == 1
    assert run.summary["projected_location_visibility"]["transitions"] == {
        "limited_to_available": 1
    }
    assert all(path.exists() for path in paths.values())
    assert postgres_database.execute(
        """
        select latitude,longitude,geocode_provider,geocode_result_id
        from public.housing_properties where id=%s
        """,
        (property_id,),
    ).fetchone() == property_before
    assert postgres_database.execute(
        """
        select latitude,longitude,distance_to_western_km,map_ready
        from public.housing_listing_observations where property_id=%s
        """,
        (property_id,),
    ).fetchone() == observation_before
