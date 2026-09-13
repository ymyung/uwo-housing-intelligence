-- Stage 4 normalized persistence for UWO off-campus housing.
-- The legacy public.listings table is intentionally left untouched.

create table public.housing_pipeline_runs (
    id bigint generated always as identity primary key,
    run_id text not null unique,
    source text not null,
    status text not null check (
        status in ('running', 'completed', 'completed_with_warnings', 'failed')
    ),
    canonical_for_import boolean not null default false,
    source_git_commit text,
    git_dirty boolean,
    started_at timestamptz not null,
    completed_at timestamptz,
    manifest_json jsonb not null,
    import_started_at timestamptz,
    import_completed_at timestamptz,
    import_status text not null default 'pending' check (
        import_status in ('pending', 'running', 'completed', 'failed')
    ),
    import_error text,
    import_override_used boolean not null default false,
    import_configuration jsonb not null default '{}'::jsonb,
    canonical_sha256 char(64) not null,
    manifest_sha256 char(64) not null,
    lifecycle_updates_eligible boolean not null default false,
    lifecycle_updates_applied boolean not null default false,
    lifecycle_ineligibility_reasons jsonb not null default '[]'::jsonb,
    change_summary jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (jsonb_typeof(manifest_json) = 'object'),
    check (jsonb_typeof(change_summary) = 'object'),
    check (jsonb_typeof(import_configuration) = 'object'),
    check (jsonb_typeof(lifecycle_ineligibility_reasons) = 'array'),
    check (canonical_sha256 ~ '^[0-9a-f]{64}$'),
    check (manifest_sha256 ~ '^[0-9a-f]{64}$')
);

create table public.housing_geocode_results (
    id bigint generated always as identity primary key,
    normalized_query text not null,
    provider text not null,
    provider_result_id text,
    status text not null,
    latitude double precision check (latitude between -90 and 90),
    longitude double precision check (longitude between -180 and 180),
    confidence double precision check (confidence between 0 and 1),
    match_type text,
    result_type text,
    formatted_address text,
    city text,
    postal_code text,
    country_code text,
    error text,
    raw_provider_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (normalized_query, provider),
    check ((latitude is null) = (longitude is null)),
    check (status <> 'ok' or latitude is not null),
    check (jsonb_typeof(raw_provider_data) = 'object')
);

create unique index housing_geocode_provider_result_uidx
    on public.housing_geocode_results (provider, provider_result_id)
    where provider_result_id is not null;

create table public.housing_properties (
    id bigint generated always as identity primary key,
    normalized_address text,
    display_address text,
    unit_identifier text,
    city text,
    province text,
    postal_code text,
    country_code text,
    latitude double precision check (latitude between -90 and 90),
    longitude double precision check (longitude between -180 and 180),
    geocode_confidence double precision check (geocode_confidence between 0 and 1),
    geocode_provider text,
    geocode_status text,
    geocode_result_id bigint references public.housing_geocode_results(id),
    address_complete boolean not null default false,
    -- match_key is only populated for complete, explainable exact matches.
    -- Incomplete addresses remain unkeyed and are never globally deduplicated.
    match_key text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (match_key is null or address_complete),
    check ((latitude is null) = (longitude is null)),
    check (geocode_status <> 'ok' or latitude is not null)
);

create unique index housing_properties_match_key_uidx
    on public.housing_properties (match_key)
    where match_key is not null;

create table public.housing_listings (
    id bigint generated always as identity primary key,
    source text not null,
    source_listing_id text not null,
    source_url text not null,
    property_id bigint references public.housing_properties(id),
    first_seen_pipeline_run_id bigint not null
        references public.housing_pipeline_runs(id),
    last_seen_pipeline_run_id bigint not null
        references public.housing_pipeline_runs(id),
    status text not null default 'active' check (
        status in ('active', 'possibly_removed', 'removed', 'relisted')
    ),
    missing_run_count integer not null default 0 check (missing_run_count >= 0),
    removed_at timestamptz,
    relisted_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (source, source_listing_id),
    check (btrim(source) <> ''),
    check (btrim(source_listing_id) <> ''),
    check (btrim(source_url) <> '')
);

