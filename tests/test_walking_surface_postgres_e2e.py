"""Real PostgreSQL + local R5 lifecycle coverage for walking surfaces.

Run explicitly with TEST_DATABASE_URL set and the local ``r5-surface``
container ready on the loopback-only R5_SURFACE_URL endpoint.
"""
from __future__ import annotations

import concurrent.futures
import gzip
import os
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.repository import PostgresListingRepository
from backend.travel_time_surface_repository import PostgresSurfaceRepository
from backend.travel_time_surface_service import (
    HttpR5SurfaceClient,
    SurfaceError,
    WalkingSurfaceService,
)
from backend.travel_time_surfaces import (
    cache_identity,
    decode_seconds,
    decode_validity_mask,
    load_surface_policy,
    routing_fingerprint,
)


pytestmark = [pytest.mark.postgres, pytest.mark.r5]
POLICY = load_surface_policy(Path("config/travel-time-surface.example.toml"))
NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


class _GateR5:
    """Hold exactly one real R5 request long enough to exercise DB locking."""

    def __init__(self, delegate: HttpR5SurfaceClient) -> None:
        self.delegate = delegate
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def health(self) -> dict[str, object]:
        return self.delegate.health()

    def walking_surface(self, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=20):
            raise SurfaceError("test gate timed out")
        return self.delegate.walking_surface(**kwargs)  # type: ignore[arg-type]


class _CountingR5:
    """Observe calls without changing the real worker implementation."""

    def __init__(self, delegate: HttpR5SurfaceClient) -> None:
        self.delegate = delegate
        self.walking_calls = 0

    def health(self) -> dict[str, object]:
        return self.delegate.health()

    def walking_surface(self, **kwargs: object) -> dict[str, object]:
        self.walking_calls += 1
        return self.delegate.walking_surface(**kwargs)  # type: ignore[arg-type]


class _FailOnceR5:
    """Inject one safe service-layer failure; the retry delegates to real R5."""

    def __init__(self, delegate: HttpR5SurfaceClient) -> None:
        self.delegate = delegate
        self.failed = False

    def health(self) -> dict[str, object]:
        return self.delegate.health()

    def walking_surface(self, **kwargs: object) -> dict[str, object]:
        if not self.failed:
            self.failed = True
            raise SurfaceError("injected R5 failure for retry coverage")
        return self.delegate.walking_surface(**kwargs)  # type: ignore[arg-type]


def _seed_fixture(connection) -> tuple[dict[str, int], dict[str, str]]:
    run_id = connection.execute(
        """
        insert into public.housing_pipeline_runs (
            run_id, source, status, canonical_for_import, started_at,
            completed_at, manifest_json, import_status, canonical_sha256,
            manifest_sha256
        ) values ('walk-surface-e2e', 'uwo_offcampus', 'completed', true, %s, %s,
                  '{}'::jsonb, 'completed', repeat('a', 64), repeat('b', 64))
        returning id
        """,
        (NOW, NOW),
    ).fetchone()[0]
    property_coordinates = {
        "shared": (43.010161008339615, -81.25833282208825),
        "other": (42.99120169375, -81.24700425),
        "concurrent": (42.953913, -81.190658),
        "retry": (43.010161008339615, -81.25833282208825),
        "invalid": (0.0, 0.0),
    }
    properties: dict[str, int] = {}
    for name, (latitude, longitude) in property_coordinates.items():
        properties[name] = int(
            connection.execute(
                """
                insert into public.housing_properties (
                    normalized_address, display_address, latitude, longitude,
                    geocode_status, address_complete, match_key
                ) values (%s, %s, %s, %s, 'ok', true, %s) returning id
                """,
                (f"{name} walk fixture", f"{name.title()} Walk Fixture", latitude, longitude, f"walk-e2e-{name}"),
            ).fetchone()[0]
        )

    listing_sources = {"one": "walk-one", "two": "walk-two", "other": "walk-other", "invalid": "walk-invalid"}
    listing_properties = {"one": "shared", "two": "shared", "other": "other", "invalid": "invalid"}
    listings: dict[str, str] = {}
    for offset, (name, source_id) in enumerate(listing_sources.items()):
        property_id = properties[listing_properties[name]]
        latitude, longitude = property_coordinates[listing_properties[name]]
        listing_id = int(
            connection.execute(
                """
                insert into public.housing_listings (
                    source, source_listing_id, source_url, property_id,
                    first_seen_pipeline_run_id, last_seen_pipeline_run_id, status
                ) values ('uwo_offcampus', %s, %s, %s, %s, %s, 'active') returning id
                """,
                (source_id, f"https://example.invalid/{source_id}", property_id, run_id, run_id),
            ).fetchone()[0]
        )
        connection.execute(
            """
            insert into public.housing_listing_observations (
                listing_id, pipeline_run_id, property_id, observed_at, change_type,
                title, address, price_text, price_numeric, price_period, price_monthly,
                bedrooms, housing_type, lease_type, is_sublet, latitude, longitude,
                map_ready, geocode_status, geocode_confidence, distance_to_western_km,
                raw_data, provenance_data, confidence_data, comparison_data, review_flags,
                observation_hash
            ) values (%s,%s,%s,%s,'new',%s,%s,'$900',900,'month',900,1,'room',
                'standard',false,%s,%s,true,'ok',0.95,1.0,'{}'::jsonb,'{}'::jsonb,
                '{}'::jsonb,'{}'::jsonb,'[]'::jsonb,%s)
            """,
            (listing_id, run_id, property_id, NOW, f"Walking E2E {name}", f"{name.title()} fixture", latitude, longitude, f"{offset + 1:064x}"),
        )
        listings[name] = source_id
    return properties, listings


