from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.repository import InMemoryListingRepository
from backend.travel_time_surface_repository import InMemorySurfaceRepository, new_pending_surface
from backend.travel_time_surface_service import SurfaceUnavailable, UnsupportedSurfaceMode, WalkingSurfaceService
from backend.travel_time_surfaces import (
    compress_payload, decode_seconds, decode_validity_mask, decompress_payload,
    encode_seconds, encode_validity_mask, load_surface_policy, routing_fingerprint,
)


POLICY = load_surface_policy(Path("config/travel-time-surface.example.toml"))


class FakeR5:
    def __init__(self) -> None:
        self.calls = 0
        self.network = "osm-a"

    def health(self):
        return {"status": "ready", "network_fingerprint": self.network, "grid_fingerprint": POLICY.grid_fingerprint, "r5py_version": "1.1.7", "r5_version": "v7.5.1-r5py"}

    def walking_surface(self, *, latitude, longitude, policy):
        self.calls += 1
        return {"grid_fingerprint": policy.grid_fingerprint, "values_seconds": [60, None] + [120] * (policy.cell_count - 2), "destination_validity": [True, False] + [True] * (policy.cell_count - 2), "reachable_count": policy.cell_count - 1, "duration_ms": 12}


def service():
    fake = FakeR5()
    return WalkingSurfaceService(InMemorySurfaceRepository(), fake, POLICY), fake


def test_dense_uint16_gzip_and_validity_mask_round_trip() -> None:
    values = [60, None] + [120] * (POLICY.cell_count - 2)
    raw = encode_seconds(values, POLICY)
    assert len(raw) == 29606
    assert decode_seconds(decompress_payload(compress_payload(raw), POLICY), POLICY) == values
    mask = encode_validity_mask([True, False] + [True] * (POLICY.cell_count - 2), POLICY)
    assert len(mask) == 1851
    assert decode_validity_mask(mask, POLICY)[:2] == [True, False]


def test_walking_cache_identity_excludes_gtfs_but_changes_with_network_or_version() -> None:
    first = routing_fingerprint(network_fingerprint="osm-a", r5py_version="1.1.7", r5_version="v7", policy=POLICY)
    assert first == routing_fingerprint(network_fingerprint="osm-a", r5py_version="1.1.7", r5_version="v7", policy=POLICY)
    assert first != routing_fingerprint(network_fingerprint="osm-b", r5py_version="1.1.7", r5_version="v7", policy=POLICY)
    assert first != routing_fingerprint(network_fingerprint="osm-a", r5py_version="1.1.8", r5_version="v7", policy=POLICY)


def test_same_property_reuses_surface_but_other_property_does_not() -> None:
    value, fake = service()
    one, first_hit = value.get_or_compute(property_id=4, latitude=43.01, longitude=-81.25)
    two, second_hit = value.get_or_compute(property_id=4, latitude=43.01, longitude=-81.25)
    three, third_hit = value.get_or_compute(property_id=3, latitude=43.01, longitude=-81.25)
    assert one.id == two.id and not first_hit and second_hit
    assert three.id != one.id and not third_hit and fake.calls == 2


def test_duplicate_claim_is_durable_computing_not_second_owner() -> None:
    repository = InMemorySurfaceRepository()
    record = new_pending_surface(property_id=4, latitude=43.01, longitude=-81.25, cache_identity="a" * 64, routing_fingerprint="b" * 64, grid_fingerprint=POLICY.grid_fingerprint)
    assert repository.claim(record)[0] == "owned"
    assert repository.claim(record)[0] == "computing"


def test_unsupported_modes_and_unavailable_r5_are_explicit() -> None:
    value, fake = service()
    try:
        value.get_or_compute(property_id=4, latitude=43.01, longitude=-81.25, mode="bicycle")
    except UnsupportedSurfaceMode:
        pass
    else:
        raise AssertionError("bicycle must be rejected")
    fake.health = lambda: (_ for _ in ()).throw(SurfaceUnavailable("unavailable"))
    try:
        value.get_or_compute(property_id=4, latitude=43.01, longitude=-81.25)
    except SurfaceUnavailable:
        pass
    else:
        raise AssertionError("R5 unavailability must surface")


def test_api_returns_metadata_binary_and_rejects_bike_transit() -> None:
    value, fake = service()
    listings = InMemoryListingRepository([
        {"listing_id": "one", "property_id": 4, "latitude": 43.01, "longitude": -81.25, "address": "Fixture"},
        {"listing_id": "two", "property_id": 4, "latitude": 43.01, "longitude": -81.25, "address": "Fixture"},
    ])
    client = TestClient(create_app(repository=listings, walking_surface_service=value))
    grid = client.get("/api/travel-time-surfaces/grid")
    assert grid.status_code == 200 and grid.json()["cell_count"] == 14803
    metadata = client.get("/api/listings/one/travel-time-surface?mode=walking")
    assert metadata.status_code == 200 and metadata.json()["cache_hit"] is False
    repeat = client.get("/api/listings/two/travel-time-surface?mode=walking")
    assert repeat.json()["cache_hit"] is True and fake.calls == 1
    payload = client.get(metadata.json()["value_url"])
    assert payload.status_code == 200 and payload.headers["content-encoding"] == "gzip"
    # httpx transparently decodes Content-Encoding; the wire contract is
    # asserted by the header, while the received vector stays compact binary.
    assert len(payload.content) == 29606 and payload.content[:2] == b"<\x00"
    validity = client.get(metadata.json()["validity_url"])
    assert validity.status_code == 200 and len(validity.content) == 1851
    assert client.get("/api/listings/one/travel-time-surface?mode=bicycle").status_code == 422
    assert client.get("/api/listings/one/travel-time-surface?mode=transit").status_code == 422
