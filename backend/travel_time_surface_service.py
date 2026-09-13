"""API-facing orchestration for accepted walking numerical surfaces."""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from backend.travel_time_surface_repository import SurfaceRecord, SurfaceRepository, new_pending_surface
from backend.travel_time_surfaces import (
    ALGORITHM_VERSION, SurfacePolicy, cache_identity, compress_payload, encode_seconds,
    encode_validity_mask, routing_fingerprint,
)


class SurfaceError(RuntimeError): pass
class SurfaceUnavailable(SurfaceError): pass
class UnsupportedSurfaceMode(SurfaceError): pass


class R5SurfaceClient(Protocol):
    def health(self) -> dict[str, Any]: ...
    def walking_surface(self, *, latitude: float, longitude: float, policy: SurfacePolicy) -> dict[str, Any]: ...


class HttpR5SurfaceClient:
    def __init__(self, base_url: str | None = None, *, timeout_seconds: int = 35) -> None:
        self.base_url = (base_url or os.getenv("R5_SURFACE_URL", "http://127.0.0.1:8091")).rstrip("/")
        if not self.base_url.startswith(("http://127.0.0.1", "http://localhost")):
            raise ValueError("R5 surface service must use loopback URL")
        self.timeout_seconds = timeout_seconds

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(self.base_url + path, method="POST" if payload is not None else "GET")
        if payload is not None:
            request.data = json.dumps(payload).encode()
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", "R5 request failed")
            except Exception:
                detail = "R5 request failed"
            raise SurfaceError(str(detail)) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SurfaceUnavailable("R5 walking service is unavailable") from exc

    def health(self) -> dict[str, Any]: return self._request("/health")

    def walking_surface(self, *, latitude: float, longitude: float, policy: SurfacePolicy) -> dict[str, Any]:
        return self._request("/walking-surface", {
            "mode": "walking", "latitude": latitude, "longitude": longitude,
            "origin_max_snap_metres": policy.origin_snap_metres,
            "destination_max_snap_metres": policy.destination_snap_metres,
            "max_travel_seconds": policy.max_travel_seconds,
        })


@dataclass
class WalkingSurfaceService:
    repository: SurfaceRepository
    r5: R5SurfaceClient
    policy: SurfacePolicy

    def _fingerprints(self) -> tuple[dict[str, Any], str]:
        health = self.r5.health()
        if health.get("status") != "ready" or health.get("grid_fingerprint") != self.policy.grid_fingerprint:
            raise SurfaceUnavailable("R5 walking service is not ready for the accepted grid")
        fingerprint = routing_fingerprint(
            network_fingerprint=str(health["network_fingerprint"]), r5py_version=str(health["r5py_version"]),
            r5_version=str(health["r5_version"]), policy=self.policy,
        )
        return health, fingerprint

    def get_or_compute(self, *, property_id: int, latitude: float, longitude: float, mode: str = "walking") -> tuple[SurfaceRecord, bool]:
        if mode != "walking":
            raise UnsupportedSurfaceMode("numerical travel-time surfaces support walking only")
        health, fingerprint = self._fingerprints()
        identity = cache_identity(property_id=property_id, latitude=latitude, longitude=longitude, routing_fingerprint_value=fingerprint, policy=self.policy)
        current = self.repository.find(identity)
        if current and current.status == "ready":
            return current, True
        pending = new_pending_surface(
            property_id=property_id, latitude=latitude, longitude=longitude,
            cache_identity=identity, routing_fingerprint=fingerprint,
            grid_fingerprint=self.policy.grid_fingerprint,
            network_fingerprint=str(health["network_fingerprint"]),
            r5py_version=str(health["r5py_version"]), r5_version=str(health["r5_version"]),
        )
        claim, record = self.repository.claim(pending)
        if claim == "ready": return record, True
        if claim == "computing": return record, False
        try:
            result = self.r5.walking_surface(latitude=latitude, longitude=longitude, policy=self.policy)
            if result.get("grid_fingerprint") != self.policy.grid_fingerprint or len(result.get("values_seconds", [])) != self.policy.cell_count:
                raise SurfaceError("R5 returned malformed grid data")
            values, validity = result["values_seconds"], result.get("destination_validity")
            if not isinstance(validity, list) or len(validity) != self.policy.cell_count:
                raise SurfaceError("R5 returned malformed destination validity")
            raw = encode_seconds(values, self.policy)
            compressed = compress_payload(raw)
            mask = encode_validity_mask([bool(value) for value in validity], self.policy)
            ready = SurfaceRecord(**{**record.__dict__, "status": "ready", "computed_at": datetime.now(timezone.utc), "duration_ms": int(result["duration_ms"]), "reachable_count": int(result["reachable_count"]), "unavailable_count": self.policy.cell_count - int(result["reachable_count"]), "compressed_payload": compressed, "validity_mask": mask, "raw_length": len(raw), "compressed_length": len(compressed), "origin_snap_distance_metres": float(result.get("origin_snap_distance_metres", 0)), "etag": hashlib.sha256(compressed).hexdigest()})
            return self.repository.mark_ready(ready), False
        except SurfaceError as exc:
            self.repository.mark_failed(record.id, code="r5_error", detail=str(exc))
            raise
        except Exception as exc:
            self.repository.mark_failed(record.id, code="surface_compute_failed", detail=str(exc))
            raise SurfaceError("walking surface computation failed") from exc

    def metadata(self, record: SurfaceRecord, *, cache_hit: bool) -> dict[str, Any]:
        return {"surface_id": record.id, "property_id": record.property_id, "mode": "walking", "status": record.status, "cache_hit": cache_hit, "computed_at": record.computed_at.isoformat() if record.computed_at else None, "duration_ms": record.duration_ms, "reachable_count": record.reachable_count, "unavailable_count": record.unavailable_count, "origin_snap_distance_metres": record.origin_snap_distance_metres, "grid": self.policy.grid_metadata(), "routing_fingerprint": record.routing_fingerprint, "network_fingerprint": record.network_fingerprint, "r5py_version": record.r5py_version, "r5_version": record.r5_version, "algorithm_version": ALGORITHM_VERSION, "value_url": f"/api/travel-time-surfaces/{record.id}/values" if record.status == "ready" else None, "validity_url": f"/api/travel-time-surfaces/{record.id}/validity" if record.status == "ready" else None}
