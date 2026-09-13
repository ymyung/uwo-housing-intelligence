"""Internal long-lived R5 walking-surface service.

This process is intentionally separate from FastAPI.  It loads one approved
R5 network at startup, accepts loopback/internal HTTP only, and exposes no
bicycle or transit calculation path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from r5_surface_poc import GRID, UNDERLYING_R5_VERSION, _surface_rows, _valid_geometry, grid_fingerprint, grid_points, sha256


class WalkingSurfaceEngine:
    def __init__(self, *, osm: Path, max_concurrency: int, request_timeout_seconds: int) -> None:
        os.environ.setdefault("R5_POC_JAVA_HEAP", "8G")
        sys.argv = [sys.argv[0], "--max-memory", os.environ["R5_POC_JAVA_HEAP"]]
        import r5py

        self.r5py = r5py
        self.network_fingerprint = sha256(osm)
        self.request_timeout_seconds = request_timeout_seconds
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        self._started = time.perf_counter()
        self.network = r5py.TransportNetwork(osm)
        self.destinations = grid_points()
        self.network_ready_seconds = round(time.perf_counter() - self._started, 3)

    def health(self) -> dict[str, object]:
        return {
            "status": "ready",
            "network_loaded": True,
            "network_fingerprint": self.network_fingerprint,
            "grid_fingerprint": grid_fingerprint(),
            "grid_cell_count": GRID["cell_count"],
            "r5py_version": self.r5py.__version__,
            "r5_version": UNDERLYING_R5_VERSION,
            "network_ready_seconds": self.network_ready_seconds,
            "capabilities": ["walking_surface"],
        }

    def compute(self, request: dict[str, object]) -> dict[str, object]:
        if request.get("mode") != "walking":
            raise ValueError("unsupported_mode: only walking numerical surfaces are accepted")
        latitude, longitude = float(request["latitude"]), float(request["longitude"])
        origin_max_snap = float(request["origin_max_snap_metres"])
        destination_max_snap = float(request["destination_max_snap_metres"])
        max_seconds = int(request["max_travel_seconds"])
        if max_seconds != 7200 or destination_max_snap != 400:
            raise ValueError("walking request does not match the accepted surface contract")
        if not self._semaphore.acquire(timeout=self.request_timeout_seconds):
            raise RuntimeError("capacity_timeout: R5 walking concurrency limit is busy")
        started = time.perf_counter()
        try:
            import geopandas as gpd
            import pandas as pd
            from shapely import Point

            origin = gpd.GeoDataFrame({"id": ["surface-origin"]}, geometry=[Point(longitude, latitude)], crs="EPSG:4326")
            snapped_origin = self.network.snap_to_network(origin.geometry, street_mode=self.r5py.TransportMode.WALK)
            if not _valid_geometry(snapped_origin.iloc[0]):
                raise ValueError("invalid_origin: origin cannot be snapped to walking network")
            distance = float(origin.geometry.to_crs(GRID["crs"]).distance(snapped_origin.to_crs(GRID["crs"])).iloc[0])
            if distance > origin_max_snap:
                raise ValueError(f"invalid_origin: snap distance {distance:.2f} m exceeds {origin_max_snap:.0f} m")
            destination_snap = self.network.snap_to_network(self.destinations.geometry, street_mode=self.r5py.TransportMode.WALK)
            valid_destination = destination_snap.map(_valid_geometry)
            destination_distances = self.destinations.geometry.to_crs(GRID["crs"])[valid_destination].distance(destination_snap.to_crs(GRID["crs"])[valid_destination])
            valid_destination = valid_destination & pd.Series(
                [float(destination_distances.get(index, float("inf"))) <= destination_max_snap for index in self.destinations.index],
                index=self.destinations.index,
            )
            matrix = self.r5py.TravelTimeMatrix(
                self.network,
                origins=origin,
                destinations=self.destinations,
                snap_to_network=True,
                max_time=timedelta(seconds=max_seconds),
                speed_walking=4.788,
                transport_modes=[self.r5py.TransportMode.WALK],
                access_modes=[self.r5py.TransportMode.WALK],
            )
            rows = _surface_rows(pd.DataFrame(matrix), self.destinations)
            rows.loc[rows["travel_time_seconds"] > max_seconds, "travel_time_seconds"] = None
            rows.loc[~valid_destination.to_numpy(), "travel_time_seconds"] = None
            values = [None if pd.isna(value) else int(value) for value in rows["travel_time_seconds"]]
            destination_validity = [bool(value) for value in valid_destination]
            return {
                "grid_fingerprint": grid_fingerprint(),
                "network_fingerprint": self.network_fingerprint,
                "r5py_version": self.r5py.__version__,
                "r5_version": UNDERLYING_R5_VERSION,
                "origin_snap_distance_metres": round(distance, 3),
                "values_seconds": values,
                "destination_validity": destination_validity,
                "reachable_count": sum(value is not None for value in values),
                "destination_snap_rejected_count": sum(not value for value in destination_validity),
                "duration_ms": round((time.perf_counter() - started) * 1000),
            }
        finally:
            self._semaphore.release()


def make_handler(engine: WalkingSurfaceEngine) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: HTTPStatus, value: dict[str, object]) -> None:
            payload = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(HTTPStatus.OK, engine.health())
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/walking-surface":
                self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError("request body is missing or too large")
                result = engine.compute(json.loads(self.rfile.read(size)))
                self._send(HTTPStatus.OK, result)
            except (KeyError, TypeError, ValueError) as exc:
                self._send(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            except RuntimeError as exc:
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            except Exception:  # pragma: no cover - process logs retain full detail
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "r5_compute_failed"})

        def log_message(self, _format: str, *_args: object) -> None:
            return
    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--max-concurrency", type=int, default=2)
    parser.add_argument("--request-timeout-seconds", type=int, default=30)
    args = parser.parse_args()
    engine = WalkingSurfaceEngine(osm=args.osm, max_concurrency=args.max_concurrency, request_timeout_seconds=args.request_timeout_seconds)
    ThreadingHTTPServer((args.host, args.port), make_handler(engine)).serve_forever()


if __name__ == "__main__":
    main()
