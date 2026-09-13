from pathlib import Path


MIGRATION = Path("supabase/migrations/20260810000100_create_walking_travel_time_surfaces.sql")


def test_walk_surface_migration_is_compact_versioned_and_walk_only() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "create table public.housing_walk_time_surfaces" in sql
    assert "property_id bigint not null references public.housing_properties(id)" in sql
    assert "cache_identity char(64) not null unique" in sql
    assert "network_fingerprint char(64) not null" in sql
    assert "r5py_version text not null" in sql
    assert "r5_version text not null" in sql
    assert "compressed_payload bytea" in sql
    assert "destination_validity_mask bytea" in sql
    assert "raw_length integer check (raw_length is null or raw_length = 29606)" in sql
    assert "status in ('ready', 'computing', 'failed', 'unavailable')" in sql
    assert "cycling" not in sql and "transit" not in sql
    assert "enable row level security" in sql
