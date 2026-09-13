-- Durable, provider-neutral location accessibility intelligence.
-- Profiles belong to properties/origins, never to ephemeral advertisements.

create table public.housing_accessibility_profiles (
    id bigint generated always as identity primary key,
    cache_identity char(64) not null,
    origin_key text not null,
    origin_type text not null check (
        origin_type in ('property', 'entrance', 'coordinate')
    ),
    origin_property_id bigint references public.housing_properties(id),
    origin_zone_id text,
    origin_latitude double precision not null check (origin_latitude between -90 and 90),
    origin_longitude double precision not null check (origin_longitude between -180 and 180),
    entrance_fingerprint text,
    hotspot_id text not null,
    travel_mode text not null check (
        travel_mode in ('walking', 'cycling', 'transit', 'driving')
    ),
    day_type text check (day_type in ('weekday', 'saturday', 'sunday')),
    time_period text check (
        time_period in (
            'weekday_morning_commute', 'weekday_midday',
            'weekday_evening_commute', 'weekday_late_evening',
            'saturday_daytime', 'sunday_daytime'
        )
    ),
    representative_duration_seconds integer check (
        representative_duration_seconds is null or representative_duration_seconds >= 0
    ),
    minimum_duration_seconds integer check (
        minimum_duration_seconds is null or minimum_duration_seconds >= 0
    ),
    maximum_duration_seconds integer check (
        maximum_duration_seconds is null or maximum_duration_seconds >= 0
    ),
    distance_meters integer check (distance_meters is null or distance_meters >= 0),
    walking_duration_seconds integer check (
        walking_duration_seconds is null or walking_duration_seconds >= 0
    ),
    transfer_count integer check (transfer_count is null or transfer_count >= 0),
    nearest_stop_id text,
    stop_to_destination_seconds integer check (
        stop_to_destination_seconds is null or stop_to_destination_seconds >= 0
    ),
    provider text not null,
    provider_profile text not null,
    result_type text not null check (
        result_type in (
            'exact_route', 'cached_exact_property', 'cached_exact_origin',
            'same_stop_reuse', 'nearby_origin_estimate',
            'straight_line_fallback', 'pending_provider', 'unavailable', 'stale'
        )
    ),
    confidence double precision check (confidence between 0 and 1),
    sample_count integer not null default 0 check (sample_count >= 0),
    calculated_at timestamptz not null,
    schedule_version text,
    network_version text,
    expires_at timestamptz,
    source_profile_id bigint references public.housing_accessibility_profiles(id),
    estimation_distance_meters integer check (
        estimation_distance_meters is null or estimation_distance_meters >= 0
    ),
    estimation_method text,
    provider_metadata jsonb not null default '{}'::jsonb,
    is_stale boolean not null default false,
    stale_at timestamptz,
    stale_reason text,
    origin_walking_to_stop_seconds integer check (
        origin_walking_to_stop_seconds is null or origin_walking_to_stop_seconds >= 0
    ),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (btrim(origin_key) <> ''),
    check (btrim(hotspot_id) <> ''),
    check (btrim(provider) <> ''),
    check (btrim(provider_profile) <> ''),
    check (cache_identity ~ '^[0-9a-f]{64}$'),
    check (jsonb_typeof(provider_metadata) = 'object'),
    check (origin_type <> 'property' or origin_property_id is not null),
    check (travel_mode <> 'transit' or (day_type is not null and time_period is not null)),
    check (
        minimum_duration_seconds is null
        or maximum_duration_seconds is null
        or minimum_duration_seconds <= maximum_duration_seconds
    ),
    check (
        representative_duration_seconds is null
        or minimum_duration_seconds is null
        or representative_duration_seconds >= minimum_duration_seconds
    ),
    check (
        representative_duration_seconds is null
        or maximum_duration_seconds is null
        or representative_duration_seconds <= maximum_duration_seconds
    )
);

-- Only one current aggregate may own a complete provider/cache identity.
-- Stale rows remain available for audit while a replacement becomes current.
create unique index housing_accessibility_current_cache_uidx
    on public.housing_accessibility_profiles (cache_identity)
    where not is_stale;

