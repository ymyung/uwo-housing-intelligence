"""Walk-only travel-time-surface contracts, encoding, and cache identity.

This module deliberately contains no R5 import.  The Java/R5 runtime stays in
the isolated local analysis service; the API and persistence layer deal only
with validated compact artifacts.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import struct
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


UNAVAILABLE_UINT16 = 65535
ENCODING_VERSION = "uint16le-gzip-v1"
VALIDITY_MASK_VERSION = "destination-snap-bitset-v1"
ALGORITHM_VERSION = "r5-walk-surface-v1"


@dataclass(frozen=True)
class SurfacePolicy:
    grid_version: str
    crs: str
    cell_size_metres: int
    cell_count: int
    columns: int
    rows: int
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    ordering: str
    grid_fingerprint: str
    destination_snap_metres: int
    origin_snap_metres: int
    max_travel_seconds: int
    display_rounding_seconds: int

    @property
    def raw_vector_bytes(self) -> int:
        return self.cell_count * 2

    @property
    def validity_mask_bytes(self) -> int:
        return (self.cell_count + 7) // 8

    def grid_metadata(self) -> dict[str, Any]:
        return {
            "grid_version": self.grid_version,
            "crs": self.crs,
            "cell_size_metres": self.cell_size_metres,
            "rows": self.rows,
            "columns": self.columns,
            "cell_count": self.cell_count,
            "extent": {"min_x": self.min_x, "min_y": self.min_y, "max_x": self.max_x, "max_y": self.max_y},
            "ordering": self.ordering,
            "grid_fingerprint": self.grid_fingerprint,
            "value_encoding": ENCODING_VERSION,
            "unavailable_sentinel": UNAVAILABLE_UINT16,
            "validity_mask": VALIDITY_MASK_VERSION,
            "destination_snap_threshold_metres": self.destination_snap_metres,
            "display_rounding_seconds": self.display_rounding_seconds,
        }


def load_surface_policy(path: Path) -> SurfacePolicy:
    """Load and validate the reviewed walk-only contract configuration."""
    with path.open("rb") as source:
        data = tomllib.load(source)
    if data["contract"]["classification"] != "SURFACE_CONTRACT_READY_WITH_MODE_LIMITATIONS":
        raise ValueError("walking surface contract is not accepted")
    if data["mode"]["walk"].get("enabled") is not True:
        raise ValueError("walking surfaces are not enabled")
    if data["mode"]["bicycle"].get("enabled") or data["mode"]["transit"].get("enabled"):
        raise ValueError("surface policy must not enable bicycle or transit")
    grid, surface, origin, walk = data["grid"], data["surface"], data["origin"], data["mode"]["walk"]
    policy = SurfacePolicy(
        grid_version=str(grid["version"]), crs=str(grid["crs"]), cell_size_metres=int(grid["cell_size_metres"]),
        cell_count=int(grid["cell_count"]), columns=int(grid["columns"]), rows=int(grid["rows"]),
        min_x=float(grid["min_x"]), min_y=float(grid["min_y"]), max_x=float(grid["max_x"]), max_y=float(grid["max_y"]),
        ordering=str(grid["ordering"]), grid_fingerprint=str(grid["grid_sha256"]),
        destination_snap_metres=int(walk["destination_snap_threshold_metres"]),
        origin_snap_metres=int(origin["maximum_snap_distance_metres"]),
        max_travel_seconds=int(surface["max_travel_minutes"]) * 60,
        display_rounding_seconds=int(surface["display_rounding_seconds"]),
    )
    if policy.rows * policy.columns != policy.cell_count or policy.cell_count != 14803:
        raise ValueError("surface grid is not the accepted 14,803-cell grid")
    if policy.destination_snap_metres != 400 or policy.max_travel_seconds != 7200:
        raise ValueError("surface policy differs from the accepted walking contract")
    return policy


def encode_seconds(values: list[int | None], policy: SurfacePolicy) -> bytes:
    if len(values) != policy.cell_count:
        raise ValueError("surface vector has the wrong grid length")
    output = bytearray(policy.raw_vector_bytes)
    for index, value in enumerate(values):
        if value is None:
            struct.pack_into("<H", output, index * 2, UNAVAILABLE_UINT16)
            continue
        seconds = int(value)
        if not 0 <= seconds <= policy.max_travel_seconds:
            raise ValueError("surface value is outside the accepted walking range")
        struct.pack_into("<H", output, index * 2, seconds)
    return bytes(output)


def decode_seconds(payload: bytes, policy: SurfacePolicy) -> list[int | None]:
    if len(payload) != policy.raw_vector_bytes:
        raise ValueError("surface payload has the wrong uncompressed length")
    values = struct.unpack(f"<{policy.cell_count}H", payload)
    result: list[int | None] = []
    for value in values:
        if value == UNAVAILABLE_UINT16:
            result.append(None)
        elif value <= policy.max_travel_seconds:
            result.append(value)
        else:
            raise ValueError("surface payload contains an invalid reserved value")
    return result


def compress_payload(raw: bytes) -> bytes:
    return gzip.compress(raw, mtime=0)


def decompress_payload(payload: bytes, policy: SurfacePolicy) -> bytes:
    try:
        raw = gzip.decompress(payload)
    except OSError as exc:
        raise ValueError("surface payload is not valid gzip") from exc
    if len(raw) != policy.raw_vector_bytes:
        raise ValueError("surface compressed payload expands to the wrong length")
    return raw


def encode_validity_mask(valid_destinations: list[bool], policy: SurfacePolicy) -> bytes:
    if len(valid_destinations) != policy.cell_count:
        raise ValueError("validity mask has the wrong grid length")
    mask = bytearray(policy.validity_mask_bytes)
    for index, valid in enumerate(valid_destinations):
        if valid:
            mask[index // 8] |= 1 << (index % 8)
    return bytes(mask)


def decode_validity_mask(payload: bytes, policy: SurfacePolicy) -> list[bool]:
    if len(payload) != policy.validity_mask_bytes:
        raise ValueError("validity mask has the wrong length")
    return [bool(payload[index // 8] & (1 << (index % 8))) for index in range(policy.cell_count)]


def routing_fingerprint(*, network_fingerprint: str, r5py_version: str, r5_version: str, policy: SurfacePolicy) -> str:
    """Fingerprint walking inputs only; GTFS is intentionally absent."""
    data = {
        "algorithm": ALGORITHM_VERSION,
        "mode": "walking",
        "network_fingerprint": network_fingerprint,
        "r5py_version": r5py_version,
        "r5_version": r5_version,
        "grid_fingerprint": policy.grid_fingerprint,
        "destination_snap_metres": policy.destination_snap_metres,
        "origin_snap_metres": policy.origin_snap_metres,
        "max_travel_seconds": policy.max_travel_seconds,
        "speed_walking_kph": 4.788,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cache_identity(*, property_id: int, latitude: float, longitude: float, routing_fingerprint_value: str, policy: SurfacePolicy) -> str:
    data = {
        "canonical_property_id": property_id,
        "origin_latitude": f"{latitude:.8f}",
        "origin_longitude": f"{longitude:.8f}",
        "routing_fingerprint": routing_fingerprint_value,
        "grid_fingerprint": policy.grid_fingerprint,
        "algorithm": ALGORITHM_VERSION,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
