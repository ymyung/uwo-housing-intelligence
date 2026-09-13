-- City of London municipal reference data.  All feature records are immutable
-- by dataset run; promotion changes only the current pointer.

create extension if not exists postgis;

create schema if not exists reference_data;

create table reference_data.dataset_runs (
    id bigint generated always as identity primary key,
    dataset_name text not null check (
        dataset_name in ('municipal_addresses', 'building_footprints', 'parcels')
    ),
    source_url text not null,
    source_dataset_id text not null,
    downloaded_at timestamptz not null,
    source_updated_at timestamptz,
    source_schema jsonb not null,
    source_schema_sha256 char(64) not null,
    content_sha256 char(64) not null,
    feature_count integer not null check (feature_count >= 0),
    invalid_feature_count integer not null default 0 check (invalid_feature_count >= 0),
    crs_srid integer not null,
    importer_version text not null,
    validation_status text not null check (
        validation_status in ('pending', 'passed', 'failed')
    ),
    import_status text not null check (
        import_status in ('staging', 'promoted', 'failed', 'reused')
    ),
    is_current boolean not null default false,
    validation_summary jsonb not null default '{}'::jsonb,
    failure_reason text,
    created_at timestamptz not null default now(),
    promoted_at timestamptz,
    unique (dataset_name, content_sha256),
    check (jsonb_typeof(source_schema) = 'object'),
    check (jsonb_typeof(validation_summary) = 'object'),
    check (source_schema_sha256 ~ '^[0-9a-f]{64}$'),
    check (content_sha256 ~ '^[0-9a-f]{64}$')
);

create unique index reference_dataset_runs_current_uidx
    on reference_data.dataset_runs (dataset_name)
    where is_current;

create table reference_data.municipal_addresses (
    id bigint generated always as identity primary key,
    dataset_run_id bigint not null references reference_data.dataset_runs(id),
    source_object_id text not null,
    gis_id text,
    municipal_number text,
    municipal_number_qualifier text,
    street_name text,
    street_type text,
    street_direction text,
    unit_number text,
    full_address text,
    status text,
    source_last_edit_at timestamptz,
    normalized_address text,
    normalized_civic_address text,
    normalized_unit text,
    source_attributes jsonb not null,
    geometry geometry(Point, 26917) not null,
    created_at timestamptz not null default now(),
    unique (dataset_run_id, source_object_id),
    check (jsonb_typeof(source_attributes) = 'object')
);

create index municipal_addresses_geometry_gix
    on reference_data.municipal_addresses using gist (geometry);
create index municipal_addresses_normalized_address_idx
    on reference_data.municipal_addresses (normalized_address);
create index municipal_addresses_normalized_civic_idx
    on reference_data.municipal_addresses (normalized_civic_address, normalized_unit);
create index municipal_addresses_source_identifier_idx
    on reference_data.municipal_addresses (gis_id, source_object_id);

create table reference_data.building_footprints (
    id bigint generated always as identity primary key,
    dataset_run_id bigint not null references reference_data.dataset_runs(id),
    source_object_id text not null,
    source_last_edit_at timestamptz,
    source_attributes jsonb not null,
    geometry geometry(Geometry, 26917) not null,
    created_at timestamptz not null default now(),
    unique (dataset_run_id, source_object_id),
    check (jsonb_typeof(source_attributes) = 'object'),
    check (st_geometrytype(geometry) in ('ST_Polygon', 'ST_MultiPolygon'))
);

create index building_footprints_geometry_gix
    on reference_data.building_footprints using gist (geometry);
create index building_footprints_source_identifier_idx
    on reference_data.building_footprints (source_object_id);

create table reference_data.parcels (
    id bigint generated always as identity primary key,
    dataset_run_id bigint not null references reference_data.dataset_runs(id),
    source_object_id text not null,
    gis_id text,
    source_last_edit_at timestamptz,
    source_attributes jsonb not null,
    geometry geometry(Geometry, 26917) not null,
    created_at timestamptz not null default now(),
    unique (dataset_run_id, source_object_id),
    check (jsonb_typeof(source_attributes) = 'object'),
    check (st_geometrytype(geometry) in ('ST_Polygon', 'ST_MultiPolygon'))
);

create index parcels_geometry_gix
    on reference_data.parcels using gist (geometry);
create index parcels_source_identifier_idx
    on reference_data.parcels (gis_id, source_object_id);

create table reference_data.property_resolution_runs (
    id uuid primary key,
    address_dataset_run_id bigint not null references reference_data.dataset_runs(id),
    building_dataset_run_id bigint references reference_data.dataset_runs(id),
    parcel_dataset_run_id bigint references reference_data.dataset_runs(id),
    property_count integer not null check (property_count >= 0),
    created_at timestamptz not null default now(),
    completed_at timestamptz,
    status text not null check (status in ('running', 'completed', 'failed')),
    summary jsonb not null default '{}'::jsonb,
    check (jsonb_typeof(summary) = 'object')
);

create table reference_data.property_reference_matches (
    id bigint generated always as identity primary key,
    resolution_run_id uuid not null references reference_data.property_resolution_runs(id),
    property_id bigint not null references public.housing_properties(id),
    municipal_address_id bigint references reference_data.municipal_addresses(id),
    building_footprint_id bigint references reference_data.building_footprints(id),
    parcel_id bigint references reference_data.parcels(id),
    address_match_method text not null check (
        address_match_method in ('EXACT_UNIT_MATCH', 'EXACT_CIVIC_MATCH', 'NORMALIZED_MATCH', 'AMBIGUOUS', 'NO_MATCH')
    ),
    address_match_confidence numeric(4, 3) check (
        address_match_confidence is null or address_match_confidence between 0 and 1
    ),
    building_match_method text not null check (
        building_match_method in ('EXACT_CONTAINMENT', 'NEAR_UNAMBIGUOUS', 'AMBIGUOUS', 'NO_BUILDING')
    ),
    building_match_confidence numeric(4, 3) check (
        building_match_confidence is null or building_match_confidence between 0 and 1
    ),
    parcel_match_method text not null check (
        parcel_match_method in ('ADDRESS_CONTAINMENT', 'BUILDING_OVERLAP', 'AMBIGUOUS', 'NO_PARCEL')
    ),
    parcel_match_confidence numeric(4, 3) check (
        parcel_match_confidence is null or parcel_match_confidence between 0 and 1
    ),
    review_required boolean not null default false,
    reason_codes jsonb not null default '[]'::jsonb,
    is_current boolean not null default false,
    resolved_at timestamptz not null default now(),
    unique (resolution_run_id, property_id),
    check (jsonb_typeof(reason_codes) = 'array')
);

create unique index property_reference_matches_current_uidx
    on reference_data.property_reference_matches (property_id)
    where is_current;
create index property_reference_matches_address_idx
    on reference_data.property_reference_matches (municipal_address_id)
    where municipal_address_id is not null;
create index property_reference_matches_building_idx
    on reference_data.property_reference_matches (building_footprint_id)
    where building_footprint_id is not null;
create index property_reference_matches_parcel_idx
    on reference_data.property_reference_matches (parcel_id)
    where parcel_id is not null;

comment on schema reference_data is
    'Versioned City of London public reference data for conservative property-resolution evidence.';
comment on table reference_data.parcels is
    'Municipal parcel evidence only; not legal survey truth, ownership evidence, or rental-legality evidence.';
comment on table reference_data.property_reference_matches is
    'Shadow property-resolution evidence. It never merges housing advertisements or replaces Geoapify coordinates automatically.';