create table public.housing_accessibility_samples (
    id bigint generated always as identity primary key,
    profile_id bigint not null references public.housing_accessibility_profiles(id),
    departure_at timestamptz not null,
    duration_seconds integer check (duration_seconds is null or duration_seconds >= 0),
    walking_duration_seconds integer check (
        walking_duration_seconds is null or walking_duration_seconds >= 0
    ),
    transfer_count integer check (transfer_count is null or transfer_count >= 0),
    distance_meters integer check (distance_meters is null or distance_meters >= 0),
    stop_to_destination_seconds integer check (
        stop_to_destination_seconds is null or stop_to_destination_seconds >= 0
    ),
    status text not null check (
        status in (
            'available', 'estimated', 'unavailable', 'pending_provider',
            'invalid_origin', 'invalid_destination'
        )
    ),
    provider_sample_id text,
    provider_metadata jsonb not null default '{}'::jsonb,
    observed_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    unique (profile_id, departure_at),
    check (jsonb_typeof(provider_metadata) = 'object')
);

create table public.housing_accessibility_reuse_history (
    id bigint generated always as identity primary key,
    requested_source_listing_id text,
    requested_property_id bigint references public.housing_properties(id),
    hotspot_id text not null,
    travel_mode text not null check (
        travel_mode in ('walking', 'cycling', 'transit', 'driving')
    ),
    time_period text,
    result_type text not null check (
        result_type in (
            'exact_route', 'cached_exact_property', 'cached_exact_origin',
            'same_stop_reuse', 'nearby_origin_estimate',
            'straight_line_fallback', 'pending_provider', 'unavailable', 'stale'
        )
    ),
    source_profile_id bigint references public.housing_accessibility_profiles(id),
    resolved_profile_id bigint references public.housing_accessibility_profiles(id),
    estimation_distance_meters integer check (
        estimation_distance_meters is null or estimation_distance_meters >= 0
    ),
    connector_duration_seconds integer check (
        connector_duration_seconds is null or connector_duration_seconds >= 0
    ),
    confidence double precision check (confidence between 0 and 1),
    reuse_reason text not null,
    request_metadata jsonb not null default '{}'::jsonb,
    requested_at timestamptz not null default now(),
    check (jsonb_typeof(request_metadata) = 'object')
);

alter table public.housing_accessibility_profiles enable row level security;
alter table public.housing_accessibility_samples enable row level security;
alter table public.housing_accessibility_reuse_history enable row level security;

create index housing_accessibility_property_lookup_idx
    on public.housing_accessibility_profiles (
        origin_property_id, hotspot_id, travel_mode, time_period, calculated_at desc
    ) where origin_property_id is not null;
create index housing_accessibility_origin_lookup_idx
    on public.housing_accessibility_profiles (
        origin_latitude, origin_longitude, hotspot_id, travel_mode
    );
create index housing_accessibility_zone_lookup_idx
    on public.housing_accessibility_profiles (origin_zone_id, hotspot_id, travel_mode)
    where origin_zone_id is not null;
create index housing_accessibility_hotspot_mode_idx
    on public.housing_accessibility_profiles (hotspot_id, travel_mode, time_period);
create index housing_accessibility_expiry_idx
    on public.housing_accessibility_profiles (expires_at, id)
    where not is_stale;
create index housing_accessibility_provider_version_idx
    on public.housing_accessibility_profiles (
        provider, provider_profile, schedule_version, network_version
    );
create index housing_accessibility_stop_reuse_idx
    on public.housing_accessibility_profiles (
        nearest_stop_id, hotspot_id, time_period, schedule_version
    ) where travel_mode = 'transit' and nearest_stop_id is not null;
create index housing_accessibility_samples_profile_idx
    on public.housing_accessibility_samples (profile_id, departure_at);
create index housing_accessibility_reuse_property_idx
    on public.housing_accessibility_reuse_history (requested_property_id, requested_at desc);
create index housing_accessibility_reuse_source_idx
    on public.housing_accessibility_reuse_history (source_profile_id, requested_at desc);

-- Add durable property identity to the existing product view without changing
-- the order or meaning of its established columns.
create or replace view public.active_housing_listings
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
    end as has_direct_western_route,
    l.property_id,
    p.match_key as property_match_key
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
left join public.housing_properties p
    on p.id = l.property_id
where l.status in ('active', 'possibly_removed', 'relisted')
  and l.source = 'uwo_offcampus';

comment on table public.housing_accessibility_profiles is
    'Durable property/origin accessibility intelligence retained independently of listing status.';
comment on view public.active_housing_listings is
    'Latest non-removed UWO listings with durable property identity for accessibility lookup.';
