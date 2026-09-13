-- Immutable property-coordinate promotion history and rollback snapshots.

create table public.housing_coordinate_promotion_runs (
    id bigint generated always as identity primary key,
    run_id text not null unique,
    promotion_policy_version text not null,
    coordinate_selection_policy_version text not null,
    coordinate_selection_policy_fingerprint char(64) not null,
    candidate_run_id text not null,
    candidate_fingerprint char(64) not null,
    status text not null check (status in (
        'executing', 'cutover_completed', 'recomputing',
        'validation_failed', 'completed', 'rolled_back',
        'rollback_completed', 'failed'
    )),
    expected_property_count integer not null check (expected_property_count > 0),
    eligible_property_count integer not null check (eligible_property_count > 0),
    stale_property_count integer not null check (stale_property_count >= 0),
    before_state_fingerprint char(64) not null,
    backup_path text not null,
    backup_sha256 char(64) not null,
    final_state_fingerprint char(64),
    rollback_of_run_id bigint references public.housing_coordinate_promotion_runs(id),
    superseded_by_run_id bigint references public.housing_coordinate_promotion_runs(id),
    summary jsonb not null default '{}'::jsonb,
    recomputation_summary jsonb not null default '{}'::jsonb,
    validation_summary jsonb not null default '{}'::jsonb,
    started_at timestamptz not null,
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    check (btrim(run_id) <> ''),
    check (btrim(promotion_policy_version) <> ''),
    check (btrim(coordinate_selection_policy_version) <> ''),
    check (coordinate_selection_policy_fingerprint ~ '^[0-9a-f]{64}$'),
    check (candidate_fingerprint ~ '^[0-9a-f]{64}$'),
    check (before_state_fingerprint ~ '^[0-9a-f]{64}$'),
    check (backup_sha256 ~ '^[0-9a-f]{64}$'),
    check (final_state_fingerprint is null or final_state_fingerprint ~ '^[0-9a-f]{64}$'),
    check (jsonb_typeof(summary) = 'object'),
    check (jsonb_typeof(recomputation_summary) = 'object'),
    check (jsonb_typeof(validation_summary) = 'object')
);

create table public.housing_coordinate_promotion_items (
    id bigint generated always as identity primary key,
    promotion_run_id bigint not null references public.housing_coordinate_promotion_runs(id),
    property_id bigint not null references public.housing_properties(id),
    candidate_evidence_fingerprint char(64) not null,
    previous_property_state jsonb not null,
    previous_map_projections jsonb not null,
    previous_visibility_state jsonb,
    new_coordinate jsonb not null,
    movement_meters double precision not null check (
        movement_meters >= 0 and movement_meters <= 100
    ),
    city_dataset_run_id bigint not null,
    city_dataset_fingerprint char(64) not null,
    municipal_address_id bigint not null,
    building_id bigint,
    parcel_id bigint,
    promotion_reason text not null,
    promoted_at timestamptz not null,
    created_at timestamptz not null default now(),
    unique (promotion_run_id, property_id),
    check (candidate_evidence_fingerprint ~ '^[0-9a-f]{64}$'),
    check (city_dataset_fingerprint ~ '^[0-9a-f]{64}$'),
    check (jsonb_typeof(previous_property_state) = 'object'),
    check (jsonb_typeof(previous_map_projections) = 'array'),
    check (previous_visibility_state is null or jsonb_typeof(previous_visibility_state) = 'object'),
    check (jsonb_typeof(new_coordinate) = 'object'),
    check (btrim(promotion_reason) <> '')
);

create index housing_coordinate_promotion_items_property_idx
    on public.housing_coordinate_promotion_items (property_id, promoted_at desc);

create index housing_coordinate_promotion_runs_status_idx
    on public.housing_coordinate_promotion_runs (status, started_at desc);

alter table public.housing_coordinate_promotion_runs enable row level security;
alter table public.housing_coordinate_promotion_items enable row level security;

comment on table public.housing_coordinate_promotion_runs is
    'Versioned canonical coordinate cutovers with backup, validation, and rollback linkage.';
comment on table public.housing_coordinate_promotion_items is
    'Immutable per-property before-state and accepted City evidence for coordinate promotion.';
