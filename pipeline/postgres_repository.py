"""Transactional psycopg 3 adapter for the Stage 4 import plan."""

from __future__ import annotations

import warnings
from dataclasses import replace
from typing import Any, Callable, Optional

from pipeline.database_importer import (
    GEOCODE_PROVIDER,
    ExistingState,
    ImportPlan,
    ImportValidationError,
    StoredListing,
    StoredProperty,
    ValidatedRun,
    build_import_plan,
    import_configuration,
    sanitize_error,
)


def _driver():
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError as error:  # pragma: no cover - depends on optional runtime dep.
        raise RuntimeError(
            "Connected Stage 4 imports require psycopg 3; install project requirements"
        ) from error
    return psycopg, Jsonb


class PostgresRepository:
    """One PostgreSQL access layer using parameterized SQL and explicit transactions."""

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._psycopg, self._Jsonb = _driver()

    def connect(self):
        return self._psycopg.connect(self._database_url)

    def completed_summary(
        self, run: ValidatedRun, selected_configuration: dict[str, Any]
    ) -> Optional[tuple[dict[str, Any], bool, tuple[str, ...]]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                select import_status, canonical_sha256, manifest_sha256,
                       change_summary, lifecycle_updates_applied,
                       lifecycle_ineligibility_reasons, import_configuration
                from public.housing_pipeline_runs
                where run_id = %s
                """,
                (run.run_id,),
            ).fetchone()
        if row is None:
            return None
        import_status, canonical_sha256, manifest_sha256, summary = row[:4]
        if canonical_sha256 != run.canonical_sha256 or manifest_sha256 != run.manifest_sha256:
            raise ImportValidationError(
                "The selected run_id already exists with different manifest or canonical content"
            )
        if import_status != "completed":
            return None
        if dict(row[6]) != selected_configuration:
            raise ImportValidationError(
                "The selected run_id was already imported with different Stage 4 configuration"
            )
        return dict(summary), bool(row[4]), tuple(row[5])

    def mark_import_started(
        self, run: ValidatedRun, selected_configuration: dict[str, Any]
    ) -> None:
        manifest = run.manifest
        with self.connect() as connection:
            connection.execute(
                """
                insert into public.housing_pipeline_runs (
                    run_id, source, status, canonical_for_import,
                    source_git_commit, git_dirty, started_at, completed_at,
                    manifest_json, import_started_at, import_status, import_error,
                    import_override_used, import_configuration,
                    canonical_sha256, manifest_sha256,
                    lifecycle_updates_eligible, lifecycle_updates_applied,
                    lifecycle_ineligibility_reasons, updated_at
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    now(), 'running', null, %s, %s, %s, %s, %s, false, %s, now()
                )
                on conflict (run_id) do update set
                    source = excluded.source,
                    status = excluded.status,
                    canonical_for_import = excluded.canonical_for_import,
                    source_git_commit = excluded.source_git_commit,
                    git_dirty = excluded.git_dirty,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    manifest_json = excluded.manifest_json,
                    import_started_at = now(),
                    import_completed_at = null,
                    import_status = 'running',
                    import_error = null,
                    import_override_used = excluded.import_override_used,
                    import_configuration = excluded.import_configuration,
                    lifecycle_updates_eligible = excluded.lifecycle_updates_eligible,
                    lifecycle_updates_applied = false,
                    lifecycle_ineligibility_reasons = excluded.lifecycle_ineligibility_reasons,
                    updated_at = now()
                where public.housing_pipeline_runs.import_status <> 'completed'
                """,
                (
                    run.run_id,
                    run.source,
                    manifest["status"],
                    bool(manifest.get("canonical_for_import")),
                    manifest.get("git_commit"),
                    manifest.get("git_dirty"),
                    manifest.get("created_at_utc"),
                    run.observed_at,
                    self._Jsonb(manifest),
                    run.override_used,
                    self._Jsonb(selected_configuration),
                    run.canonical_sha256,
                    run.manifest_sha256,
                    run.lifecycle_eligible,
                    self._Jsonb(list(run.lifecycle_ineligibility_reasons)),
                ),
            )

    def completed_summary_in_transaction(
        self,
        connection,
        run: ValidatedRun,
        selected_configuration: dict[str, Any],
    ) -> Optional[tuple[dict[str, Any], bool, tuple[str, ...]]]:
        row = connection.execute(
            """
            select import_status, canonical_sha256, manifest_sha256,
                   change_summary, lifecycle_updates_applied,
                   lifecycle_ineligibility_reasons, import_configuration
            from public.housing_pipeline_runs
            where run_id = %s
            for update
            """,
            (run.run_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Pipeline run import record disappeared")
        if row[1] != run.canonical_sha256 or row[2] != run.manifest_sha256:
            raise ImportValidationError(
                "The selected run_id already exists with different content"
            )
        if dict(row[6]) != selected_configuration:
            raise ImportValidationError(
                "Concurrent import configuration differs for the selected run_id"
            )
        if row[0] != "completed":
            return None
        return dict(row[3]), bool(row[4]), tuple(row[5])

    def mark_import_failed(self, run_id: str, error: BaseException) -> None:
        message = sanitize_error(error)[:2000]
        try:
            with self.connect() as connection:
                # Do not overwrite a success after an ambiguous client-side failure.
                connection.execute(
                    """
                    update public.housing_pipeline_runs
                    set import_status = 'failed', import_error = %s,
                        import_completed_at = now(), updated_at = now()
                    where run_id = %s and import_status <> 'completed'
                    """,
                    (message, run_id),
                )
        except Exception:
            warnings.warn(
                "Stage 4 could not record failed import status; reconcile any "
                "stale 'running' row before retrying.",
                RuntimeWarning,
                stacklevel=2,
            )

    def load_state(self, connection, run: ValidatedRun) -> ExistingState:
        properties_by_id: dict[int, StoredProperty] = {}
        properties_by_match_key: dict[str, StoredProperty] = {}
        for row in connection.execute(
            """
            select id, normalized_address, unit_identifier, match_key,
                   latitude, longitude
            from public.housing_properties
            """
        ):
            stored = StoredProperty(*row)
            properties_by_id[stored.id] = stored
            if stored.match_key:
                properties_by_match_key[stored.match_key] = stored

        observation_runs: dict[int, set[str]] = {}
        for listing_id, observed_run_id in connection.execute(
            """
            select observation.listing_id, pipeline_run.run_id
            from public.housing_listing_observations observation
            join public.housing_pipeline_runs pipeline_run
              on pipeline_run.id = observation.pipeline_run_id
            """
        ):
            observation_runs.setdefault(listing_id, set()).add(observed_run_id)

        listings_by_source_id: dict[str, StoredListing] = {}
        for row in connection.execute(
            """
            select listing.id, listing.source_listing_id, listing.property_id,
                   listing.source_url, listing.status, listing.missing_run_count,
                   latest.observation_hash, latest.comparison_data
            from public.housing_listings listing
            left join lateral (
                select observation_hash, comparison_data
                from public.housing_listing_observations observation
                where observation.listing_id = listing.id
                order by observed_at desc, id desc
                limit 1
            ) latest on true
            where listing.source = %s
            """,
            (run.source,),
        ):
            listing_id = row[0]
            stored = StoredListing(
                id=listing_id,
                source_listing_id=row[1],
                property_id=row[2],
                source_url=row[3],
                status=row[4],
                missing_run_count=row[5],
                latest_observation_hash=row[6],
                latest_comparison_data=dict(row[7]) if row[7] is not None else None,
                observation_run_ids=frozenset(observation_runs.get(listing_id, set())),
            )
            listings_by_source_id[stored.source_listing_id] = stored

        review_keys = {
            row[0]
            for row in connection.execute(
                """
                select review.dedupe_key
                from public.housing_review_items review
                join public.housing_pipeline_runs pipeline_run
                  on pipeline_run.id = review.pipeline_run_id
                where pipeline_run.run_id = %s
                """,
                (run.run_id,),
            )
        }
        latest = connection.execute(
            """
            select max(completed_at)
            from public.housing_pipeline_runs
            where source = %s and import_status = 'completed'
              and lifecycle_updates_applied
            """,
            (run.source,),
        ).fetchone()[0]
        latest_imported = connection.execute(
            """
            select max(completed_at)
            from public.housing_pipeline_runs
            where source = %s and import_status = 'completed'
            """,
            (run.source,),
        ).fetchone()[0]
        return ExistingState(
            properties_by_id=properties_by_id,
            properties_by_match_key=properties_by_match_key,
            listings_by_source_id=listings_by_source_id,
            review_keys=review_keys,
            latest_lifecycle_run_at=latest.isoformat() if latest else None,
            latest_imported_run_at=(
                latest_imported.isoformat() if latest_imported else None
            ),
        )

    def _upsert_geocodes(self, connection, plan: ImportPlan) -> dict[str, int]:
        ids: dict[str, int] = {}
        for geocode in plan.geocodes:
            row = connection.execute(
                """
                insert into public.housing_geocode_results (
                    normalized_query, provider, status, latitude, longitude,
                    confidence, match_type, result_type, formatted_address,
                    city, postal_code, country_code, error, raw_provider_data
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (normalized_query, provider) do update set
                    status = excluded.status,
                    latitude = excluded.latitude,
                    longitude = excluded.longitude,
                    confidence = excluded.confidence,
                    match_type = excluded.match_type,
                    result_type = excluded.result_type,
                    formatted_address = excluded.formatted_address,
                    city = excluded.city,
                    postal_code = excluded.postal_code,
                    country_code = excluded.country_code,
                    error = excluded.error,
                    updated_at = now()
                where public.housing_geocode_results.status <> 'ok'
                   or (
                        excluded.status = 'ok'
                        and excluded.latitude is not null
                        and excluded.longitude is not null
                        and coalesce(excluded.confidence, -1)
                            >= coalesce(public.housing_geocode_results.confidence, -1)
                   )
                returning id
                """,
                (
                    geocode.normalized_query,
                    GEOCODE_PROVIDER,
                    geocode.status,
                    geocode.latitude,
                    geocode.longitude,
                    geocode.confidence,
                    geocode.match_type,
                    geocode.result_type,
                    geocode.formatted_address,
                    geocode.city,
                    geocode.postal_code,
                    geocode.country_code,
                    geocode.error,
                    self._Jsonb({}),
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    select id from public.housing_geocode_results
                    where normalized_query = %s and provider = %s
                    """,
                    (geocode.normalized_query, GEOCODE_PROVIDER),
                ).fetchone()
            ids[geocode.normalized_query] = row[0]
        return ids

    def _resolve_properties(
        self, connection, plan: ImportPlan, geocode_ids: dict[str, int]
    ) -> dict[str, int]:
        ids: dict[str, int] = {}
        geocode_query_by_property: dict[str, str] = {}
        listing_property_refs = {
            item.source_listing_id: item.property_ref
            for item in plan.listings
            if item.property_ref
        }
        for observation in plan.observations:
            if observation.listing.geocode:
                property_ref = listing_property_refs.get(observation.source_listing_id)
                if property_ref:
                    geocode_query_by_property.setdefault(
                        property_ref, observation.listing.geocode.normalized_query
                    )

        for decision in plan.properties:
            query = geocode_query_by_property.get(decision.ref)
            geocode_id = geocode_ids.get(query) if query else None
            if decision.existing_id is not None:
                ids[decision.ref] = decision.existing_id
                candidate = decision.candidate
                if decision.enrich_identity:
                    row = connection.execute(
                        """
                        update public.housing_properties
                        set normalized_address = %s, display_address = %s,
                            unit_identifier = %s, city = %s, province = %s,
                            postal_code = %s, country_code = %s,
                            address_complete = %s, match_key = %s,
                            updated_at = now()
                        where id = %s
                          and normalized_address is null
                          and match_key is null
                          and not exists (
                              select 1 from public.housing_properties competing
                              where competing.match_key = %s
                                and competing.id <> %s
                          )
                        returning id
                        """,
                        (
                            candidate.normalized_address,
                            candidate.display_address,
                            candidate.unit_identifier,
                            candidate.city,
                            candidate.province,
                            candidate.postal_code,
                            candidate.country_code,
                            candidate.address_complete,
                            candidate.match_key,
                            decision.existing_id,
                            candidate.match_key,
                            decision.existing_id,
                        ),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError(
                            "Planned incomplete-property enrichment was no longer safe"
                        )
                if query:
                    connection.execute(
                    """
                    update public.housing_properties
                    set latitude = %s, longitude = %s,
                        geocode_confidence = %s, geocode_provider = %s,
                        geocode_status = %s, geocode_result_id = %s,
                        updated_at = now()
                    where id = %s and (
                        geocode_status is distinct from 'ok'
                        or (
                            %s = 'ok'
                            and %s is not null
                            and %s is not null
                            and coalesce(%s, -1) >= coalesce(geocode_confidence, -1)
                        )
                    )
                    """,
                        (
                            candidate.latitude,
                            candidate.longitude,
                            candidate.geocode_confidence,
                            GEOCODE_PROVIDER,
                            candidate.geocode_status,
                            geocode_id,
                            decision.existing_id,
                            candidate.geocode_status,
                            candidate.latitude,
                            candidate.longitude,
                            candidate.geocode_confidence,
                        ),
                    )
                continue
            candidate = decision.candidate
            row = connection.execute(
                """
                insert into public.housing_properties (
                    normalized_address, display_address, unit_identifier, city,
                    province, postal_code, country_code, latitude, longitude,
                    geocode_confidence, geocode_provider, geocode_status,
                    geocode_result_id, address_complete, match_key
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                on conflict do nothing
                returning id
                """,
                (
                    candidate.normalized_address,
                    candidate.display_address,
                    candidate.unit_identifier,
                    candidate.city,
                    candidate.province,
                    candidate.postal_code,
                    candidate.country_code,
                    candidate.latitude,
                    candidate.longitude,
                    candidate.geocode_confidence,
                    GEOCODE_PROVIDER if query else None,
                    candidate.geocode_status,
                    geocode_id,
                    candidate.address_complete,
                    candidate.match_key,
                ),
            ).fetchone()
            if row is None and candidate.match_key:
                row = connection.execute(
                    "select id from public.housing_properties where match_key = %s",
                    (candidate.match_key,),
                ).fetchone()
            if row is None:
                raise RuntimeError("Could not resolve a planned property")
            ids[decision.ref] = row[0]
        return ids

    def _resolve_listings(
        self,
        connection,
        plan: ImportPlan,
        pipeline_run_id: int,
        property_ids: dict[str, int],
    ) -> dict[str, int]:
        ids: dict[str, int] = {}
        for decision in plan.listings:
            property_id = property_ids.get(decision.property_ref) if decision.property_ref else None
            refresh_current_state = decision.refresh_current_state
            if decision.existing_id is None:
                row = connection.execute(
                    """
                    insert into public.housing_listings (
                        source, source_listing_id, source_url, property_id,
                        first_seen_pipeline_run_id, last_seen_pipeline_run_id,
                        status, missing_run_count
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (source, source_listing_id) do nothing
                    returning id
                    """,
                    (
                        plan.run.source,
                        decision.source_listing_id,
                        decision.source_url,
                        property_id,
                        pipeline_run_id,
                        pipeline_run_id,
                        decision.new_status,
                        decision.missing_run_count,
                    ),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        """
                        select id from public.housing_listings
                        where source = %s and source_listing_id = %s
                        """,
                        (plan.run.source, decision.source_listing_id),
                    ).fetchone()
                listing_id = row[0]
            else:
                listing_id = decision.existing_id
                connection.execute(
                    """
                    update public.housing_listings
                    set source_url = case when %s then %s else source_url end,
                        property_id = case
                            when %s then coalesce(%s, property_id)
                            else property_id end,
                        last_seen_pipeline_run_id = case when %s then %s else last_seen_pipeline_run_id end,
                        status = %s,
                        missing_run_count = %s,
                        removed_at = case
                            when %s = 'removed' and status <> 'removed' then %s::timestamptz
                            else removed_at end,
                        relisted_at = case
                            when %s = 'relisted' and status <> 'relisted' then %s::timestamptz
                            else relisted_at end,
                        updated_at = now()
                    where id = %s
                    """,
                    (
                        refresh_current_state,
                        decision.source_url,
                        refresh_current_state,
                        property_id,
                        refresh_current_state,
                        pipeline_run_id,
                        decision.new_status,
                        decision.missing_run_count,
                        decision.new_status,
                        plan.run.observed_at,
                        decision.new_status,
                        plan.run.observed_at,
                        listing_id,
                    ),
                )
            ids[decision.source_listing_id] = listing_id
        return ids

    def _insert_observations(
        self,
        connection,
        plan: ImportPlan,
        pipeline_run_id: int,
        listing_ids: dict[str, int],
        property_ids: dict[str, int],
    ) -> None:
        property_refs = {
            item.source_listing_id: item.property_ref for item in plan.listings
        }
        for observation in plan.observations:
            values = observation.listing.values
            property_id = property_ids.get(property_refs[observation.source_listing_id])
            row = connection.execute(
                """
                insert into public.housing_listing_observations (
                    listing_id, pipeline_run_id, property_id, observed_at,
                    change_type, changed_fields, title, description, address,
                    price_text, price_numeric, price_period, price_monthly,
                    bedrooms, housing_type, utilities_included, utilities_status,
                    lease_type, lease_term_months, is_sublet, furnished,
                    parking_available, parking_spaces, laundry, air_conditioning,
                    dishwasher, bathrooms, bathroom_type, available_now,
                    availability_text, date_available, tenant_type, preferred_gender,
                    amenities, latitude, longitude, map_ready,
                    geocode_status, geocode_confidence, geocode_quality_issue,
                    distance_to_western_km, raw_data, provenance_data,
                    confidence_data, comparison_data, review_flags, observation_hash
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                on conflict (listing_id, pipeline_run_id) do nothing
                returning observation_hash
                """,
                (
                    listing_ids[observation.source_listing_id],
                    pipeline_run_id,
                    property_id,
                    plan.run.observed_at,
                    observation.classification,
                    self._Jsonb(list(observation.changed_fields)),
                    values["title"],
                    values["description"],
                    values["address"],
                    values["price_text"],
                    values["price_numeric"],
                    values["price_period"],
                    values["price_monthly"],
                    values["bedrooms"],
                    values["housing_type"],
                    values["utilities_included"],
                    values["utilities_status"],
                    values["lease_type"],
                    values["lease_term_months"],
                    values["is_sublet"],
                    values["furnished"],
                    values["parking_available"],
                    values["parking_spaces"],
                    values["laundry"],
                    values["air_conditioning"],
                    values["dishwasher"],
                    values["bathrooms"],
                    values["bathroom_type"],
                    values["available_now"],
                    values["availability_text"],
                    values["date_available"],
                    values["tenant_type"],
                    values["preferred_gender"],
                    self._Jsonb(values["amenities"]),
                    values["latitude"],
                    values["longitude"],
                    values["map_ready"],
                    values["geocode_status"],
                    values["geocode_confidence"],
                    values["geocode_quality_issue"],
                    values["distance_to_western_km"],
                    self._Jsonb(observation.listing.raw_data),
                    self._Jsonb(observation.listing.provenance_data),
                    self._Jsonb(observation.listing.confidence_data),
                    self._Jsonb(observation.listing.comparison_data),
                    self._Jsonb(observation.listing.review_flags),
                    observation.listing.observation_hash,
                ),
            ).fetchone()
            if row is None:
                existing_hash = connection.execute(
                    """
                    select observation_hash
                    from public.housing_listing_observations
                    where listing_id = %s and pipeline_run_id = %s
                    """,
                    (listing_ids[observation.source_listing_id], pipeline_run_id),
                ).fetchone()[0]
                if existing_hash != observation.listing.observation_hash:
                    raise ImportValidationError(
                        "Existing immutable observation has a different hash"
                    )

    def _insert_reviews(
        self,
        connection,
        plan: ImportPlan,
        pipeline_run_id: int,
        listing_ids: dict[str, int],
        property_ids: dict[str, int],
    ) -> None:
        for review in plan.reviews:
            connection.execute(
                """
                insert into public.housing_review_items (
                    pipeline_run_id, listing_id, property_id, review_type,
                    severity, field_name, reason, payload, dedupe_key
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (dedupe_key) do nothing
                """,
                (
                    pipeline_run_id,
                    listing_ids.get(review.source_listing_id),
                    property_ids.get(review.property_ref),
                    review.review_type,
                    review.severity,
                    review.field_name,
                    review.reason,
                    self._Jsonb(review.payload),
                    review.dedupe_key,
                ),
            )

    def apply_plan(
        self,
        connection,
        plan: ImportPlan,
        *,
        failure_injector: Optional[Callable[[str], None]] = None,
    ) -> None:
        pipeline_run_id = connection.execute(
            "select id from public.housing_pipeline_runs where run_id = %s for update",
            (plan.run.run_id,),
        ).fetchone()[0]
        geocode_ids = self._upsert_geocodes(connection, plan)
        property_ids = self._resolve_properties(connection, plan, geocode_ids)
        listing_ids = self._resolve_listings(
            connection, plan, pipeline_run_id, property_ids
        )
        self._insert_observations(
            connection, plan, pipeline_run_id, listing_ids, property_ids
        )
        self._insert_reviews(
            connection, plan, pipeline_run_id, listing_ids, property_ids
        )
        if failure_injector is not None:
            # Integration-test seam: the CLI never supplies this callback.
            failure_injector("after_domain_writes")
        connection.execute(
            """
            update public.housing_pipeline_runs
            set import_status = 'completed', import_error = null,
                import_completed_at = now(), change_summary = %s,
                lifecycle_updates_applied = %s,
                lifecycle_ineligibility_reasons = %s,
                updated_at = now()
            where id = %s
            """,
            (
                self._Jsonb(plan.summary),
                plan.lifecycle_applied,
                self._Jsonb(list(plan.lifecycle_ineligibility_reasons)),
                pipeline_run_id,
            ),
        )


def import_validated_run(
    run: ValidatedRun,
    *,
    database_url: str,
    missing_run_threshold: int,
    skip_lifecycle_updates: bool,
    _test_failure_injector: Optional[Callable[[str], None]] = None,
) -> ImportPlan:
    """Import one validated run and return its deterministic change plan."""
    repository = PostgresRepository(database_url)
    selected_configuration = import_configuration(
        missing_run_threshold=missing_run_threshold,
        skip_lifecycle_updates=skip_lifecycle_updates,
        override_used=run.override_used,
        override_reason=run.override_reason,
    )
    completed = repository.completed_summary(run, selected_configuration)
    if completed is not None:
        summary, lifecycle_applied, lifecycle_reasons = completed
        return build_import_plan(
            replace(run),
            ExistingState(
                imported_run_summary=summary,
                imported_run_lifecycle_applied=lifecycle_applied,
                imported_run_lifecycle_reasons=lifecycle_reasons,
            ),
            missing_run_threshold=missing_run_threshold,
            skip_lifecycle_updates=skip_lifecycle_updates,
        )

    try:
        repository.mark_import_started(run, selected_configuration)
        with repository.connect() as connection:
            with connection.transaction():
                connection.execute(
                    "select pg_advisory_xact_lock(hashtext(%s))", (run.source,)
                )
                concurrent_summary = repository.completed_summary_in_transaction(
                    connection, run, selected_configuration
                )
                if concurrent_summary is not None:
                    summary, lifecycle_applied, lifecycle_reasons = concurrent_summary
                    plan = build_import_plan(
                        run,
                        ExistingState(
                            imported_run_summary=summary,
                            imported_run_lifecycle_applied=lifecycle_applied,
                            imported_run_lifecycle_reasons=lifecycle_reasons,
                        ),
                        missing_run_threshold=missing_run_threshold,
                        skip_lifecycle_updates=skip_lifecycle_updates,
                    )
                else:
                    state = repository.load_state(connection, run)
                    plan = build_import_plan(
                        run,
                        state,
                        missing_run_threshold=missing_run_threshold,
                        skip_lifecycle_updates=skip_lifecycle_updates,
                    )
                    repository.apply_plan(
                        connection,
                        plan,
                        failure_injector=_test_failure_injector,
                    )
        return plan
    except Exception as error:
        repository.mark_import_failed(run.run_id, error)
        raise
