from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.reference_data.london.coordinate_promotion import (
    FrozenCandidate,
    PromotionPlan,
    PromotionRow,
    capture_before_state,
    execute_disposable_promotion,
    finalize_local_promotion,
    install_disposable_promotion_store,
    load_promotion_config,
    promotion_state_fingerprint,
    rollback_disposable_promotion,
    validate_post_cutover,
)
from pipeline.reference_data.london.coordinate_selection_shadow import (
    critical_table_fingerprints,
)


pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _remove_disposable_promotion_store(postgres_database):
    """Keep proposed, non-migration tables inside this test's lifetime."""

    yield
    postgres_database.execute(
        "drop table if exists public.housing_coordinate_promotion_items"
    )
    postgres_database.execute(
        "drop table if exists public.housing_coordinate_promotion_runs"
    )


def _seed_fixture(connection) -> dict[str, int]:
    runs: list[int] = []
    for suffix in ("old", "current"):
        runs.append(
            int(
                connection.execute(
                    """
                    insert into public.housing_pipeline_runs (
                      run_id,source,status,canonical_for_import,started_at,
                      completed_at,manifest_json,import_status,canonical_sha256,
                      manifest_sha256
                    ) values (%s,'uwo_offcampus','completed',true,%s,%s,
                      '{}'::jsonb,'completed',repeat('a',64),repeat('b',64))
                    returning id
                    """,
                    (f"promotion-{suffix}", NOW, NOW),
                ).fetchone()[0]
            )
        )
    result_id = int(
        connection.execute(
            """
            insert into public.housing_geocode_results (
              normalized_query,provider,provider_result_id,status,latitude,
              longitude,confidence,raw_provider_data
            ) values ('1 promotion st','geoapify','promotion-result','ok',
              43.0,-81.25,0.91,'{}'::jsonb) returning id
            """
        ).fetchone()[0]
    )
    properties: dict[str, int] = {}
    for index, label in enumerate(("eligible", "conflict", "geo_fallback", "over_100"), 1):
        properties[label] = int(
            connection.execute(
                """
                insert into public.housing_properties (
                  normalized_address,display_address,latitude,longitude,
                  geocode_confidence,geocode_provider,geocode_status,
                  geocode_result_id,address_complete,match_key
                ) values (%s,%s,43.0,-81.25,0.91,'geoapify','ok',%s,true,%s)
                returning id
                """,
                (
                    f"{index} PROMOTION ST",
                    f"{index} Promotion St",
                    result_id,
                    f"promotion-{label}",
                ),
            ).fetchone()[0]
        )
    listing_id = int(
        connection.execute(
            """
            insert into public.housing_listings (
              source,source_listing_id,source_url,property_id,
              first_seen_pipeline_run_id,last_seen_pipeline_run_id,status
            ) values ('uwo_offcampus','promotion-1',
              'https://example.invalid/promotion-1',%s,%s,%s,'active') returning id
            """,
            (properties["eligible"], runs[0], runs[1]),
        ).fetchone()[0]
    )
    for offset, pipeline_run_id in enumerate(runs):
        connection.execute(
            """
            insert into public.housing_listing_observations (
              listing_id,pipeline_run_id,property_id,observed_at,change_type,
              title,address,price_text,price_numeric,price_period,price_monthly,
              bedrooms,housing_type,lease_type,is_sublet,latitude,longitude,
              map_ready,geocode_status,geocode_confidence,
              distance_to_western_km,raw_data,provenance_data,confidence_data,
              comparison_data,review_flags,observation_hash
            ) values (%s,%s,%s,%s,'new','Promotion fixture','1 Promotion St',
              '$900',900,'month',900,1,'room','standard',false,43.0,-81.25,
              true,'ok',0.91,1.0,%s::jsonb,%s::jsonb,'{}'::jsonb,'{}'::jsonb,
              '[]'::jsonb,%s)
            """,
            (
                listing_id,
                pipeline_run_id,
                properties["eligible"],
                NOW.replace(hour=10 + offset),
                f'{{"raw":"immutable-{offset}"}}',
                f'{{"source":"stage3-{offset}"}}',
                str(offset + 1) * 64,
            ),
        )
    connection.execute(
        """
        insert into public.housing_property_location_visibility (
          property_id,policy_version,location_status,map_visible,route_available,
          reason_codes,source_run_id,source_fingerprint,is_current,assessed_at
        ) values (%s,'location-visibility-v1','available',true,true,'[]'::jsonb,
          'promotion-fixture',repeat('1',64),true,%s)
        """,
        (properties["eligible"], NOW),
    )
    connection.execute(
        """
        insert into public.housing_accessibility_profiles (
          cache_identity,origin_key,origin_type,origin_property_id,
          origin_latitude,origin_longitude,hotspot_id,travel_mode,
          representative_duration_seconds,minimum_duration_seconds,
          maximum_duration_seconds,distance_meters,walking_duration_seconds,
          provider,provider_profile,result_type,confidence,sample_count,calculated_at
        ) values (repeat('2',64),'property:promotion','property',%s,43.0,-81.25,
          'western-main-campus','walking',900,900,900,1200,900,'otp','foot',
          'exact_route',1.0,1,%s)
        """,
        (properties["eligible"], NOW),
    )
    connection.execute(
        """
        insert into public.housing_walk_time_surfaces (
          id,property_id,origin_latitude,origin_longitude,cache_identity,
          routing_fingerprint,grid_fingerprint,network_fingerprint,r5py_version,
          r5_version,status,computed_at,duration_ms,reachable_count,
          unavailable_count,raw_length,compressed_length,compressed_payload,
          destination_validity_mask,etag
        ) values (gen_random_uuid(),%s,43.0,-81.25,repeat('3',64),repeat('4',64),
          repeat('5',64),repeat('6',64),'test','test','ready',%s,100,1,14802,
          29606,1,decode('00','hex'),decode(repeat('00',1851),'hex'),repeat('b',64))
        """,
        (properties["eligible"], NOW),
    )
    ranking_run = int(
        connection.execute(
            """
            insert into public.housing_ranking_runs (
              run_id,ranking_version,status,config_fingerprint,input_fingerprint,
              output_fingerprint,listing_count,ranked_count,started_at,completed_at
            ) values ('promotion-ranking','ranking-v1','completed',repeat('7',64),
              repeat('8',64),repeat('9',64),1,1,%s,%s) returning id
            """,
            (NOW, NOW),
        ).fetchone()[0]
    )
    connection.execute(
        """
        insert into public.housing_listing_scores (
          listing_id,ranking_run_id,ranking_version,ranking_status,overall_score,
          value_score,campus_access_score,transit_score,amenity_score,
          data_quality_score,explanation,input_fingerprint,is_current,computed_at
        ) values (%s,%s,'ranking-v1','ranked',80,80,80,80,80,80,'{}'::jsonb,
          repeat('a',64),true,%s)
        """,
        (listing_id, ranking_run, NOW),
    )
    return {**properties, "listing": listing_id}


