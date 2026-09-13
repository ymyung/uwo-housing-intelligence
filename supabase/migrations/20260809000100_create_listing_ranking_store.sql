-- Versioned, explainable listing-level ranking results.

create table public.housing_ranking_runs (
    id bigint generated always as identity primary key,
    run_id text not null unique,
    ranking_version text not null,
    status text not null check (status in ('running', 'completed', 'failed')),
    config_fingerprint char(64) not null,
    input_fingerprint char(64) not null,
    output_fingerprint char(64),
    listing_count integer not null check (listing_count >= 0),
    ranked_count integer not null default 0 check (ranked_count >= 0),
    partial_count integer not null default 0 check (partial_count >= 0),
    excluded_count integer not null default 0 check (excluded_count >= 0),
    score_rows_inserted integer not null default 0 check (score_rows_inserted >= 0),
    score_rows_superseded integer not null default 0 check (score_rows_superseded >= 0),
    summary jsonb not null default '{}'::jsonb,
    started_at timestamptz not null,
    completed_at timestamptz,
    error_message text,
    created_at timestamptz not null default now(),
    check (btrim(run_id) <> ''),
    check (btrim(ranking_version) <> ''),
    check (config_fingerprint ~ '^[0-9a-f]{64}$'),
    check (input_fingerprint ~ '^[0-9a-f]{64}$'),
    check (output_fingerprint is null or output_fingerprint ~ '^[0-9a-f]{64}$'),
    check (jsonb_typeof(summary) = 'object')
);

create table public.housing_listing_scores (
    id bigint generated always as identity primary key,
    listing_id bigint not null references public.housing_listings(id),
    ranking_run_id bigint not null references public.housing_ranking_runs(id),
    ranking_version text not null,
    ranking_status text not null check (
        ranking_status in ('ranked', 'partial', 'excluded')
    ),
    overall_score numeric(5, 2),
    value_score numeric(5, 2),
    campus_access_score numeric(5, 2),
    transit_score numeric(5, 2),
    amenity_score numeric(5, 2),
    data_quality_score numeric(5, 2),
    explanation jsonb not null,
    input_fingerprint char(64) not null,
    is_current boolean not null default true,
    computed_at timestamptz not null,
    superseded_at timestamptz,
    created_at timestamptz not null default now(),
    check (btrim(ranking_version) <> ''),
    check (input_fingerprint ~ '^[0-9a-f]{64}$'),
    check (jsonb_typeof(explanation) = 'object'),
    check (overall_score is null or overall_score between 0 and 100),
    check (value_score is null or value_score between 0 and 100),
    check (campus_access_score is null or campus_access_score between 0 and 100),
    check (transit_score is null or transit_score between 0 and 100),
    check (amenity_score is null or amenity_score between 0 and 100),
    check (data_quality_score is null or data_quality_score between 0 and 100),
    check (
        (ranking_status = 'ranked'
            and overall_score is not null
            and value_score is not null
            and campus_access_score is not null
            and transit_score is not null)
        or (ranking_status <> 'ranked' and overall_score is null)
    ),
    check ((is_current and superseded_at is null) or not is_current)
);

alter table public.housing_ranking_runs enable row level security;
alter table public.housing_listing_scores enable row level security;

create unique index housing_listing_scores_current_uidx
    on public.housing_listing_scores (listing_id, ranking_version)
    where is_current;

create index housing_listing_scores_ranked_sort_idx
    on public.housing_listing_scores (
        ranking_version, overall_score desc, listing_id
    ) where is_current and ranking_status = 'ranked';

create index housing_listing_scores_status_idx
    on public.housing_listing_scores (
        ranking_version, ranking_status, listing_id
    ) where is_current;

create index housing_listing_scores_history_idx
    on public.housing_listing_scores (
        listing_id, ranking_version, computed_at desc
    );

create index housing_ranking_runs_version_idx
    on public.housing_ranking_runs (ranking_version, completed_at desc);

comment on table public.housing_ranking_runs is
    'Reproducible listing-ranking executions and aggregate metrics.';
comment on table public.housing_listing_scores is
    'Versioned listing-level scores with structured explanations and retained history.';

-- Read-only API projection. Keep the established compatibility view unchanged
-- and join exactly one current Ranking v1 row at listing grain.
create view public.ranked_housing_listings
with (security_invoker = true) as
select
    active_listing.*,
    observation.provenance_data,
    observation.review_flags,
    jsonb_array_length(observation.review_flags) > 0 as needs_manual_review,
    observation.raw_data ->> 'pet_policy' as pet_policy,
    case
        when jsonb_array_length(observation.review_flags) > 0 then 'needs-review'
        when (
            28
            + case when active_listing.price_monthly is not null then 14 else 0 end
            + case when active_listing.housing_type is not null then 14 else 0 end
            + case when active_listing.bedrooms is not null then 14 else 0 end
            + case when active_listing.map_ready then 20 else 0 end
            + least(10, greatest(0, coalesce(active_listing.geocode_confidence, 0) * 10))
        ) >= 85 then 'confirmed'
        else 'parsed'
    end as data_quality_status,
    score.ranking_version,
    score.ranking_status,
    score.overall_score as ranking_overall_score,
    score.value_score as ranking_value_score,
    score.campus_access_score as ranking_campus_access_score,
    score.transit_score as ranking_transit_score,
    score.amenity_score as ranking_amenity_score,
    score.data_quality_score as ranking_data_quality_score,
    score.explanation as ranking_explanation,
    score.input_fingerprint as ranking_input_fingerprint,
    score.computed_at as ranking_computed_at
from public.active_housing_listings active_listing
join public.housing_listings listing
    on listing.source = 'uwo_offcampus'
   and listing.source_listing_id = active_listing.listing_id
join lateral (
    select candidate.provenance_data, candidate.review_flags, candidate.raw_data
    from public.housing_listing_observations candidate
    where candidate.listing_id = listing.id
    order by candidate.observed_at desc, candidate.id desc
    limit 1
) observation on true
left join public.housing_listing_scores score
    on score.listing_id = listing.id
   and score.ranking_version = 'ranking-v1'
   and score.is_current;

comment on view public.ranked_housing_listings is
    'Active listing API projection with exactly the current persisted Ranking v1 row.';
