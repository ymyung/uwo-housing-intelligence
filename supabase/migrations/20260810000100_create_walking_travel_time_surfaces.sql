-- Compact, versioned, walk-only R5 numerical surfaces.  One payload is stored
-- per canonical property/cache identity; grid cells are never expanded into rows.
create table public.housing_walk_time_surfaces (
    id uuid primary key,
    property_id bigint not null references public.housing_properties(id),
    origin_latitude double precision not null,
    origin_longitude double precision not null,
    cache_identity char(64) not null unique,
    routing_fingerprint char(64) not null,
    grid_fingerprint char(64) not null,
    network_fingerprint char(64) not null,
    r5py_version text not null,
    r5_version text not null,
    origin_snap_distance_metres double precision,
    surface_algorithm_version text not null default 'r5-walk-surface-v1',
    status text not null check (status in ('ready', 'computing', 'failed', 'unavailable')),
    started_at timestamptz not null default now(),
    computed_at timestamptz,
    duration_ms integer check (duration_ms is null or duration_ms >= 0),
    reachable_count integer check (reachable_count is null or reachable_count between 0 and 14803),
    unavailable_count integer check (unavailable_count is null or unavailable_count between 0 and 14803),
    raw_length integer check (raw_length is null or raw_length = 29606),
    compressed_length integer check (compressed_length is null or compressed_length >= 0),
    value_encoding text not null default 'uint16le-gzip-v1',
    compressed_payload bytea,
    destination_validity_mask bytea,
    validity_mask_version text not null default 'destination-snap-bitset-v1',
    etag char(64),
    error_code text,
    error_detail text,
    created_at timestamptz not null default now(),
    check ((status = 'ready') = (compressed_payload is not null)),
    check (status <> 'ready' or octet_length(destination_validity_mask) = 1851),
    check (status <> 'ready' or reachable_count + unavailable_count = 14803),
    check (origin_snap_distance_metres is null or origin_snap_distance_metres >= 0),
    check (origin_latitude between -90 and 90 and origin_longitude between -180 and 180)
);

alter table public.housing_walk_time_surfaces enable row level security;
create index housing_walk_surface_property_current_idx
    on public.housing_walk_time_surfaces (property_id, computed_at desc);
create index housing_walk_surface_status_idx
    on public.housing_walk_time_surfaces (status, started_at);
create index housing_walk_surface_routing_idx
    on public.housing_walk_time_surfaces (routing_fingerprint, grid_fingerprint);

comment on table public.housing_walk_time_surfaces is
    'Walk-only compact R5 numerical surface cache. OTP remains authoritative for exact routes.';