def _surface_count(connection, property_id: int | None = None) -> int:
    if property_id is None:
        return int(connection.execute("select count(*) from public.housing_walk_time_surfaces").fetchone()[0])
    return int(connection.execute("select count(*) from public.housing_walk_time_surfaces where property_id = %s", (property_id,)).fetchone()[0])


def test_real_walking_surface_lifecycle(postgres_database, postgres_target) -> None:
    """Validate cache, binary contract, locking, retry, and compatibility on real services."""
    assert postgres_database.execute(
        "select extname from pg_extension where extname = 'postgis'"
    ).fetchone()[0] == "postgis"
    index_names = {
        row[0]
        for row in postgres_database.execute(
            """select indexname from pg_catalog.pg_indexes
            where schemaname = 'public' and tablename = 'housing_walk_time_surfaces'"""
        )
    }
    assert {
        "housing_walk_surface_property_current_idx",
        "housing_walk_surface_status_idx",
        "housing_walk_surface_routing_idx",
    } <= index_names
    r5_url = os.getenv("R5_SURFACE_URL", "http://127.0.0.1:8091")
    r5 = HttpR5SurfaceClient(r5_url, timeout_seconds=70)
    startup_started = time.perf_counter()
    health = r5.health()
    worker_ready_ms = round((time.perf_counter() - startup_started) * 1000)
    assert health["status"] == "ready"
    assert health["grid_fingerprint"] == POLICY.grid_fingerprint

    properties, listings = _seed_fixture(postgres_database)
    surface_repository = PostgresSurfaceRepository(postgres_target.url)
    counted_r5 = _CountingR5(r5)
    service = WalkingSurfaceService(surface_repository, counted_r5, POLICY)
    api = TestClient(create_app(
        repository=PostgresListingRepository(postgres_target.url),
        walking_surface_service=service,
    ))

    grid = api.get("/api/travel-time-surfaces/grid")
    assert grid.status_code == 200
    grid_body = grid.json()
    assert grid_body["grid_version"] == POLICY.grid_version
    assert grid_body["grid_fingerprint"] == POLICY.grid_fingerprint
    assert grid_body["crs"] == POLICY.crs
    assert (grid_body["rows"], grid_body["columns"], grid_body["cell_count"]) == (POLICY.rows, POLICY.columns, POLICY.cell_count)
    assert grid_body["cell_size_metres"] == POLICY.cell_size_metres
    assert grid_body["extent"] == POLICY.grid_metadata()["extent"]
    assert grid_body["ordering"] == POLICY.ordering
    assert grid_body["validity_mask"] == "destination-snap-bitset-v1"

    uncached_started = time.perf_counter()
    first = api.get(f"/api/listings/{listings['one']}/travel-time-surface")
    uncached_ms = round((time.perf_counter() - uncached_started) * 1000)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["status"] == "ready" and first_body["cache_hit"] is False
    assert first_body["property_id"] == properties["shared"]
    assert first_body["network_fingerprint"] == health["network_fingerprint"]
    assert first_body["r5py_version"] == health["r5py_version"]
    assert first_body["r5_version"] == health["r5_version"]
    assert counted_r5.walking_calls == 1
    assert _surface_count(postgres_database) == 1

    record = surface_repository.find_by_id(first_body["surface_id"])
    assert record is not None and record.status == "ready"
    assert (record.latitude, record.longitude) == pytest.approx((43.010161008339615, -81.25833282208825))
    assert record.grid_fingerprint == POLICY.grid_fingerprint
    assert record.network_fingerprint == health["network_fingerprint"]
    assert record.compressed_payload and record.compressed_length == len(record.compressed_payload)
    assert record.validity_mask and len(record.validity_mask) == POLICY.validity_mask_bytes
    assert record.duration_ms is not None and record.duration_ms >= 0
    values = decode_seconds(gzip.decompress(record.compressed_payload), POLICY)
    assert len(values) == POLICY.cell_count
    assert decode_validity_mask(record.validity_mask, POLICY)
    assert record.reachable_count is not None and record.unavailable_count is not None
    assert record.reachable_count + record.unavailable_count == POLICY.cell_count

    cached_started = time.perf_counter()
    second = api.get(f"/api/listings/{listings['two']}/travel-time-surface")
    cached_ms = round((time.perf_counter() - cached_started) * 1000)
    assert second.status_code == 200 and second.json()["cache_hit"] is True
    assert second.json()["surface_id"] == first_body["surface_id"]
    assert counted_r5.walking_calls == 1
    assert _surface_count(postgres_database) == 1

    values_started = time.perf_counter()
    binary = api.get(first_body["value_url"])
    cached_values_ms = round((time.perf_counter() - values_started) * 1000)
    assert binary.status_code == 200
    assert binary.headers["content-type"].startswith("application/octet-stream")
    assert binary.headers["content-encoding"] == "gzip"
    assert binary.headers["etag"] == record.etag
    assert "max-age=3600" in binary.headers["cache-control"]
    # TestClient transparently expands Content-Encoding; the stored payload is
    # separately decoded above, so this confirms the endpoint vector contract.
    assert decode_seconds(binary.content, POLICY) == values
    assert api.get("/api/travel-time-surfaces/not-a-real-surface/values").status_code == 404
    assert api.get(f"/api/listings/{listings['one']}/travel-time-surface?mode=bicycle").status_code == 422
    assert api.get(f"/api/listings/{listings['one']}/travel-time-surface?mode=transit").status_code == 422

    isolated = api.get(f"/api/listings/{listings['other']}/travel-time-surface")
    assert isolated.status_code == 200 and isolated.json()["surface_id"] != first_body["surface_id"]
    assert _surface_count(postgres_database, properties["other"]) == 1

    base_fingerprint = routing_fingerprint(
        network_fingerprint=str(health["network_fingerprint"]), r5py_version=str(health["r5py_version"]),
        r5_version=str(health["r5_version"]), policy=POLICY,
    )
    base_identity = cache_identity(property_id=properties["shared"], latitude=43.010161008339615, longitude=-81.25833282208825, routing_fingerprint_value=base_fingerprint, policy=POLICY)
    assert surface_repository.find(base_identity) is not None
    assert cache_identity(property_id=properties["shared"], latitude=43.010171008339615, longitude=-81.25833282208825, routing_fingerprint_value=base_fingerprint, policy=POLICY) != base_identity
    changed_grid = replace(POLICY, grid_fingerprint="f" * 64)
    assert cache_identity(property_id=properties["shared"], latitude=43.010161008339615, longitude=-81.25833282208825, routing_fingerprint_value=base_fingerprint, policy=changed_grid) != base_identity
    assert routing_fingerprint(network_fingerprint="a" * 64, r5py_version=str(health["r5py_version"]), r5_version=str(health["r5_version"]), policy=POLICY) != base_fingerprint
    assert routing_fingerprint(network_fingerprint=str(health["network_fingerprint"]), r5py_version="changed", r5_version=str(health["r5_version"]), policy=POLICY) != base_fingerprint
    # The walking function intentionally has no GTFS input: a GTFS change
    # cannot invalidate this OSM/R5 walking artifact.
    assert base_fingerprint == routing_fingerprint(network_fingerprint=str(health["network_fingerprint"]), r5py_version=str(health["r5py_version"]), r5_version=str(health["r5_version"]), policy=POLICY)

    invalid = api.get(f"/api/listings/{listings['invalid']}/travel-time-surface")
    assert invalid.status_code == 422
    invalid_rows = postgres_database.execute(
        "select id, status, compressed_payload from public.housing_walk_time_surfaces where property_id = %s", (properties["invalid"],)
    ).fetchall()
    assert invalid_rows and invalid_rows[0][1] == "failed" and invalid_rows[0][2] is None
    assert api.get(f"/api/travel-time-surfaces/{invalid_rows[0][0]}/values").status_code == 409

    gate = _GateR5(r5)
    concurrent_service = WalkingSurfaceService(surface_repository, gate, POLICY)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(concurrent_service.get_or_compute, property_id=properties["concurrent"], latitude=42.953913, longitude=-81.190658)
        assert gate.started.wait(timeout=15)
        observer = executor.submit(concurrent_service.get_or_compute, property_id=properties["concurrent"], latitude=42.953913, longitude=-81.190658)
        observed_record, observed_hit = observer.result(timeout=20)
        assert observed_record.status == "computing" and observed_hit is False
        gate.release.set()
        owned_record, owned_hit = owner.result(timeout=90)
    assert owned_record.status == "ready" and owned_hit is False and gate.calls == 1
    assert _surface_count(postgres_database, properties["concurrent"]) == 1

    fail_once = _FailOnceR5(r5)
    retry_service = WalkingSurfaceService(surface_repository, fail_once, POLICY)
    with pytest.raises(SurfaceError, match="injected R5 failure"):
        retry_service.get_or_compute(property_id=properties["retry"], latitude=43.010161008339615, longitude=-81.25833282208825)
    failed = postgres_database.execute(
        "select status, compressed_payload, error_code from public.housing_walk_time_surfaces where property_id = %s", (properties["retry"],)
    ).fetchone()
    assert failed == ("failed", None, "r5_error")
    retried, retry_hit = retry_service.get_or_compute(property_id=properties["retry"], latitude=43.010161008339615, longitude=-81.25833282208825)
    assert retried.status == "ready" and retry_hit is False

    with pytest.raises(psycopg.errors.UniqueViolation):
        postgres_database.execute(
            """insert into public.housing_walk_time_surfaces
            (id, property_id, origin_latitude, origin_longitude, cache_identity, routing_fingerprint,
             grid_fingerprint, network_fingerprint, r5py_version, r5_version, status)
            values (%s,%s,43.01,-81.25,%s,%s,%s,%s,'test','test','computing')""",
            (uuid.uuid4(), properties["shared"], base_identity, base_fingerprint, POLICY.grid_fingerprint, str(health["network_fingerprint"])),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        postgres_database.execute(
            """insert into public.housing_walk_time_surfaces
            (id, property_id, origin_latitude, origin_longitude, cache_identity, routing_fingerprint,
             grid_fingerprint, network_fingerprint, r5py_version, r5_version, status)
            values (%s,%s,43.01,-81.25,%s,%s,%s,%s,'test','test','invalid')""",
            (uuid.uuid4(), properties["shared"], "d" * 64, base_fingerprint, POLICY.grid_fingerprint, str(health["network_fingerprint"])),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        postgres_database.execute("delete from public.housing_properties where id = %s", (properties["shared"],))

    persistence_ms = max(0, uncached_ms - int(record.duration_ms or 0))
    print(
        "walking-surface-e2e metrics: "
        f"worker_health_ms={worker_ready_ms}, network_ready_seconds={health.get('network_ready_seconds')}, "
        f"uncached_api_ms={uncached_ms}, r5_compute_ms={record.duration_ms}, "
        f"approx_persist_ms={persistence_ms}, cached_metadata_ms={cached_ms}, "
        f"cached_values_ms={cached_values_ms}, gzip_bytes={len(record.compressed_payload)}"
    )
    assert worker_ready_ms >= 0 and uncached_ms >= 0 and cached_ms >= 0 and cached_values_ms >= 0
