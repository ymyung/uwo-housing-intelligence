from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from backend.location_visibility import apply_public_location, public_location
from pipeline.location_visibility import load_location_visibility
from scripts.triage_mvp_reviews import classify_location_visibility


def _write_visibility(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "property_id": 1,
        "policy_version": "location-visibility-v1",
        "location_status": "available",
        "map_visible": "true",
        "route_available": "true",
        "reason_codes": "[]",
        "source_run_id": "fixture-run",
        "source_fingerprint": "a" * 64,
    }
    row.update(updates)
    return row


def test_public_contract_uses_explicit_decision_and_redacts_unavailable_coordinates() -> None:
    source = {
        "latitude": 43.01,
        "longitude": -81.27,
        "map_ready": True,
        "location_status": "unavailable",
        "location_map_visible": False,
        "location_route_available": False,
        "location_reason_codes": ["city_reference_disagreement"],
    }

    assert public_location(source)["route_available"] is False
    projected = apply_public_location(source)
    assert projected["latitude"] is None
    assert projected["longitude"] is None
    assert "location_reason_codes" not in projected


def test_invalid_explicit_contract_fails_closed() -> None:
    location = public_location(
        {
            "latitude": 43.01,
            "longitude": -81.27,
            "location_status": "available",
            "location_map_visible": True,
            "location_route_available": False,
        }
    )
    assert location["status"] == "unavailable"
    assert location["map_visible"] is False


def test_visibility_csv_loader_validates_status_flags_and_identity(tmp_path: Path) -> None:
    path = tmp_path / "location-visibility.csv"
    _write_visibility(
        path,
        [
            _row(),
            _row(
                property_id=2,
                location_status="limited",
                map_visible="true",
                route_available="false",
                reason_codes=json.dumps(["canonical_map_not_ready"]),
            ),
        ],
    )

    rows = load_location_visibility(path)

    assert len(rows) == 2
    assert rows[1].reason_codes == ("canonical_map_not_ready",)

    _write_visibility(
        path,
        [_row(location_status="unavailable", map_visible="true")],
    )
    with pytest.raises(ValueError, match="visibility flags"):
        load_location_visibility(path)


def test_triage_location_policy_contains_uncertainty_without_changing_coordinates() -> None:
    row = {"latitude": "43.01", "longitude": "-81.27", "map_ready": "true"}
    category, reasons = classify_location_visibility(
        row,
        {
            "city_evidence": "SIGNIFICANT_CITY_DISAGREEMENT",
            "geocode_result_type": "building",
        },
        city_distance_meters=300,
        city_significant=True,
    )
    assert category == "exclude_from_map_demo"
    assert "city_reference_disagreement" in reasons
    assert row["latitude"] == "43.01"
