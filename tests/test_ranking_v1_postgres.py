from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.ranking_repository import PostgresRankingRepository
from backend.ranking_v1 import RankingResult, load_ranking_config


pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)


def insert_listing(connection) -> int:
    pipeline_run_id = connection.execute(
        """
        insert into public.housing_pipeline_runs (
            run_id, source, status, canonical_for_import, started_at,
            completed_at, manifest_json, import_status, canonical_sha256,
            manifest_sha256
        ) values (
            'ranking-fixture-run', 'uwo_offcampus', 'completed', true, %s, %s,
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
        ) values ('1 test st', '1 Test St', 43.0, -81.2, 'ok', true, 'ranking-test')
        returning id
        """
    ).fetchone()[0]
    return int(
        connection.execute(
            """
            insert into public.housing_listings (
                source, source_listing_id, source_url, property_id,
                first_seen_pipeline_run_id, last_seen_pipeline_run_id, status
            ) values (
                'uwo_offcampus', 'ranking-listing',
                'https://offcampus.uwo.ca/Listings/Details/ranking-listing',
                %s, %s, %s, 'active'
            ) returning id
            """,
            (property_id, pipeline_run_id, pipeline_run_id),
        ).fetchone()[0]
    )


def result(listing_id: int, *, fingerprint: str = "c" * 64) -> RankingResult:
    return RankingResult(
        listing_id=listing_id,
        source_listing_id="ranking-listing",
        property_id=1,
        ranking_version="ranking-v1",
        ranking_status="ranked",
        overall_score=75.0,
        value_score=80.0,
        campus_access_score=70.0,
        transit_score=72.5,
        amenity_score=None,
        data_quality_score=None,
        explanation={
            "ranking_version": "ranking-v1",
            "ranking_status": "ranked",
            "component_scores": {
                "value": 80.0,
                "campus_access": 70.0,
                "transit": 72.5,
                "amenities": None,
            },
        },
        input_fingerprint=fingerprint,
        computed_at=NOW,
    )


def start(repository, config, run_id: str, listing_count: int = 1) -> int:
    return repository.start_run(
        run_id=run_id,
        config=config,
        input_fingerprint="d" * 64,
        listing_count=listing_count,
        started_at=NOW,
    )


def test_ranking_persistence_is_idempotent_and_retains_history(
    postgres_database, postgres_target
) -> None:
    listing_id = insert_listing(postgres_database)
    config = load_ranking_config(Path("config/ranking-v1.toml"))
    repository = PostgresRankingRepository(postgres_target.url)
    first_result = result(listing_id)
    first_run = start(repository, config, "ranking-persist-first")

    first = repository.complete_run(
        ranking_run_id=first_run,
        results=[first_result],
        output_fingerprint="e" * 64,
        summary={"fixture": True},
        completed_at=NOW,
    )

    assert first == {"inserted": 1, "superseded": 0}
    assert repository.current_score_count(config.version) == (1, 1)
    assert repository.duplicate_current_count(config.version) == 0

    second_run = start(repository, config, "ranking-persist-second")
    second = repository.complete_run(
        ranking_run_id=second_run,
        results=[replace(first_result, computed_at=NOW.replace(day=10))],
        output_fingerprint="e" * 64,
        summary={"fixture": True},
        completed_at=NOW.replace(day=10),
    )

    assert second == {"inserted": 0, "superseded": 0}
    assert repository.current_score_count(config.version) == (1, 1)

    changed_run = start(repository, config, "ranking-persist-changed")
    changed = repository.complete_run(
        ranking_run_id=changed_run,
        results=[
            replace(
                first_result,
                input_fingerprint="f" * 64,
                overall_score=74.0,
                value_score=78.0,
                computed_at=NOW.replace(day=11),
            )
        ],
        output_fingerprint="1" * 64,
        summary={"fixture": True},
        completed_at=NOW.replace(day=11),
    )

    assert changed == {"inserted": 1, "superseded": 1}
    assert repository.current_score_count(config.version) == (1, 1)
    assert repository.duplicate_current_count(config.version) == 0
    assert postgres_database.execute(
        "select count(*) from public.housing_listing_scores"
    ).fetchone()[0] == 2
    assert postgres_database.execute(
        "select count(*) from public.housing_listing_scores where not is_current"
    ).fetchone()[0] == 1


def test_ranking_score_foreign_key_prevents_listing_deletion(
    postgres_database, postgres_target
) -> None:
    listing_id = insert_listing(postgres_database)
    config = load_ranking_config(Path("config/ranking-v1.toml"))
    repository = PostgresRankingRepository(postgres_target.url)
    run_id = start(repository, config, "ranking-delete-restrict")
    repository.complete_run(
        ranking_run_id=run_id,
        results=[result(listing_id)],
        output_fingerprint="e" * 64,
        summary={},
        completed_at=NOW,
    )

    with pytest.raises(Exception):
        postgres_database.execute(
            "delete from public.housing_listings where id = %s", (listing_id,)
        )