create table public.housing_listing_observations (
    id bigint generated always as identity primary key,
    listing_id bigint not null references public.housing_listings(id),
    pipeline_run_id bigint not null references public.housing_pipeline_runs(id),
    property_id bigint references public.housing_properties(id),
    observed_at timestamptz not null,
    change_type text not null check (
        change_type in ('new', 'updated', 'unchanged', 'relisted')
    ),
    changed_fields jsonb not null default '[]'::jsonb,
    title text,
    description text,
    address text,
    price_text text,
    price_numeric numeric(12, 2) check (price_numeric is null or price_numeric >= 0),
    price_period text,
    price_monthly numeric(12, 2) check (price_monthly is null or price_monthly >= 0),
    bedrooms integer check (bedrooms is null or bedrooms >= 0),
    housing_type text,
    utilities_included boolean,
    utilities_status text,
    lease_type text,
    lease_term_months integer check (
        lease_term_months is null or lease_term_months >= 0
    ),
    is_sublet boolean,
    furnished boolean,
    parking_available boolean,
    parking_spaces integer check (parking_spaces is null or parking_spaces >= 0),
    laundry boolean,
    air_conditioning boolean,
    dishwasher boolean,
    bathrooms numeric(6, 2) check (bathrooms is null or bathrooms >= 0),
    bathroom_type text,
    available_now boolean,
    availability_text text,
    date_available date,
    tenant_type text,
    preferred_gender text,
    amenities jsonb not null default '[]'::jsonb check (
        jsonb_typeof(amenities) = 'array'
    ),
    latitude double precision check (latitude between -90 and 90),
    longitude double precision check (longitude between -180 and 180),
    map_ready boolean not null default false,
    geocode_status text,
    geocode_confidence double precision check (geocode_confidence between 0 and 1),
    geocode_quality_issue text,
    distance_to_western_km double precision check (
        distance_to_western_km is null or distance_to_western_km >= 0
    ),
    raw_data jsonb not null,
    provenance_data jsonb not null default '{}'::jsonb,
    confidence_data jsonb not null default '{}'::jsonb,
    comparison_data jsonb not null,
    review_flags jsonb not null default '[]'::jsonb check (
        jsonb_typeof(review_flags) = 'array'
    ),
    observation_hash char(64) not null,
    created_at timestamptz not null default now(),
    unique (listing_id, pipeline_run_id),
    check ((latitude is null) = (longitude is null)),
    check (
        not map_ready
        or (
            latitude is not null
            and longitude is not null
            and geocode_status is not distinct from 'ok'
        )
    ),
    check (observation_hash ~ '^[0-9a-f]{64}$'),
    check (jsonb_typeof(raw_data) = 'object'),
    check (jsonb_typeof(provenance_data) = 'object'),
    check (jsonb_typeof(confidence_data) = 'object'),
    check (jsonb_typeof(comparison_data) = 'object'),
    check (jsonb_typeof(changed_fields) = 'array')
);

create table public.housing_review_items (
    id bigint generated always as identity primary key,
    pipeline_run_id bigint not null references public.housing_pipeline_runs(id),
    listing_id bigint references public.housing_listings(id),
    property_id bigint references public.housing_properties(id),
    review_type text not null check (
        review_type in (
            'ai', 'geocode', 'parsing', 'identity', 'property_match',
            'price', 'availability', 'import_validation'
        )
    ),
    severity text not null check (
        severity in ('info', 'warning', 'error', 'critical')
    ),
    field_name text,
    reason text not null,
    payload jsonb not null default '{}'::jsonb,
    status text not null default 'open' check (
        status in ('open', 'in_review', 'resolved', 'dismissed')
    ),
    -- Deterministic importer fingerprint prevents duplicates on reimport.
    dedupe_key char(64) not null unique,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    resolved_at timestamptz,
    resolution_note text,
    resolved_by text,
    check (jsonb_typeof(payload) = 'object')
);

-- Supabase exposes public-schema relations through PostgREST. Base tables are
-- service-role only until deployment-specific policies are reviewed.
alter table public.housing_pipeline_runs enable row level security;
alter table public.housing_geocode_results enable row level security;
alter table public.housing_properties enable row level security;
alter table public.housing_listings enable row level security;
alter table public.housing_listing_observations enable row level security;
alter table public.housing_review_items enable row level security;

create index housing_pipeline_runs_lifecycle_latest_idx
    on public.housing_pipeline_runs (source, completed_at desc)
    where import_status = 'completed' and lifecycle_updates_applied;
create index housing_listings_status_idx
    on public.housing_listings (status);
create index housing_listings_active_idx
    on public.housing_listings (source, last_seen_pipeline_run_id desc)
    where status in ('active', 'possibly_removed', 'relisted');
create index housing_listings_property_idx
    on public.housing_listings (property_id);
create index housing_observations_run_idx
    on public.housing_listing_observations (pipeline_run_id);
create index housing_observations_listing_latest_idx
    on public.housing_listing_observations (listing_id, observed_at desc, id desc);
