"""Persistence adapters for immutable-versioned walking surface artifacts."""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol


SurfaceStatus = Literal["ready", "computing", "failed", "unavailable"]


@dataclass(frozen=True)
class SurfaceRecord:
    id: str
    property_id: int
    latitude: float
    longitude: float
    cache_identity: str
    routing_fingerprint: str
    grid_fingerprint: str
    network_fingerprint: str
    r5py_version: str
    r5_version: str
    status: SurfaceStatus
    origin_snap_distance_metres: float | None = None
    computed_at: datetime | None = None
    duration_ms: int | None = None
    reachable_count: int | None = None
    unavailable_count: int | None = None
    compressed_payload: bytes | None = None
    validity_mask: bytes | None = None
    raw_length: int | None = None
    compressed_length: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    etag: str | None = None


class SurfaceRepository(Protocol):
    def find(self, cache_identity: str) -> SurfaceRecord | None: ...
    def find_by_id(self, surface_id: str) -> SurfaceRecord | None: ...
    def claim(self, record: SurfaceRecord) -> tuple[Literal["owned", "ready", "computing"], SurfaceRecord]: ...
    def mark_ready(self, record: SurfaceRecord) -> SurfaceRecord: ...
    def mark_failed(self, surface_id: str, *, code: str, detail: str) -> None: ...


class InMemorySurfaceRepository:
    def __init__(self) -> None:
        self.rows: dict[str, SurfaceRecord] = {}

    def find(self, cache_identity: str) -> SurfaceRecord | None:
        return self.rows.get(cache_identity)

    def find_by_id(self, surface_id: str) -> SurfaceRecord | None:
        return next((row for row in self.rows.values() if row.id == surface_id), None)

    def claim(self, record: SurfaceRecord) -> tuple[Literal["owned", "ready", "computing"], SurfaceRecord]:
        current = self.rows.get(record.cache_identity)
        if current is None or current.status == "failed":
            self.rows[record.cache_identity] = record
            return "owned", record
        return ("ready" if current.status == "ready" else "computing"), current

    def mark_ready(self, record: SurfaceRecord) -> SurfaceRecord:
        self.rows[record.cache_identity] = record
        return record

    def mark_failed(self, surface_id: str, *, code: str, detail: str) -> None:
        for identity, row in self.rows.items():
            if row.id == surface_id:
                self.rows[identity] = SurfaceRecord(**{**row.__dict__, "status": "failed", "error_code": code, "error_detail": detail})
                return


