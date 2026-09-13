"""Generate deterministic R5 inputs for the travel-time-surface acceptance study.

This is an extension of the isolated POC, not a production worker.  It runs
inside the existing R5 container, reads mounted routing inputs, and writes
ignored acceptance artifacts.  OTP comparisons run separately on the host so
the two engines do not compete for memory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from r5_surface_poc import (
    GRID,
    ORIGINS,
    UNDERLYING_R5_VERSION,
    _origins,
    _valid_geometry,
    grid_cell_centroid,
    grid_fingerprint,
    grid_points,
    sha256,
)


SNAP_THRESHOLDS_METRES = (100, 150, 200, 250, 400, 600)
SAMPLE_SIZE = 72
TRANSIT_SAMPLE_SIZE = 36
MAX_TRAVEL_MINUTES = 120
R5_GTFS_PREPROCESSING_VERSION = "r5_gtfs_preprocess_v1"

# These definitions deliberately mirror backend.accessibility_periods.  The
# numerical-map window spans the existing three representative OTP departures.
TRANSIT_PERIODS = (
    {
        "id": "weekday_morning_commute",
        "label": "Weekday morning",
        "service_date": "2026-06-15",
        "window_start": "07:30",
        "window_minutes": 60,
        "existing_otp_departures": ("07:30", "08:00", "08:30"),
    },
    {
        "id": "weekday_midday",
        "label": "Weekday midday",
        "service_date": "2026-06-15",
        "window_start": "11:30",
        "window_minutes": 60,
        "existing_otp_departures": ("11:30", "12:00", "12:30"),
    },
    {
        "id": "weekday_evening_commute",
        "label": "Weekday evening",
        "service_date": "2026-06-15",
        "window_start": "16:30",
        "window_minutes": 60,
        "existing_otp_departures": ("16:30", "17:00", "17:30"),
    },
    {
        "id": "weekday_late_evening",
        "label": "Weekday late evening",
        "service_date": "2026-06-15",
        "window_start": "21:30",
        "window_minutes": 60,
        "existing_otp_departures": ("21:30", "22:00", "22:30"),
    },
    {
        "id": "saturday_daytime",
        "label": "Saturday daytime",
        "service_date": "2026-06-20",
        "window_start": "11:30",
        "window_minutes": 60,
        "existing_otp_departures": ("11:30", "12:00", "12:30"),
    },
    {
        "id": "sunday_daytime",
        "label": "Sunday daytime",
        "service_date": "2026-06-21",
        "window_start": "11:30",
        "window_minutes": 60,
        "existing_otp_departures": ("11:30", "12:00", "12:30"),
    },
)

# Known locations deliberately cover the requested geographic and road-context
# categories.  The actual deterministic grid cell is the nearest centroid.
ANCHORS = (
    ("western_campus", 43.0096, -81.2737),
    ("downtown", 42.9849, -81.2453),
    ("north_london", 43.0470, -81.2670),
    ("south_london", 42.9120, -81.2490),
    ("east_london", 42.9810, -81.1510),
    ("west_london", 42.9810, -81.3500),
    ("major_road_richmond", 43.0050, -81.2700),
    ("major_road_wonderland", 42.9900, -81.3100),
    ("major_road_highbury", 42.9500, -81.2200),
    ("edge_northwest", 43.0600, -81.3800),
    ("edge_northeast", 43.0600, -81.1200),
    ("edge_southwest", 42.8400, -81.3800),
    ("edge_southeast", 42.8400, -81.1200),
    ("sparse_northwest", 43.0400, -81.3500),
    ("sparse_southeast", 42.8900, -81.1600),
)
SNAP_BANDS = ((0, 100), (100, 150), (150, 200), (200, 250), (250, 400), (400, 600), (600, 1601))


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _period_departure(specification: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(
        f"{specification['service_date']}T{specification['window_start']}:00"
    )


def _spread(values: list[int], count: int) -> list[int]:
    if not values:
        return []
    if len(values) <= count:
        return values
    return [values[round(index * (len(values) - 1) / (count - 1))] for index in range(count)]


def _nearest_cell(frame: Any, latitude: float, longitude: float) -> int:
    distances = (frame.geometry.y - latitude) ** 2 + (frame.geometry.x - longitude) ** 2
    return int(frame.loc[distances.idxmin(), "grid_cell_id"])


def _snap_frame(network: Any, frame: Any, *, street_mode: Any) -> Any:
    """Return coordinates and projected distances for one R5 street mode."""
    import pandas as pd

    snapped = network.snap_to_network(frame.geometry, street_mode=street_mode)
    valid = snapped.map(_valid_geometry)
    original_projected = frame.geometry.to_crs(GRID["crs"])
    snapped_projected = snapped.to_crs(GRID["crs"])
    distances = original_projected[valid].distance(snapped_projected[valid])
    return pd.DataFrame(
        {
            "grid_cell_id": frame["grid_cell_id"].astype(int),
            "snapped": valid.astype(bool),
            "snap_latitude": [point.y if _valid_geometry(point) else None for point in snapped],
            "snap_longitude": [point.x if _valid_geometry(point) else None for point in snapped],
            "snap_distance_metres": [
                float(distances.loc[index]) if index in distances.index else None
                for index in frame.index
            ],
        }
    )


def _selected_sample(grid: Any, walk_snap: Any, bicycle_snap: Any) -> tuple[Any, set[int]]:
    """Create a stable 72-cell set with geography and snap-distance coverage."""
    import pandas as pd

    base = grid[["grid_cell_id", "geometry"]].copy()
    base["latitude"] = base.geometry.y
    base["longitude"] = base.geometry.x
    base["x"], base["y"] = zip(
        *(grid_cell_centroid(int(value)) for value in base["grid_cell_id"])
    )
    base = base.merge(
        walk_snap.rename(
            columns={
                "snapped": "walk_snapped",
                "snap_latitude": "walk_snapped_latitude",
                "snap_longitude": "walk_snapped_longitude",
                "snap_distance_metres": "walk_snap_distance_metres",
            }
        ),
        on="grid_cell_id",
    ).merge(
        bicycle_snap.rename(
            columns={
                "snapped": "bicycle_snapped",
                "snap_latitude": "bicycle_snapped_latitude",
                "snap_longitude": "bicycle_snapped_longitude",
                "snap_distance_metres": "bicycle_snap_distance_metres",
            }
        ),
        on="grid_cell_id",
    )
    tags: dict[int, set[str]] = defaultdict(set)
    anchor_ids: list[int] = []
    for label, latitude, longitude in ANCHORS:
        cell_id = _nearest_cell(base, latitude, longitude)
        anchor_ids.append(cell_id)
        tags[cell_id].add(label)
    selected: list[int] = list(dict.fromkeys(anchor_ids))
    for lower, upper in SNAP_BANDS:
        candidates = sorted(
            int(value)
            for value in base.loc[
                base["walk_snap_distance_metres"].between(lower, upper, inclusive="left"),
                "grid_cell_id",
            ]
        )
        for cell_id in _spread(candidates, 8):
            if cell_id not in selected:
                selected.append(cell_id)
            tags[cell_id].add(f"walk_snap_{lower}_{upper}m")
    low_snap = sorted(
        int(value)
        for value in base.loc[
            base["walk_snap_distance_metres"].le(100), "grid_cell_id"
        ]
    )
    for cell_id in _spread(low_snap, SAMPLE_SIZE * 2):
        if len(selected) >= SAMPLE_SIZE:
            break
        if cell_id not in selected:
            selected.append(cell_id)
            tags[cell_id].add("coverage_fill_low_snap")
    if len(selected) < SAMPLE_SIZE:
        for cell_id in sorted(base["grid_cell_id"].astype(int)):
            if len(selected) >= SAMPLE_SIZE:
                break
            if cell_id not in selected:
                selected.append(cell_id)
                tags[cell_id].add("coverage_fill")
    selected = selected[:SAMPLE_SIZE]
    selected_frame = base[base["grid_cell_id"].isin(selected)].copy()
    selected_frame["selection_tags"] = selected_frame["grid_cell_id"].map(
        lambda value: ";".join(sorted(tags[int(value)]))
    )
    selected_frame["id"] = selected_frame["grid_cell_id"].map(
        lambda value: f"acceptance-grid-{int(value)}"
    )

    transit_ids: list[int] = [
        cell_id for cell_id in selected if any(cell_id == anchor_id for anchor_id in anchor_ids)
    ]
    for lower, upper in SNAP_BANDS:
        candidates = sorted(
            int(value)
            for value in selected_frame.loc[
                selected_frame["walk_snap_distance_metres"].between(lower, upper, inclusive="left"),
                "grid_cell_id",
            ]
        )
        for cell_id in _spread(candidates, 3):
            if cell_id not in transit_ids:
                transit_ids.append(cell_id)
    for cell_id in _spread(selected, TRANSIT_SAMPLE_SIZE * 2):
        if len(transit_ids) >= TRANSIT_SAMPLE_SIZE:
            break
        if cell_id not in transit_ids:
            transit_ids.append(cell_id)
    transit_ids = set(transit_ids[:TRANSIT_SAMPLE_SIZE])
    selected_frame["in_transit_study"] = selected_frame["grid_cell_id"].isin(transit_ids)
    selected_frame = selected_frame.sort_values("grid_cell_id").reset_index(drop=True)
    assert len(selected_frame) == SAMPLE_SIZE
    return selected_frame, transit_ids


def _matrix_rows(
    network: Any,
    r5py: Any,
    origins: Any,
    destinations: Any,
    *,
    mode: str,
    period: dict[str, Any] | None,
) -> Any:
    import numpy as np
    import pandas as pd

    kwargs: dict[str, Any] = {
        "origins": origins,
        "destinations": destinations,
        "snap_to_network": True,
        "max_time": timedelta(minutes=MAX_TRAVEL_MINUTES),
        "speed_walking": 4.788,
        "speed_cycling": 15.012,
    }
    if mode == "walk":
        kwargs.update(
            transport_modes=[r5py.TransportMode.WALK],
            access_modes=[r5py.TransportMode.WALK],
        )
    elif mode == "bicycle":
        kwargs.update(
            transport_modes=[r5py.TransportMode.BICYCLE],
            access_modes=[r5py.TransportMode.BICYCLE],
            max_bicycle_traffic_stress=4,
        )
    elif mode == "transit" and period is not None:
        kwargs.update(
            transport_modes=[r5py.TransportMode.TRANSIT],
            access_modes=[r5py.TransportMode.WALK],
            egress_modes=[r5py.TransportMode.WALK],
            departure=_period_departure(period),
            departure_time_window=timedelta(minutes=int(period["window_minutes"])),
            percentiles=[50],
        )
    else:
        raise ValueError("Transit requires a period specification")
    matrix = r5py.TravelTimeMatrix(network, **kwargs)
    result = pd.DataFrame(matrix)[["from_id", "to_id", "travel_time"]].copy()
    result["origin_property_id"] = result["from_id"].str.removeprefix("property-").astype(int)
    result["grid_cell_id"] = result["to_id"].str.removeprefix("acceptance-grid-").astype(int)
    result["r5_travel_time_seconds"] = np.rint(
        result["travel_time"].astype(float) * 60
    ).astype("Int64")
    result.loc[
        result["r5_travel_time_seconds"] > MAX_TRAVEL_MINUTES * 60,
        "r5_travel_time_seconds",
    ] = None
    result["mode"] = mode
    result["period_id"] = period["id"] if period else "direct"
    return result.drop(columns=["from_id", "to_id", "travel_time"])


def _gtfs_rows(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    import csv

    with archive.open(name) as source:
        return list(csv.DictReader((line.decode("utf-8-sig") for line in source)))


def gtfs_preprocessing_audit(source: Path, derived: Path) -> dict[str, object]:
    """Prove the derived feed preserves schedule meaning except blank transfer defaults."""
    with zipfile.ZipFile(source) as source_zip, zipfile.ZipFile(derived) as derived_zip:
        source_trips = _gtfs_rows(source_zip, "trips.txt")
        source_times = _gtfs_rows(source_zip, "stop_times.txt")
        source_transfers = _gtfs_rows(source_zip, "transfers.txt")
        derived_trips = _gtfs_rows(derived_zip, "trips.txt")
        derived_times = _gtfs_rows(derived_zip, "stop_times.txt")
        derived_transfers = _gtfs_rows(derived_zip, "transfers.txt")
        late = [
            row
            for row in source_times
            if any((row.get(key) or "00:00:00") >= "24:00:00" for key in ("arrival_time", "departure_time"))
        ]
        trip_service = {row["trip_id"]: row.get("service_id") for row in source_trips}
        trip_route = {row["trip_id"]: row.get("route_id") for row in source_trips}
        late_trip_ids = sorted({row["trip_id"] for row in late})
        late_service_ids = sorted({trip_service[value] for value in late_trip_ids if trip_service.get(value)})
        late_route_ids = sorted({trip_route[value] for value in late_trip_ids if trip_route.get(value)})
        transfer_changes = [
            index
            for index, (before, after) in enumerate(zip(source_transfers, derived_transfers), start=2)
            if (before.get("transfer_type") or "").strip() != (after.get("transfer_type") or "").strip()
        ]
        expected_changes = [
            index
            for index, row in enumerate(source_transfers, start=2)
            if not (row.get("transfer_type") or "").strip()
        ]
        late_columns = ("trip_id", "arrival_time", "departure_time", "stop_sequence")
        late_preserved = all(
            tuple(before.get(column) for column in late_columns)
            == tuple(after.get(column) for column in late_columns)
            for before, after in zip(source_times, derived_times)
            if any((before.get(key) or "00:00:00") >= "24:00:00" for key in ("arrival_time", "departure_time"))
        )
    return {
        "version": R5_GTFS_PREPROCESSING_VERSION,
        "source_sha256": sha256(source),
        "derived_sha256": sha256(derived),
        "canonical_gtfs_modified": False,
        "transform": "Blank transfers.txt transfer_type values become explicit GTFS default 0; all other schedule fields remain semantically unchanged.",
        "blank_transfer_type_rows": expected_changes,
        "observed_transfer_type_rows_changed": transfer_changes,
        "blank_transfer_default_verified": transfer_changes == expected_changes,
        "trip_rows_preserved": len(source_trips) == len(derived_trips) and source_trips == derived_trips,
        "stop_time_rows_preserved": len(source_times) == len(derived_times),
        "late_stop_times_at_or_after_24": len(late),
        "late_clock_times_preserved": late_preserved,
        "late_affected_trip_ids": late_trip_ids,
        "late_affected_route_ids": late_route_ids,
        "late_affected_service_ids": late_service_ids,
        "late_evening_validation_policy": "R5 retains extended GTFS clock times unchanged and is compared with OTP for weekday late-evening 21:30-22:30. No modulo-24 or service-date rewrite is permitted.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm", type=Path, required=True)
    parser.add_argument("--gtfs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    args, _unknown = parser.parse_known_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("XDG_CACHE_HOME", str(args.cache_dir))
    sys.argv = [sys.argv[0], "--max-memory", os.environ.get("R5_POC_JAVA_HEAP", "8G")]
    import r5py

    started = time.perf_counter()
    network = r5py.TransportNetwork(args.osm, [args.gtfs])
    network_load_seconds = round(time.perf_counter() - started, 3)
    grid = grid_points()
    walk_snap = _snap_frame(network, grid, street_mode=r5py.TransportMode.WALK)
    bicycle_snap = _snap_frame(network, grid, street_mode=r5py.TransportMode.BICYCLE)
    sample, transit_ids = _selected_sample(grid, walk_snap, bicycle_snap)
    sample.to_csv(args.output / "validation-sample.csv", index=False)

    grid_snap_summary: dict[str, object] = {}
    for mode, frame in (("walk", walk_snap), ("bicycle", bicycle_snap), ("transit", walk_snap)):
        grid_snap_summary[mode] = {
            "total_cells": len(frame),
            "snapped_cells": int(frame["snapped"].sum()),
            "distance_metres": {
                "median": round(float(frame["snap_distance_metres"].median()), 2),
                "p90": round(float(frame["snap_distance_metres"].quantile(0.9)), 2),
                "max": round(float(frame["snap_distance_metres"].max()), 2),
            },
            "threshold_coverage": {
                str(threshold): {
                    "cells": int(frame["snap_distance_metres"].le(threshold).sum()),
                    "percent": round(float(frame["snap_distance_metres"].le(threshold).mean()) * 100, 3),
                }
                for threshold in SNAP_THRESHOLDS_METRES
            },
        }
    _write_json(args.output / "grid-snap-summary.json", grid_snap_summary)

    origins = _origins()
    origin_snap = {
        "walk": _snap_frame(network, origins.rename(columns={"property_id": "grid_cell_id"}), street_mode=r5py.TransportMode.WALK),
        "bicycle": _snap_frame(network, origins.rename(columns={"property_id": "grid_cell_id"}), street_mode=r5py.TransportMode.BICYCLE),
    }
    _write_json(
        args.output / "origin-snap-summary.json",
        {
            mode: [
                {
                    "property_id": int(row.grid_cell_id),
                    "snapped": bool(row.snapped),
                    "latitude": float(origins.loc[origins.property_id == row.grid_cell_id].geometry.iloc[0].y),
                    "longitude": float(origins.loc[origins.property_id == row.grid_cell_id].geometry.iloc[0].x),
                    "snapped_latitude": row.snap_latitude,
                    "snapped_longitude": row.snap_longitude,
                    "snap_distance_metres": row.snap_distance_metres,
                }
                for row in frame.itertuples(index=False)
            ]
            for mode, frame in origin_snap.items()
        },
    )

    destinations = sample[["id", "grid_cell_id", "geometry"]].copy()
    result_frames = []
    matrix_seconds: dict[str, float] = {}
    for mode in ("walk", "bicycle"):
        matrix_started = time.perf_counter()
        result_frames.append(_matrix_rows(network, r5py, origins, destinations, mode=mode, period=None))
        matrix_seconds[mode] = round(time.perf_counter() - matrix_started, 3)
    for period in TRANSIT_PERIODS:
        matrix_started = time.perf_counter()
        result_frames.append(
            _matrix_rows(network, r5py, origins, destinations, mode="transit", period=period)
        )
        matrix_seconds[f"transit:{period['id']}"] = round(time.perf_counter() - matrix_started, 3)
    import pandas as pd

    results = pd.concat(result_frames, ignore_index=True).merge(
        sample.drop(columns=["geometry"]), on="grid_cell_id", how="left"
    )
    results["r5_snap_distance_metres"] = results.apply(
        lambda row: row["bicycle_snap_distance_metres"]
        if row["mode"] == "bicycle"
        else row["walk_snap_distance_metres"],
        axis=1,
    )
    results["r5_snapped_latitude"] = results.apply(
        lambda row: row["bicycle_snapped_latitude"]
        if row["mode"] == "bicycle"
        else row["walk_snapped_latitude"],
        axis=1,
    )
    results["r5_snapped_longitude"] = results.apply(
        lambda row: row["bicycle_snapped_longitude"]
        if row["mode"] == "bicycle"
        else row["walk_snapped_longitude"],
        axis=1,
    )
    results = results.drop(columns=[
        "walk_snapped", "walk_snapped_latitude", "walk_snapped_longitude", "walk_snap_distance_metres",
        "bicycle_snapped", "bicycle_snapped_latitude", "bicycle_snapped_longitude", "bicycle_snap_distance_metres",
    ])
    results.to_csv(args.output / "r5-acceptance-results.csv", index=False)

    _write_json(args.output / "gtfs-r5-preprocessing-audit.json", gtfs_preprocessing_audit(
        Path("/inputs/london-transit.gtfs.zip"), args.gtfs
    ))
    _write_json(
        args.output / "r5-acceptance-run.json",
        {
            "r5py_version": r5py.__version__,
            "underlying_r5_version": UNDERLYING_R5_VERSION,
            "network_load_seconds": network_load_seconds,
            "matrix_seconds": matrix_seconds,
            "grid_sha256": grid_fingerprint(),
            "osm_sha256": sha256(args.osm),
            "canonical_osm_sha256": sha256(Path("/inputs/ontario.osm.pbf")),
            "gtfs_sha256": sha256(args.gtfs),
            "canonical_gtfs_sha256": sha256(Path("/inputs/london-transit.gtfs.zip")),
            "validation_cells": len(sample),
            "transit_validation_cells": len(transit_ids),
            "origins": [
                {"property_id": value[0], "latitude": value[1], "longitude": value[2], "role": value[3]}
                for value in ORIGINS
            ],
            "transit_periods": TRANSIT_PERIODS,
            "max_travel_minutes": MAX_TRAVEL_MINUTES,
        },
    )
    print(json.dumps({"validation_cells": len(sample), "transit_cells": len(transit_ids), "r5_rows": len(results)}, indent=2))


if __name__ == "__main__":
    main()
