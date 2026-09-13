-- Official City of London mobility and recreation reference evidence.
-- This migration is additive and intentionally creates no property relationships
-- or product-facing behavior.

create schema if not exists reference_data;

alter table reference_data.dataset_runs
    drop constraint if exists dataset_runs_dataset_name_check;
alter table reference_data.dataset_runs
    drop constraint if exists reference_dataset_runs_dataset_name_check;
alter table reference_data.dataset_runs
    add constraint reference_dataset_runs_dataset_name_check check (
        dataset_name in (
            'municipal_addresses', 'building_footprints', 'parcels',
            'bicycle_routes', 'recreation_paths_multi_use',
            'thames_valley_parkway', 'walking_trails_unpaved', 'sidewalks',
            'walkways', 'parks', 'pedestrian_crossovers',
            'signalized_intersections'
        )
    );

create table reference_data.london_bicycle_routes (
    id bigint generated always as identity primary key,
    dataset_run_id bigint not null references reference_data.dataset_runs(id),
    source_object_id text not null,
    source_last_edit_at timestamptz,
    source_attributes jsonb not null,
    geometry geometry(Geometry, 26917) not null,
    gis_id text,
    route_name text,
    from_street text,
    to_street text,
    facility_type text,
    directionality text,
    travel_direction text,
    status text,
    left_protection text,
    right_protection text,
    is_private text,
    installation_year text,
    created_at timestamptz not null default now(),
    unique (dataset_run_id, source_object_id),
    check (jsonb_typeof(source_attributes) = 'object'),
    check (st_geometrytype(geometry) in ('ST_LineString', 'ST_MultiLineString'))
);

create index london_bicycle_routes_geometry_gix
    on reference_data.london_bicycle_routes using gist (geometry);
create index london_bicycle_routes_run_source_idx
    on reference_data.london_bicycle_routes (dataset_run_id, source_object_id);
create index london_bicycle_routes_facility_idx
    on reference_data.london_bicycle_routes (facility_type)
    where facility_type is not null;

create table reference_data.london_recreation_paths (
    id bigint generated always as identity primary key,
    dataset_run_id bigint not null references reference_data.dataset_runs(id),
    source_object_id text not null,
    source_last_edit_at timestamptz,
    source_attributes jsonb not null,
    geometry geometry(Geometry, 26917) not null,
    gis_id text,
    path_name text,
    path_type text,
    source_category text,
    park_category text,
    status text,
    source text,
    asset_id text,
    created_at timestamptz not null default now(),
    unique (dataset_run_id, source_object_id),
    check (jsonb_typeof(source_attributes) = 'object'),
    check (st_geometrytype(geometry) in ('ST_LineString', 'ST_MultiLineString'))
);

create index london_recreation_paths_geometry_gix
    on reference_data.london_recreation_paths using gist (geometry);
create index london_recreation_paths_run_source_idx
    on reference_data.london_recreation_paths (dataset_run_id, source_object_id);
create index london_recreation_paths_source_category_idx
    on reference_data.london_recreation_paths (source_category)
    where source_category is not null;

create table reference_data.london_sidewalks (
    id bigint generated always as identity primary key,
    dataset_run_id bigint not null references reference_data.dataset_runs(id),
    source_object_id text not null,
    source_last_edit_at timestamptz,
    source_attributes jsonb not null,
    geometry geometry(Geometry, 26917) not null,
    assumed text,
    width_text text,
    material text,
    beat text,
    created_at timestamptz not null default now(),
    unique (dataset_run_id, source_object_id),
    check (jsonb_typeof(source_attributes) = 'object'),
    check (st_geometrytype(geometry) in ('ST_LineString', 'ST_MultiLineString'))
);

create index london_sidewalks_geometry_gix
    on reference_data.london_sidewalks using gist (geometry);
create index london_sidewalks_run_source_idx
    on reference_data.london_sidewalks (dataset_run_id, source_object_id);

create table reference_data.london_walkways (
    id bigint generated always as identity primary key,
    dataset_run_id bigint not null references reference_data.dataset_runs(id),
    source_object_id text not null,
    source_last_edit_at timestamptz,
    source_attributes jsonb not null,
    geometry geometry(Geometry, 26917) not null,
    gis_id text,
    assumed text,
    from_number text,
    from_name text,
    to_number text,
    to_name text,
    created_at timestamptz not null default now(),
    unique (dataset_run_id, source_object_id),
    check (jsonb_typeof(source_attributes) = 'object'),
    check (st_geometrytype(geometry) in ('ST_LineString', 'ST_MultiLineString'))
);