class PostgresSurfaceRepository:
    """Small synchronous repository; uniqueness protects cross-process requests."""
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @classmethod
    def from_environment(cls) -> "PostgresSurfaceRepository":
        url = os.getenv("DATABASE_URL", "").strip() or os.getenv("ACCESSIBILITY_DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("DATABASE_URL or ACCESSIBILITY_DATABASE_URL is required for walking surfaces")
        return cls(url)

    def _connect(self) -> Any:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.database_url, row_factory=dict_row)

    @staticmethod
    def _record(row: dict[str, Any]) -> SurfaceRecord:
        return SurfaceRecord(
            id=str(row["id"]), property_id=int(row["property_id"]), latitude=float(row["origin_latitude"]), longitude=float(row["origin_longitude"]),
            cache_identity=row["cache_identity"], routing_fingerprint=row["routing_fingerprint"], grid_fingerprint=row["grid_fingerprint"], status=row["status"],
            network_fingerprint=row["network_fingerprint"], r5py_version=row["r5py_version"], r5_version=row["r5_version"],
            origin_snap_distance_metres=row["origin_snap_distance_metres"],
            computed_at=row["computed_at"], duration_ms=row["duration_ms"], reachable_count=row["reachable_count"], unavailable_count=row["unavailable_count"],
            compressed_payload=row["compressed_payload"], validity_mask=row["destination_validity_mask"], raw_length=row["raw_length"], compressed_length=row["compressed_length"],
            error_code=row["error_code"], error_detail=row["error_detail"], etag=row["etag"],
        )

    def find(self, cache_identity: str) -> SurfaceRecord | None:
        with self._connect() as connection:
            row = connection.execute("select * from public.housing_walk_time_surfaces where cache_identity = %s", (cache_identity,)).fetchone()
        return self._record(row) if row else None

    def find_by_id(self, surface_id: str) -> SurfaceRecord | None:
        try:
            parsed_id = uuid.UUID(surface_id)
        except (TypeError, ValueError):
            return None
        with self._connect() as connection:
            row = connection.execute("select * from public.housing_walk_time_surfaces where id = %s", (parsed_id,)).fetchone()
        return self._record(row) if row else None

    def claim(self, record: SurfaceRecord) -> tuple[Literal["owned", "ready", "computing"], SurfaceRecord]:
        with self._connect() as connection, connection.transaction():
            # The advisory lock makes the read/failed-retry transition atomic;
            # the durable unique cache identity protects after transaction end.
            connection.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (record.cache_identity,))
            row = connection.execute("select * from public.housing_walk_time_surfaces where cache_identity = %s for update", (record.cache_identity,)).fetchone()
            if row and row["status"] == "ready":
                return "ready", self._record(row)
            if row and row["status"] == "computing":
                return "computing", self._record(row)
            if row:
                updated = connection.execute(
                    "update public.housing_walk_time_surfaces set status = 'computing', error_code = null, error_detail = null, started_at = now() where id = %s returning *", (row["id"],)
                ).fetchone()
                return "owned", self._record(updated)
            inserted = connection.execute(
                """insert into public.housing_walk_time_surfaces
                (id, property_id, origin_latitude, origin_longitude, cache_identity, routing_fingerprint, grid_fingerprint,
                 network_fingerprint, r5py_version, r5_version, status)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'computing') returning *""",
                (record.id, record.property_id, record.latitude, record.longitude, record.cache_identity, record.routing_fingerprint,
                 record.grid_fingerprint, record.network_fingerprint, record.r5py_version, record.r5_version),
            ).fetchone()
            return "owned", self._record(inserted)

    def mark_ready(self, record: SurfaceRecord) -> SurfaceRecord:
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """update public.housing_walk_time_surfaces set status='ready', computed_at=%s, duration_ms=%s,
                reachable_count=%s, unavailable_count=%s, raw_length=%s, compressed_length=%s,
                compressed_payload=%s, destination_validity_mask=%s, etag=%s, origin_snap_distance_metres=%s,
                error_code=null, error_detail=null
                where id=%s and status='computing' returning *""",
                (record.computed_at, record.duration_ms, record.reachable_count, record.unavailable_count, record.raw_length, record.compressed_length,
                 record.compressed_payload, record.validity_mask, record.etag, record.origin_snap_distance_metres, record.id),
            ).fetchone()
        if row is None:
            raise RuntimeError("walking surface was no longer computing")
        return self._record(row)

    def mark_failed(self, surface_id: str, *, code: str, detail: str) -> None:
        with self._connect() as connection:
            connection.execute("update public.housing_walk_time_surfaces set status='failed', error_code=%s, error_detail=%s where id=%s", (code, detail[:500], surface_id))


def new_pending_surface(*, property_id: int, latitude: float, longitude: float, cache_identity: str, routing_fingerprint: str, grid_fingerprint: str, network_fingerprint: str = "unknown", r5py_version: str = "unknown", r5_version: str = "unknown") -> SurfaceRecord:
    return SurfaceRecord(id=str(uuid.uuid4()), property_id=property_id, latitude=latitude, longitude=longitude, cache_identity=cache_identity, routing_fingerprint=routing_fingerprint, grid_fingerprint=grid_fingerprint, network_fingerprint=network_fingerprint, r5py_version=r5py_version, r5_version=r5_version, status="computing")
