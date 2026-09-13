"""Focused offline tests for the isolated R5 proof-of-concept utilities."""
from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pandas as pd
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "r5_surface_poc.py"
SPEC = importlib.util.spec_from_file_location("r5_surface_poc", SCRIPT)
assert SPEC and SPEC.loader
r5_surface_poc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r5_surface_poc)
VALIDATE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "r5_validate_otp.py"
VALIDATE_SPEC = importlib.util.spec_from_file_location("r5_validate_otp", VALIDATE_SCRIPT)
assert VALIDATE_SPEC and VALIDATE_SPEC.loader
r5_validate_otp = importlib.util.module_from_spec(VALIDATE_SPEC)
VALIDATE_SPEC.loader.exec_module(r5_validate_otp)
ACCEPTANCE_VALIDATE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "r5_acceptance_validate_otp.py"
ACCEPTANCE_VALIDATE_SPEC = importlib.util.spec_from_file_location(
    "r5_acceptance_validate_otp", ACCEPTANCE_VALIDATE_SCRIPT
)
assert ACCEPTANCE_VALIDATE_SPEC and ACCEPTANCE_VALIDATE_SPEC.loader
r5_acceptance_validate_otp = importlib.util.module_from_spec(ACCEPTANCE_VALIDATE_SPEC)
ACCEPTANCE_VALIDATE_SPEC.loader.exec_module(r5_acceptance_validate_otp)


def test_r5_surface_grid_is_exact_and_row_major() -> None:
    assert r5_surface_poc.GRID["columns"] * r5_surface_poc.GRID["rows"] == 14803
    assert r5_surface_poc.grid_cell_centroid(0) == pytest.approx((468427.3781, 4742168.8695))
    assert r5_surface_poc.grid_cell_centroid(112) == pytest.approx((490827.3781, 4742168.8695))
    assert r5_surface_poc.grid_cell_centroid(113) == pytest.approx((468427.3781, 4742368.8695))
    assert r5_surface_poc.grid_fingerprint() == "483932591b94921bdcaa0ae159997e89fc5f2cedd824f97bcfe36260131e9e76"


def test_r5_surface_uint16_encoding_keeps_order_and_unreachable_sentinel() -> None:
    rows = pd.DataFrame(
        {
            "grid_cell_id": [0, 2, 14802],
            "travel_time_seconds": [120, None, 3600],
        }
    )
    payload = r5_surface_poc.encode_surface_seconds(rows)
    assert len(payload) == 14803 * 2
    values = memoryview(payload).cast("H")
    assert values[0] == 120
    assert values[1] == r5_surface_poc.UNREACHABLE_UINT16
    assert values[2] == r5_surface_poc.UNREACHABLE_UINT16
    assert values[14802] == 3600


def test_r5_surface_rejects_cell_ids_outside_the_grid() -> None:
    with pytest.raises(ValueError, match="outside"):
        r5_surface_poc.grid_cell_centroid(14803)


def test_r5_otp_validation_summary_distinguishes_reachability() -> None:
    result = r5_validate_otp.summary(
        [
            {"grid_cell_id": 1, "r5_travel_time_seconds": 600, "otp_statistic_seconds": 720, "absolute_difference_minutes": 2},
            {"grid_cell_id": 2, "r5_travel_time_seconds": None, "otp_statistic_seconds": None, "absolute_difference_minutes": None},
            {"grid_cell_id": 3, "r5_travel_time_seconds": 600, "otp_statistic_seconds": None, "absolute_difference_minutes": None},
            {"grid_cell_id": 4, "r5_travel_time_seconds": None, "otp_statistic_seconds": 600, "absolute_difference_minutes": None},
        ]
    )
    assert {key: result[key] for key in ("reachable_both", "unreachable_both", "r5_only", "otp_only")} == {"reachable_both": 1, "unreachable_both": 1, "r5_only": 1, "otp_only": 1}
    assert result["absolute_error_minutes"]["median"] == 2


def test_acceptance_transit_window_covers_the_full_hour_at_fixed_intervals() -> None:
    departures = r5_acceptance_validate_otp._departure_window(
        {"service_date": "2026-06-15", "window_start": "07:30", "window_minutes": 60}
    )
    assert [value.strftime("%H:%M") for value in departures] == [
        "07:30", "07:40", "07:50", "08:00", "08:10", "08:20", "08:30"
    ]


def test_acceptance_threshold_analysis_filters_by_snap_distance_and_keeps_reachability() -> None:
    records = [
        {
            "mode": "walk", "r5_snap_distance_metres": 80, "r5_travel_time_seconds": 600,
            "otp_statistic_seconds": 720, "absolute_difference_minutes": 2,
        },
        {
            "mode": "walk", "r5_snap_distance_metres": 250, "r5_travel_time_seconds": 600,
            "otp_statistic_seconds": None, "absolute_difference_minutes": None,
        },
    ]
    grid_snaps = {
        mode: {"threshold_coverage": {str(value): {"cells": 10, "percent": 50} for value in (100, 150, 200, 250, 400, 600)}}
        for mode in ("walk", "bicycle", "transit")
    }
    result = r5_acceptance_validate_otp.threshold_rows(records, grid_snaps)
    walk_100 = next(row for row in result if row["mode"] == "walk" and row["snap_threshold_metres"] == 100)
    walk_250 = next(row for row in result if row["mode"] == "walk" and row["snap_threshold_metres"] == 250)
    assert walk_100["validation_rows"] == 1
    assert walk_100["reachable_both"] == 1
    assert walk_250["validation_rows"] == 2
    assert walk_250["r5_only"] == 1


def test_acceptance_caps_exact_otp_routes_at_the_r5_surface_limit() -> None:
    row = {
        "mode": "walk", "period_id": "direct", "origin_property_id": "3", "grid_cell_id": "1",
        "latitude": "42.99", "longitude": "-81.24", "selection_tags": "test",
        "in_transit_study": "false", "r5_travel_time_seconds": "", "r5_snap_distance_metres": "10",
    }
    record = r5_acceptance_validate_otp._make_record(
        row, otp_seconds=7201, raw_otp_statistic_seconds=7201, otp_error=None
    )
    assert record["otp_raw_statistic_seconds"] == 7201
    assert record["otp_statistic_seconds"] is None
    assert record["otp_exceeds_surface_cap"] is True


def test_walk_only_surface_contract_preserves_disabled_modes() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config" / "travel-time-surface.example.toml"
    with config_path.open("rb") as source:
        policy = tomllib.load(source)
    assert policy["contract"]["classification"] == "SURFACE_CONTRACT_READY_WITH_MODE_LIMITATIONS"
    assert policy["mode"]["walk"]["enabled"] is True
    assert policy["mode"]["walk"]["destination_snap_threshold_metres"] == 400
    assert policy["mode"]["bicycle"]["enabled"] is False
    assert policy["mode"]["transit"]["enabled"] is False
