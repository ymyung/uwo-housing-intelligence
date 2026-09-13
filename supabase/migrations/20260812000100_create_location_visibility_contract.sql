-- Reviewed property-level rules for public map and routing visibility.

create table public.housing_property_location_visibility (
    id bigint generated always as identity primary key,
    property_id bigint not null references public.housing_properties(id),
    policy_version text not null,
    location_status text not null check (
        location_status in ('available', 'limited', 'unavailable')
    ),
    map_visible boolean not null,
    route_available boolean not null,
    reason_codes jsonb not null default '[]'::jsonb,
    source_run_id text not null,
    source_fingerprint char(64) not null,
    is_current boolean not null default true,
    assessed_at timestamptz not null default now(),
    superseded_at timestamptz,
    created_at timestamptz not null default now(),
    check (btrim(policy_version) <> ''),
    check (btrim(source_run_id) <> ''),
    check (source_fingerprint ~ '^[0-9a-f]{64}$'),
    check (jsonb_typeof(reason_codes) = 'array'),
    check (
        (location_status = 'available' and map_visible and route_available)
        or (location_status = 'limited' and map_visible and not route_available)
        or (location_status = 'unavailable' and not map_visible and not route_available)
    ),
    check ((is_current and superseded_at is null) or not is_current)
);

alter table public.housing_property_location_visibility enable row level security;

create unique index housing_property_location_visibility_current_uidx
    on public.housing_property_location_visibility (property_id)
    where is_current;

create index housing_property_location_visibility_status_idx
    on public.housing_property_location_visibility (
        location_status, map_visible, route_available, property_id
    ) where is_current;

create index housing_property_location_visibility_history_idx
    on public.housing_property_location_visibility (
        property_id, assessed_at desc, id desc
    );

comment on table public.housing_property_location_visibility is
    'Versioned reviewed decisions controlling public map and route use at property grain.';

create view public.product_housing_listings
with (security_invoker = true) as
select
    ranked.*,
    coalesce(visibility.location_status, 'unavailable') as location_status,
    coalesce(visibility.map_visible, false) as location_map_visible,
    coalesce(visibility.route_available, false) as location_route_available,
    coalesce(visibility.reason_codes, '["not_assessed"]'::jsonb) as location_reason_codes
from public.ranked_housing_listings ranked
left join public.housing_property_location_visibility visibility
    on visibility.property_id = ranked.property_id
   and visibility.is_current;

comment on view public.product_housing_listings is
    'Fail-closed listing API projection with a reviewed public location-visibility decision.';