def _plan(property_id: int) -> PromotionPlan:
    config = replace(load_promotion_config(), expected_city_selected=1)
    row = PromotionRow(
        property_id=property_id,
        display_address="1 Promotion St",
        previous_latitude=43.0,
        previous_longitude=-81.25,
        previous_source="geoapify",
        previous_status="ok",
        previous_confidence=0.91,
        previous_geocode_result_id=1,
        new_latitude=43.0001,
        new_longitude=-81.2501,
        new_distance_to_western_km=2.123,
        movement_meters=13.8,
        city_dataset_run_id=1,
        city_dataset_fingerprint="d" * 64,
        municipal_address_id=1,
        building_id=1,
        parcel_id=1,
        city_match_method="EXACT_CIVIC_MATCH",
        city_match_confidence=0.98,
        selection_reason="exact validated City point agrees with Geoapify",
        candidate_evidence_fingerprint="e" * 64,
        active_listing_count=1,
        latest_observation_count=1,
        accessibility_profile_count=1,
        walking_profile_count=1,
        cycling_profile_count=0,
        transit_profile_count=0,
        cached_exact_route_count=1,
        walking_surface_count=1,
        current_ranking_row_count=1,
    )
    frozen = FrozenCandidate((), (), {}, Path("fixture.csv"), config.candidate_fingerprint, config.coordinate_selection_policy_fingerprint)
    return PromotionPlan(
        config=config,
        frozen=frozen,
        eligible=(row,),
        stale=(),
        dependency_impact={
            "properties": 1,
            "active_listing_count": 1,
            "latest_observation_count": 1,
            "accessibility_profile_count": 1,
            "walking_profile_count": 1,
            "cycling_profile_count": 0,
            "transit_profile_count": 0,
            "cached_exact_route_count": 1,
            "walking_surface_count": 1,
            "current_ranking_row_count": 1,
        },
        movement_audit={},
        protected_before={},
        protected_after={},
        planned_at=NOW,
    )


def _coordinate_state(connection, fixture: dict[str, int]):
    properties = connection.execute(
        "select id,latitude,longitude,geocode_provider from public.housing_properties order by id"
    ).fetchall()
    observations = connection.execute(
        "select id,latitude,longitude,raw_data,provenance_data from public.housing_listing_observations order by id"
    ).fetchall()
    return properties, observations


