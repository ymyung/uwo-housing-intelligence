import json
from pathlib import Path

import pytest

from pipeline.reference_data.london.client import ArcGISClient
from pipeline.reference_data.london.datasets import DatasetSpec, load_settings
from pipeline.reference_data.london.importer import read_snapshot, save_snapshot
from pipeline.reference_data.london.normalization import normalize_address
from pipeline.reference_data.london.validation import validate_snapshot


def _spec() -> DatasetSpec:
    return DatasetSpec("municipal_addresses", "https://example.invalid/layer", "layer", "esriGeometryPoint", "OBJECTID")


def _metadata() -> dict:
    return {
        "geometryType": "esriGeometryPoint",
        "maxRecordCount": 2,
        "spatialReference": {"wkid": 26917},
        "fields": [{"name": "OBJECTID"}],
    }


def _features() -> list[dict]:
    return [
        {"attributes": {"OBJECTID": 1}, "geometry": {"x": 500000, "y": 4760000}},
        {"attributes": {"OBJECTID": 2}, "geometry": {"x": 500001, "y": 4760001}},
    ]


def test_london_reference_normalization_preserves_units_and_street_equivalence() -> None:
    assert normalize_address("1107 Sunset Street").civic_address == "1107 SUNSET ST"
    assert normalize_address("1107 sunset st.").civic_address == "1107 SUNSET ST"
    assert normalize_address("1107 Sunset Street, Unit 4").unit == "4"


def test_snapshot_validation_rejects_duplicate_source_ids() -> None:
    with pytest.raises(ValueError, match="invalid or duplicate"):
        validate_snapshot(_spec(), _metadata(), _features() + [_features()[0]])


def test_snapshot_fingerprints_are_deterministic_and_snapshot_is_readable(tmp_path: Path) -> None:
    settings = load_settings()
    settings = settings.__class__(tmp_path, tmp_path, 26917, "test", {"municipal_addresses": _spec()})
    first = validate_snapshot(_spec(), _metadata(), _features())
    assert first.content_sha256 == validate_snapshot(_spec(), _metadata(), _features()).content_sha256
    snapshot = save_snapshot(settings, _spec(), _metadata(), _features())
    assert read_snapshot(snapshot) == (_metadata(), _features())


def test_arcgis_pagination_is_ordered_and_retries_once() -> None:
    calls: list[str] = []
    payloads = [
        OSError("temporary"), _metadata(), {"count": 2},
        {"features": _features(), "exceededTransferLimit": False},
    ]
    class Response:
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(self.payload).encode()
    def opener(url, timeout):
        calls.append(url); payload = payloads.pop(0)
        if isinstance(payload, Exception): raise payload
        return Response(payload)
    client = ArcGISClient(opener=opener, retries=2)
    metadata, features = client.download(_spec())
    assert metadata["geometryType"] == "esriGeometryPoint" and len(features) == 2
    assert "orderByFields=OBJECTID+ASC" in calls[-1]