create index london_walkways_geometry_gix
    on reference_data.london_walkways using gist (geometry);
create index london_walkways_run_source_idx
    on reference_data.london_walkways (dataset_run_id, source_object_id);

create table reference_data.london_parks (
    id bigint generated always as identity primary key,
    dataset_run_id bigint not null references reference_data.dataset_runs(id),
    source_object_id text not null,
    source_last_edit_at timestamptz,
    source_attributes jsonb not null,
    geometry geometry(Geometry, 26917) not null,
    gis_id text,
    park_name text,
    address text,
    park_category text,
    legacy_park_category text,
    hectares numeric,
    acres numeric,
    park_number text,
    walking_trail_length numeric,
    paved_pathway_length numeric,
    total_pathway_length numeric,
    amenity_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique (dataset_run_id, source_object_id),
    check (jsonb_typeof(source_attributes) = 'object'),
    check (jsonb_typeof(amenity_data) = 'object'),
    check (st_geometrytype(geometry) in ('ST_Polygon', 'ST_MultiPolygon'))
);

create index london_parks_geometry_gix
    on reference_data.london_parks using gist (geometry);
create index london_parks_run_source_idx
    on reference_data.london_parks (dataset_run_id, source_object_id);
create index london_parks_category_idx
    on reference_data.london_parks (park_category)
    where park_category is not null;

create table reference_data.london_pedestrian_crossovers (
    id bigint generated always as identity primary key,
    dataset_run_id bigint not null references reference_data.dataset_runs(id),
    source_object_id text not null,
    source_last_edit_at timestamptz,
    source_attributes jsonb not null,
    geometry geometry(Point, 26917) not null,
    gis_id text,
    feature_key text,
    street text,
    location_text text,
    year_installed text,
    inspection_status text,
    created_at timestamptz not null default now(),
    unique (dataset_run_id, source_object_id),
    check (jsonb_typeof(source_attributes) = 'object')
);

create index london_pedestrian_crossovers_geometry_gix
    on reference_data.london_pedestrian_crossovers using gist (geometry);
create index london_pedestrian_crossovers_run_source_idx
    on reference_data.london_pedestrian_crossovers (dataset_run_id, source_object_id);

create table reference_data.london_signalized_intersections (
    id bigint generated always as identity primary key,
    dataset_run_id bigint not null references reference_data.dataset_runs(id),
    source_object_id text not null,
    source_last_edit_at timestamptz,
    source_attributes jsonb not null,
    geometry geometry(Point, 26917) not null,
    gis_id text,
    intersection_identifier text,
    cartographic_subtype text,
    signal_status text,
    signal_type text,
    created_at timestamptz not null default now(),
    unique (dataset_run_id, source_object_id),
    check (jsonb_typeof(source_attributes) = 'object')
);

create index london_signalized_intersections_geometry_gix
    on reference_data.london_signalized_intersections using gist (geometry);
create index london_signalized_intersections_run_source_idx
    on reference_data.london_signalized_intersections (dataset_run_id, source_object_id);

comment on table reference_data.london_bicycle_routes is
    'Official City bicycle-route attributes as shadow reference evidence only; no safety or comfort score is implied.';
comment on table reference_data.london_recreation_paths is
    'Official City recreation-path evidence, including Thames Valley Parkway source layers, retained by dataset version.';
comment on table reference_data.london_sidewalks is
    'Official City sidewalk inventory as factual shadow evidence only; material and width do not imply quality.';
comment on table reference_data.london_walkways is
    'Official City pedestrian walkway inventory as factual shadow evidence only.';
comment on table reference_data.london_parks is
    'Official City park geometry and factual metadata only; it does not create park-quality or housing scores.';
comment on table reference_data.london_pedestrian_crossovers is
    'Official City pedestrian crossover locations only; no safety claim is implied.';
comment on table reference_data.london_signalized_intersections is
    'Official City signalized-intersection locations only; no safety claim is implied.';
