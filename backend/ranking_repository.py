"""PostgreSQL feature extraction and idempotent Ranking v1 persistence."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from typing import Any, Iterator, Mapping

from backend.ranking_v1 import (
    DirectAccessFeature,
    ListingFeatures,
    RankingConfig,
    RankingResult,
    TransitPeriodFeature,
)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    return []


def _dict_rows(cursor: Any) -> list[dict[str, Any]]:
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


class PostgresRankingRepository:
    """Bulk feature loader and transactional listing-score store."""

    def __init__(self, database_url: str) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self.database_url = database_url

    @contextmanager
    def connect(self) -> Iterator[Any]:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "PostgreSQL ranking requires requirements-database.txt"
            ) from exc
        with psycopg.connect(self.database_url) as connection:
            yield connection

    def ranking_schema_present(self) -> bool:
        with self.connect() as connection:
            return bool(
                connection.execute(
                    "select to_regclass('public.housing_listing_scores') is not null"
                ).fetchone()[0]
            )

    def load_listing_features(self, config: RankingConfig) -> list[ListingFeatures]:
        """Bulk-load current listing observations and compatible accessibility."""

        with self.connect() as connection:
            listings = _dict_rows(
                connection.execute(
                    """
                    with latest as (
                        select distinct on (o.listing_id) o.*
                        from public.housing_listing_observations o
                        order by o.listing_id, o.observed_at desc, o.id desc
                    )
                    select l.id as listing_id, l.source_listing_id, l.property_id,
                           l.status as listing_status, latest.observation_hash,
                           latest.address, latest.price_monthly, latest.price_period,
                           latest.bedrooms, latest.housing_type, latest.lease_type,
                           latest.is_sublet, latest.amenities, latest.review_flags
                    from public.housing_listings l
                    join latest on latest.listing_id = l.id
                    where l.source = 'uwo_offcampus'
                      and l.status in ('active', 'possibly_removed', 'relisted')
                    order by l.id
                    """
                )
            )
            property_ids = sorted(
                {int(row["property_id"]) for row in listings if row["property_id"] is not None}
            )
            profiles = (
                _dict_rows(
                    connection.execute(
                        """
                        select id, cache_identity, origin_property_id, travel_mode,
                               time_period, representative_duration_seconds,
                               minimum_duration_seconds, maximum_duration_seconds,
                               distance_meters, walking_duration_seconds,
                               transfer_count, provider_metadata
                        from public.housing_accessibility_profiles
                        where origin_property_id = any(%s::bigint[])
                          and hotspot_id = %s and provider = %s
                          and provider_profile = %s and network_version = %s
                          and (travel_mode <> 'transit' or schedule_version = %s)
                          and not is_stale
                          and (expires_at is null or expires_at > now())
                        order by origin_property_id, travel_mode, time_period, id
                        """,
                        (
                            property_ids,
                            config.hotspot_id,
                            config.provider,
                            config.provider_profile,
                            config.network_version,
                            config.schedule_version,
                        ),
                    )
                )
                if property_ids
                else []
            )
            profile_ids = [int(row["id"]) for row in profiles]
            samples = (
                _dict_rows(
                    connection.execute(
                        """
                        select profile_id, departure_at, status, provider_metadata
                        from public.housing_accessibility_samples
                        where profile_id = any(%s::bigint[])
                        order by profile_id, departure_at
                        """,
                        (profile_ids,),
                    )
                )
                if profile_ids
                else []
            )

        samples_by_profile: dict[int, list[dict[str, Any]]] = {}
        for row in samples:
            samples_by_profile.setdefault(int(row["profile_id"]), []).append(row)
        profiles_by_property: dict[int, dict[tuple[str, str | None], dict[str, Any]]] = {}
        for row in profiles:
            property_id = int(row["origin_property_id"])
            key = (str(row["travel_mode"]), row["time_period"])
            property_profiles = profiles_by_property.setdefault(property_id, {})
            if key in property_profiles:
                raise RuntimeError(
                    f"Duplicate compatible accessibility identity for property {property_id}: {key}"
                )
            property_profiles[key] = row

        output: list[ListingFeatures] = []
        for row in listings:
            property_id = int(row["property_id"]) if row["property_id"] is not None else None
            property_profiles = profiles_by_property.get(property_id or -1, {})
            walking = self._direct_feature(property_profiles.get(("walking", None)))
            cycling = self._direct_feature(property_profiles.get(("cycling", None)))
            transit = tuple(
                self._transit_feature(profile, samples_by_profile.get(int(profile["id"]), []))
                for key, profile in sorted(property_profiles.items())
                if key[0] == "transit"
            )
            output.append(
                ListingFeatures(
                    listing_id=int(row["listing_id"]),
                    source_listing_id=str(row["source_listing_id"]),
                    property_id=property_id,
                    listing_status=str(row["listing_status"]),
                    observation_hash=str(row["observation_hash"]).strip(),
                    address=str(row["address"]) if row["address"] is not None else None,
                    price_monthly=(
                        float(row["price_monthly"])
                        if row["price_monthly"] is not None
                        else None
                    ),
                    price_period=(
                        str(row["price_period"]) if row["price_period"] is not None else None
                    ),
                    bedrooms=int(row["bedrooms"]) if row["bedrooms"] is not None else None,
                    housing_type=(
                        str(row["housing_type"]) if row["housing_type"] is not None else None
                    ),
                    lease_type=(
                        str(row["lease_type"]) if row["lease_type"] is not None else None
                    ),
                    is_sublet=row["is_sublet"],
                    documented_amenities=tuple(
                        str(value) for value in _json_array(row["amenities"])
                    ),
                    review_flags=tuple(
                        str(value) for value in _json_array(row["review_flags"])
                    ),
                    walking=walking,
                    cycling=cycling,
                    transit_periods=transit,
                )
            )
        return output

    @staticmethod
    def _direct_feature(row: dict[str, Any] | None) -> DirectAccessFeature | None:
        if row is None or row["representative_duration_seconds"] is None:
            return None
        return DirectAccessFeature(
            profile_id=int(row["id"]),
            cache_identity=str(row["cache_identity"]).strip(),
            duration_seconds=int(row["representative_duration_seconds"]),
            distance_meters=(
                int(row["distance_meters"]) if row["distance_meters"] is not None else None
            ),
        )

    @staticmethod
    def _transit_feature(
        row: dict[str, Any], samples: list[dict[str, Any]]
    ) -> TransitPeriodFeature:
        metadata = _json_object(row["provider_metadata"])
        available = 0
        no_route = 0
        walking_better = 0
        other_unavailable = 0
        for sample in samples:
            if sample["status"] == "available":
                available += 1
                continue
            sample_metadata = _json_object(sample["provider_metadata"])
            codes = {
                str(value.get("code"))
                for value in sample_metadata.get("routing_diagnostics", [])
                if isinstance(value, dict) and value.get("code")
            }
            if codes == {"no_route"}:
                no_route += 1
            elif codes == {"walking_better_than_transit"}:
                walking_better += 1
            else:
                other_unavailable += 1
        return TransitPeriodFeature(
            profile_id=int(row["id"]),
            cache_identity=str(row["cache_identity"]).strip(),
            time_period=str(row["time_period"]),
            representative_duration_seconds=(
                int(row["representative_duration_seconds"])
                if row["representative_duration_seconds"] is not None
                else None
            ),
            minimum_duration_seconds=(
                int(row["minimum_duration_seconds"])
                if row["minimum_duration_seconds"] is not None
                else None
            ),
            maximum_duration_seconds=(
                int(row["maximum_duration_seconds"])
                if row["maximum_duration_seconds"] is not None
                else None
            ),
            walking_duration_seconds=(
                int(row["walking_duration_seconds"])
                if row["walking_duration_seconds"] is not None
                else None
            ),
            transfer_count=(
                int(row["transfer_count"]) if row["transfer_count"] is not None else None
            ),
            available_sample_count=available,
            requested_sample_count=int(metadata.get("requested_sample_count") or len(samples) or 3),
            no_route_sample_count=no_route,
            walking_better_sample_count=walking_better,
            other_unavailable_sample_count=other_unavailable,
            quality_status=(
                str(metadata["quality_status"])
                if metadata.get("quality_status") is not None
                else None
            ),
            reason_codes=tuple(str(value) for value in metadata.get("quality_reason_codes", [])),
        )

    def current_input_fingerprints(self, version: str) -> dict[int, str]:
        if not self.ranking_schema_present():
            return {}
        with self.connect() as connection:
            return {
                int(row[0]): str(row[1]).strip()
                for row in connection.execute(
                    """
                    select listing_id, input_fingerprint
                    from public.housing_listing_scores
                    where ranking_version = %s and is_current
                    """,
                    (version,),
                )
            }

    def start_run(
        self,
        *,
        run_id: str,
        config: RankingConfig,
        input_fingerprint: str,
        listing_count: int,
        started_at: Any,
    ) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                insert into public.housing_ranking_runs (
                    run_id, ranking_version, status, config_fingerprint,
                    input_fingerprint, listing_count, started_at
                ) values (%s,%s,'running',%s,%s,%s,%s)
                returning id
                """,
                (
                    run_id,
                    config.version,
                    config.fingerprint,
                    input_fingerprint,
                    listing_count,
                    started_at,
                ),
            ).fetchone()
        return int(row[0])

    def complete_run(
        self,
        *,
        ranking_run_id: int,
        results: list[RankingResult],
        output_fingerprint: str,
        summary: Mapping[str, Any],
        completed_at: Any,
    ) -> dict[str, int]:
        inserted = 0
        superseded = 0
        seen: set[int] = set()
        with self.connect() as connection, connection.transaction():
            for result in results:
                if result.listing_id in seen:
                    raise ValueError(f"Duplicate ranking result for listing {result.listing_id}")
                seen.add(result.listing_id)
                current = connection.execute(
                    """
                    select id, input_fingerprint
                    from public.housing_listing_scores
                    where listing_id = %s and ranking_version = %s and is_current
                    for update
                    """,
                    (result.listing_id, result.ranking_version),
                ).fetchone()
                if current is not None and str(current[1]).strip() == result.input_fingerprint:
                    continue
                if current is not None:
                    connection.execute(
                        """
                        update public.housing_listing_scores
                        set is_current = false, superseded_at = %s
                        where id = %s and is_current
                        """,
                        (completed_at, current[0]),
                    )
                    superseded += 1
                connection.execute(
                    """
                    insert into public.housing_listing_scores (
                        listing_id, ranking_run_id, ranking_version,
                        ranking_status, overall_score, value_score,
                        campus_access_score, transit_score, amenity_score,
                        data_quality_score, explanation, input_fingerprint,
                        computed_at
                    ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                    """,
                    (
                        result.listing_id,
                        ranking_run_id,
                        result.ranking_version,
                        result.ranking_status,
                        result.overall_score,
                        result.value_score,
                        result.campus_access_score,
                        result.transit_score,
                        result.amenity_score,
                        result.data_quality_score,
                        json.dumps(result.explanation, sort_keys=True),
                        result.input_fingerprint,
                        result.computed_at,
                    ),
                )
                inserted += 1
            counts = {
                status: sum(result.ranking_status == status for result in results)
                for status in ("ranked", "partial", "excluded")
            }
            connection.execute(
                """
                update public.housing_ranking_runs
                set status = 'completed', output_fingerprint = %s,
                    ranked_count = %s, partial_count = %s, excluded_count = %s,
                    score_rows_inserted = %s, score_rows_superseded = %s,
                    summary = %s::jsonb, completed_at = %s
                where id = %s and status = 'running'
                """,
                (
                    output_fingerprint,
                    counts["ranked"],
                    counts["partial"],
                    counts["excluded"],
                    inserted,
                    superseded,
                    json.dumps(dict(summary), sort_keys=True),
                    completed_at,
                    ranking_run_id,
                ),
            )
        return {"inserted": inserted, "superseded": superseded}

    def fail_run(self, ranking_run_id: int, message: str, completed_at: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                update public.housing_ranking_runs
                set status = 'failed', error_message = %s, completed_at = %s
                where id = %s and status = 'running'
                """,
                (message[:1000], completed_at, ranking_run_id),
            )

    def current_score_count(self, version: str) -> tuple[int, int]:
        with self.connect() as connection:
            row = connection.execute(
                """
                select count(*), count(distinct listing_id)
                from public.housing_listing_scores
                where ranking_version = %s and is_current
                """,
                (version,),
            ).fetchone()
        return int(row[0]), int(row[1])

    def duplicate_current_count(self, version: str) -> int:
        with self.connect() as connection:
            return int(
                connection.execute(
                    """
                    select count(*) from (
                        select listing_id
                        from public.housing_listing_scores
                        where ranking_version = %s and is_current
                        group by listing_id having count(*) > 1
                    ) duplicates
                    """,
                    (version,),
                ).fetchone()[0]
            )


def apply_ranking_migration(database_url: str, migration_path: Path) -> None:
    """Apply only the reviewed additive ranking migration to a caller-approved DB."""

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("PostgreSQL ranking requires requirements-database.txt") from exc
    with psycopg.connect(database_url) as connection:
        connection.execute(migration_path.read_text(encoding="utf-8"))