def test_disposable_cutover_is_atomic_fail_closed_and_rollbackable(postgres_database) -> None:
    fixture = _seed_fixture(postgres_database)
    install_disposable_promotion_store(postgres_database, allow_disposable_test=True)
    plan = _plan(fixture["eligible"])
    snapshot = capture_before_state(postgres_database, plan)
    before = _coordinate_state(postgres_database, fixture)

    result = execute_disposable_promotion(
        postgres_database,
        plan,
        migration_run_id="coordinate-promotion-fixture",
        allow_disposable_test=True,
        before_state_fingerprint=snapshot.state_fingerprint,
    )

    assert result == {"properties": 1, "observations": 1, "accessibility": 1, "surfaces": 1, "ranking": 1, "visibility": 1}
    eligible_property = postgres_database.execute(
        "select latitude,longitude,geocode_provider,geocode_result_id from public.housing_properties where id=%s",
        (fixture["eligible"],),
    ).fetchone()
    assert eligible_property == (43.0001, -81.2501, "city_of_london", None)
    observations = postgres_database.execute(
        "select latitude,longitude,raw_data,provenance_data from public.housing_listing_observations order by observed_at,id"
    ).fetchall()
    assert observations[0][0:2] == (43.0, -81.25)
    assert observations[0][2] == {"raw": "immutable-0"}
    assert observations[1][0:2] == (43.0001, -81.2501)
    assert observations[1][2] == {"raw": "immutable-1"}
    assert observations[1][3]["coordinate_promotion"]["migration_run_id"] == "coordinate-promotion-fixture"
    public_projection = postgres_database.execute(
        """
        select latitude,longitude,location_status,location_map_visible,
               location_route_available
        from public.product_housing_listings where property_id=%s
        """,
        (fixture["eligible"],),
    ).fetchone()
    assert public_projection == (43.0001, -81.2501, "unavailable", False, False)
    untouched = postgres_database.execute(
        "select count(*) from public.housing_properties where id=any(%s) and latitude=43.0 and longitude=-81.25 and geocode_provider='geoapify'",
        ([fixture["conflict"], fixture["geo_fallback"], fixture["over_100"]],),
    ).fetchone()[0]
    assert untouched == 3
    assert postgres_database.execute("select is_stale from public.housing_accessibility_profiles").fetchone()[0] is True
    assert postgres_database.execute(
        "select status,error_code,compressed_payload,destination_validity_mask,etag "
        "from public.housing_walk_time_surfaces"
    ).fetchone() == ("failed", "origin_coordinate_changed", None, None, None)
    assert postgres_database.execute("select is_current from public.housing_listing_scores").fetchone()[0] is False
    assert postgres_database.execute("select is_current from public.housing_property_location_visibility").fetchone()[0] is False
    assert postgres_database.execute("select count(*) from public.housing_coordinate_promotion_items").fetchone()[0] == 1
    validation = validate_post_cutover(
        postgres_database,
        plan,
        snapshot,
        migration_run_id="coordinate-promotion-fixture",
        allow_disposable_test=True,
    )
    assert validation["property_mismatches"] == []
    assert validation["projection_mismatches"] == []
    assert validation["historical_observation_changes"] == []
    assert validation["raw_data_changes"] == []

    postgres_database.execute(
        "update public.housing_coordinate_promotion_runs set status='recomputing' "
        "where run_id='coordinate-promotion-fixture'"
    )
    final_fingerprint = finalize_local_promotion(
        postgres_database,
        "coordinate-promotion-fixture",
        recomputation_summary={"profiles": 1},
        validation_summary={"passed": True},
        allow_disposable_test=True,
    )
    assert final_fingerprint == promotion_state_fingerprint(
        postgres_database, "coordinate-promotion-fixture"
    )
    assert postgres_database.execute(
        "select status,final_state_fingerprint from "
        "public.housing_coordinate_promotion_runs where "
        "run_id='coordinate-promotion-fixture'"
    ).fetchone() == ("completed", final_fingerprint)

    rollback_disposable_promotion(
        postgres_database,
        promotion_run_id="coordinate-promotion-fixture",
        rollback_run_id="coordinate-promotion-fixture-rollback",
        allow_disposable_test=True,
    )

    assert _coordinate_state(postgres_database, fixture) == before
    assert postgres_database.execute(
        "select status,superseded_by_run_id is not null from public.housing_coordinate_promotion_runs where run_id='coordinate-promotion-fixture'"
    ).fetchone() == ("rolled_back", True)
    assert postgres_database.execute(
        "select status,rollback_of_run_id is not null from public.housing_coordinate_promotion_runs where run_id='coordinate-promotion-fixture-rollback'"
    ).fetchone() == ("rollback_completed", True)


def test_injected_halfway_failure_rolls_back_everything(postgres_database) -> None:
    fixture = _seed_fixture(postgres_database)
    install_disposable_promotion_store(postgres_database, allow_disposable_test=True)
    before_coordinates = _coordinate_state(postgres_database, fixture)
    before_protected = critical_table_fingerprints(postgres_database)

    with pytest.raises(RuntimeError, match="injected failure"):
        execute_disposable_promotion(
            postgres_database,
            _plan(fixture["eligible"]),
            migration_run_id="coordinate-promotion-injected-failure",
            allow_disposable_test=True,
            fail_after_property_update=True,
        )

    assert _coordinate_state(postgres_database, fixture) == before_coordinates
    assert critical_table_fingerprints(postgres_database) == before_protected
    assert postgres_database.execute("select count(*) from public.housing_coordinate_promotion_runs").fetchone()[0] == 0
    assert postgres_database.execute("select count(*) from public.housing_coordinate_promotion_items").fetchone()[0] == 0