create index housing_observations_monthly_price_idx
    on public.housing_listing_observations (price_monthly)
    where price_monthly is not null;
create index housing_observations_bedrooms_idx
    on public.housing_listing_observations (bedrooms)
    where bedrooms is not null;
create index housing_observations_housing_type_idx
    on public.housing_listing_observations (housing_type);
create index housing_observations_map_ready_idx
    on public.housing_listing_observations (pipeline_run_id, listing_id)
    where map_ready;
create index housing_properties_normalized_address_idx
    on public.housing_properties (normalized_address);
create index housing_review_items_open_idx
    on public.housing_review_items (created_at, pipeline_run_id)
    where status in ('open', 'in_review');
create index housing_review_items_type_severity_idx
    on public.housing_review_items (review_type, severity, status);

-- Compatibility shape for the current FastAPI/frontend contract. Relisted and
-- possibly-removed advertisements remain visible; removed advertisements do not.
create view public.active_housing_listings
with (security_invoker = true) as
select
    l.source_listing_id as listing_id,
    l.source_listing_id,
    l.source_url as listing_url,
    o.title,
    o.description,
    o.address,
    o.price_numeric,
    o.price_monthly,
    o.price_text,
    o.price_period,
    o.bedrooms,
    o.housing_type,
    o.utilities_included,
    o.utilities_status,
    o.lease_type,
    o.lease_term_months,
    o.is_sublet,
    o.furnished,
    o.parking_available,
    o.parking_spaces,
    o.laundry,
    o.air_conditioning,
    o.dishwasher,
    o.bathrooms,
    o.bathroom_type,
    o.availability_text,
    o.tenant_type,
    o.preferred_gender,
    o.amenities,
    o.latitude,
    o.longitude,
    o.map_ready,
    o.distance_to_western_km,
    l.status as listing_status,
    first_run.started_at as first_seen_at,
    last_run.started_at as last_seen_at,
    o.raw_data ->> 'image_url' as image_url,
    o.raw_data ->> 'image_urls' as image_urls,
    o.raw_data ->> 'available_from' as available_from,
    o.raw_data ->> 'available_to' as available_to,
    o.raw_data ->> 'availability_category' as availability_category,
    o.geocode_status,
    o.geocode_confidence,
    o.geocode_quality_issue,
    case lower(coalesce(o.raw_data ->> 'scraped_ok', ''))
        when 'true' then true when '1' then true
        when 'false' then false when '0' then false else null
    end as scraped_ok,
    o.raw_data ->> 'otp_status' as otp_status,
    o.raw_data ->> 'otp_error' as otp_error,
    o.raw_data ->> 'otp_route_summary' as otp_route_summary,
    o.raw_data ->> 'nearest_stop_id' as nearest_stop_id,
    o.raw_data ->> 'nearest_stop_name' as nearest_stop_name,
    o.raw_data ->> 'nearby_route_ids' as nearby_route_ids,
    o.raw_data ->> 'nearby_route_names' as nearby_route_names,
    o.raw_data ->> 'western_route_ids' as western_route_ids,
    o.raw_data ->> 'western_route_names' as western_route_names,
    null::double precision as walk_time_to_western_min,
    null::double precision as transit_time_to_western_min,
    null::double precision as transit_walk_time_min,
    null::double precision as transit_bus_time_min,
    null::integer as transit_transfers,
    null::double precision as nearest_stop_distance_m,
    null::double precision as walking_minutes_to_nearest_stop,
    null::integer as nearby_stop_count,
    null::integer as transit_score,
    case lower(coalesce(o.raw_data ->> 'otp_used_transit', ''))
        when 'true' then true when '1' then true
        when 'false' then false when '0' then false else null
    end as otp_used_transit,
    case lower(coalesce(o.raw_data ->> 'has_direct_western_route', ''))
        when 'true' then true when '1' then true
        when 'false' then false when '0' then false else null
    end as has_direct_western_route
from public.housing_listings l
join lateral (
    select observation.*
    from public.housing_listing_observations observation
    where observation.listing_id = l.id
    order by observation.observed_at desc, observation.id desc
    limit 1
) o on true
join public.housing_pipeline_runs first_run
    on first_run.id = l.first_seen_pipeline_run_id
join public.housing_pipeline_runs last_run
    on last_run.id = l.last_seen_pipeline_run_id
where l.status in ('active', 'possibly_removed', 'relisted')
  and l.source = 'uwo_offcampus';

comment on view public.active_housing_listings is
    'Latest observation for non-removed UWO housing listings; compatibility shape for the existing API.';
