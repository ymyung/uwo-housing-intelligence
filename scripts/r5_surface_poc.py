"""Isolated R5/r5py travel-time-surface proof of concept (not production code).

The script intentionally keeps the analysis engine separate from the API and
ranking code.  It only reads its OSM/GTFS inputs and writes ignored benchmark
artifacts.  It is executed inside ``docker-compose.r5-poc.yml`` so Java is
never a requirement of the Windows development host.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:  # ``resource`` is unavailable on Windows, where focused utility tests run.
    import resource
except ImportError:  # pragma: no cover - exercised only outside the container
    resource = None  # type: ignore[assignment]


ORIGINS = (
    (4, 43.010161008339615, -81.25833282208825, "near_western"),
    (3, 42.99120169375, -81.24700425, "medium_distance"),
    (658, 42.953913, -81.190658, "far_less_transit_dense"),
)
GRID = {
    "crs": "EPSG:26917",
    "min_x": 468327.3781,
    "min_y": 4742068.8695,
    "max_x": 490903.1505,
    "max_y": 4768198.7722,
    "cell_metres": 200,
    "columns": 113,
    "rows": 131,
    "cell_count": 14803,
}
BENCHMARK_DATE = datetime(2026, 6, 15, 8, 0)
UNDERLYING_R5_VERSION = "v7.5.1-r5py"
UNREACHABLE_UINT16 = 65535
UINT16_MAX_SECONDS = UNREACHABLE_UINT16 - 1
DERIVED_OSM_EXTRACTION = {
    "tool": "osmium extract",
    "strategy": "complete_ways",
    "bbox_wgs84": "-81.43000,42.80000,-81.07000,43.10000",
    "purpose": "Buffered local London experiment; canonical Ontario PBF remains unchanged.",
}


def sha256(path: Path) -> str:
    """Return an exact file fingerprint without loading the entire file."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_gtfs_rows(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    if name not in archive.namelist():
        return []
    with archive.open(name) as source:
        return list(csv.DictReader((line.decode("utf-8-sig") for line in source)))


def gtfs_audit(path: Path) -> dict[str, object]:
    """Audit schedule facts before passing a feed to R5.

    Extended clock times are deliberately reported, not repaired.  The 08:00
    weekday experiment does not use those late-night clock values, but a
    future all-day importer needs an explicit preprocessing policy.
    """
    with zipfile.ZipFile(path) as archive:
        stops = _read_gtfs_rows(archive, "stops.txt")
        trips = _read_gtfs_rows(archive, "trips.txt")
        routes = _read_gtfs_rows(archive, "routes.txt")
        stop_times = _read_gtfs_rows(archive, "stop_times.txt")
        calendars = _read_gtfs_rows(archive, "calendar.txt")
        dates = [
            value
            for item in calendars
            for value in (item.get("start_date"), item.get("end_date"))
            if value
        ]
        late = [
            item
            for item in stop_times
            if any(
                (item.get(key) or "00:00:00") >= "24:00:00"
                for key in ("arrival_time", "departure_time")
            )
        ]
        maximum = max(
            (
                max(
                    item.get("arrival_time") or "00:00:00",
                    item.get("departure_time") or "00:00:00",
                )
                for item in stop_times
            ),
            default=None,
        )
        trip_routes = {item.get("trip_id"): item.get("route_id") for item in trips}
        late_trip_ids = sorted({item.get("trip_id") for item in late if item.get("trip_id")})
        late_route_ids = sorted(
            {trip_routes[trip_id] for trip_id in late_trip_ids if trip_routes.get(trip_id)}
        )
        reference_date = BENCHMARK_DATE.strftime("%Y%m%d")
        monday_service_ids = sorted(
            {
                item.get("service_id")
                for item in calendars
                if item.get("monday") == "1"
                and (item.get("start_date") or "99999999") <= reference_date
                and (item.get("end_date") or "00000000") >= reference_date
                and item.get("service_id")
            }
        )
    return {
        "sha256": sha256(path),
        "stops": len(stops),
        "trips": len(trips),
        "routes": len(routes),
        "stop_times": len(stop_times),
        "calendar_date_range": [min(dates), max(dates)] if dates else None,
        "reference_weekday_morning": BENCHMARK_DATE.isoformat(),
        "reference_date_in_calendar_range": bool(
            dates and min(dates) <= reference_date <= max(dates)
        ),
        "monday_service_ids_enabled": monday_service_ids,
        "times_at_or_after_24": len(late),
        "maximum_clock_time": maximum,
        "affected_trip_ids": late_trip_ids,
        "affected_route_ids": late_route_ids,
        "weekday_morning_clock_time_affected": False,
        "note": (
            "Extended GTFS times are after midnight. They are outside the 08:00 "
            "weekday benchmark clock time but remain a compatibility risk for "
            "all-day analysis."
        ),
    }


def make_r5_compatible_gtfs(source: Path, destination: Path) -> dict[str, object]:
    """Create a deterministic, ignored test feed for R5's strict transfer parser.

    The GTFS specification's default transfer type is 0.  R5 rejects blank
    cells, so only those cells are made explicit.  No service times, trips, or
    source archives are altered.
    """
    changed = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as input_zip, zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as output_zip:
        for name in sorted(input_zip.namelist()):
            payload = input_zip.read(name)
            if name == "transfers.txt":
                text = payload.decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(text))
                fields = list(reader.fieldnames or [])
                if "transfer_type" not in fields:
                    fields.append("transfer_type")
                transformed = io.StringIO(newline="")
                writer = csv.DictWriter(
                    transformed, fieldnames=fields, lineterminator="\n"
                )
                writer.writeheader()
                for row in reader:
                    if not (row.get("transfer_type") or "").strip():
                        row["transfer_type"] = "0"
                        changed += 1
                    writer.writerow({field: row.get(field, "") for field in fields})
                payload = transformed.getvalue().encode("utf-8")
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            output_zip.writestr(entry, payload, compress_type=zipfile.ZIP_DEFLATED)
    return {
        "source_gtfs": str(source),
        "source_sha256": sha256(source),
        "derived_gtfs": str(destination),
        "derived_sha256": sha256(destination),
        "transform": "Blank transfers.txt transfer_type values set explicitly to GTFS default 0",
        "modified_transfer_rows": changed,
        "source_preserved": True,
    }


def grid_fingerprint() -> str:
    return hashlib.sha256(
        json.dumps(GRID, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def grid_cell_centroid(cell_id: int) -> tuple[float, float]:
    """Return a grid centroid in EPSG:26917 for its stable row-major id."""
    if not 0 <= cell_id < GRID["cell_count"]:
        raise ValueError("grid_cell_id is outside the deterministic benchmark grid")
    row, column = divmod(cell_id, GRID["columns"])
    return (
        GRID["min_x"] + (column + 0.5) * GRID["cell_metres"],
        GRID["min_y"] + (row + 0.5) * GRID["cell_metres"],
    )


def grid_points():
    """Return deterministic grid centroids in R5's WGS84 input CRS."""
    import geopandas as gpd
    from shapely import points

    coordinates: list[tuple[float, float]] = []
    identifiers: list[int] = []
    for cell_id in range(GRID["cell_count"]):
        identifiers.append(cell_id)
        coordinates.append(grid_cell_centroid(cell_id))
    frame = gpd.GeoDataFrame(
        {
            "grid_cell_id": identifiers,
            # R5 assigns zero to an OD pair with equal ids.  Grid ids must not
            # collide with housing-property origin ids even though both are
            # otherwise stable integer domains.
            "id": [f"grid-{value}" for value in identifiers],
        },
        geometry=points(
            [item[0] for item in coordinates], [item[1] for item in coordinates]
        ),
        crs=GRID["crs"],
    )
    return frame.to_crs(4326)


def _cache_inventory(cache_dir: Path) -> dict[str, int]:
    return {
        str(path.relative_to(cache_dir)).replace("\\", "/"): path.stat().st_size
        for path in sorted(cache_dir.rglob("*"))
        if path.is_file()
    }


def _java_version() -> str:
    result = subprocess.run(
        ["java", "-version"], capture_output=True, text=True, check=False
    )
    output = (result.stderr or result.stdout).strip()
    if result.returncode:
        raise RuntimeError(f"Container Java is unavailable: {output}")
    return output


class MemorySampler:
    """Best-effort container cgroup peak sampler for an ignored benchmark."""

    def __init__(self) -> None:
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @staticmethod
    def _current_bytes() -> int:
        for path in (Path("/sys/fs/cgroup/memory.current"),):
            try:
                return int(path.read_text().strip())
            except (FileNotFoundError, ValueError):
                pass
        # Linux ru_maxrss is KiB; this fallback is process-only rather than
        # complete cgroup memory, but it remains useful if cgroups are absent.
        if resource is None:
            return 0
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak_bytes = max(self.peak_bytes, self._current_bytes())
            self._stop.wait(0.05)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> int:
        self._stop.set()
        self._thread.join(timeout=1)
        self.peak_bytes = max(self.peak_bytes, self._current_bytes())
        return self.peak_bytes


def _origins():
    import geopandas as gpd
    from shapely import Point

    return gpd.GeoDataFrame(
        {
            "id": [f"property-{item[0]}" for item in ORIGINS],
            "property_id": [item[0] for item in ORIGINS],
            "role": [item[3] for item in ORIGINS],
        },
        geometry=[Point(item[2], item[1]) for item in ORIGINS],
        crs="EPSG:4326",
    )


def _valid_geometry(value: Any) -> bool:
    return value is not None and getattr(value, "is_empty", True) is False


def snap_summary(
    network: Any, points: Any, *, street_mode: Any, detail_limit: int | None = None
) -> dict[str, object]:
    """Measure R5 snap coverage and distances without treating a miss as a route."""
    snapped = network.snap_to_network(points.geometry, street_mode=street_mode)
    valid = snapped.map(_valid_geometry)
    original = points.geometry.to_crs(GRID["crs"])
    snapped_projected = snapped.to_crs(GRID["crs"])
    distances = original[valid].distance(snapped_projected[valid])
    details = []
    for count, (index, row) in enumerate(points.iterrows()):
        if detail_limit is not None and count >= detail_limit:
            break
        geometry = snapped.loc[index]
        details.append(
            {
                "id": str(row["id"]),
                "property_id": (
                    int(row["property_id"]) if "property_id" in row else None
                ),
                "original": [round(row.geometry.y, 8), round(row.geometry.x, 8)],
                "snapped": (
                    [round(geometry.y, 8), round(geometry.x, 8)]
                    if _valid_geometry(geometry)
                    else None
                ),
                "distance_metres": (
                    round(float(distances.loc[index]), 2) if index in distances.index else None
                ),
            }
        )
    return {
        "attempted": len(points),
        "snapped": int(valid.sum()),
        "unsnapped": int((~valid).sum()),
        "distance_metres": {
            "median": round(float(distances.median()), 2) if not distances.empty else None,
            "p90": round(float(distances.quantile(0.9)), 2) if not distances.empty else None,
            "max": round(float(distances.max()), 2) if not distances.empty else None,
        },
        "detail_sample_count": len(details),
        "details": details,
    }


def encode_surface_seconds(rows: Any) -> bytes:
    """Encode a full ordered grid as uint16 seconds; 65535 means unreachable."""
    import numpy as np

    values = np.full(GRID["cell_count"], UNREACHABLE_UINT16, dtype="<u2")
    import pandas as pd

    for cell_id, value in zip(rows["grid_cell_id"], rows["travel_time_seconds"]):
        if value is None or pd.isna(value):
            continue
        seconds = int(value)
        if not 0 <= seconds <= UINT16_MAX_SECONDS:
            raise ValueError("Travel time cannot be represented by uint16 seconds")
        values[int(cell_id)] = seconds
    return values.tobytes()


def _surface_rows(matrix: Any, destinations: Any) -> Any:
    """Join R5 results back to all deterministic cells and convert minutes to seconds."""
    import numpy as np
    import pandas as pd

    result = matrix[["to_id", "travel_time"]].copy()
    result["grid_cell_id"] = result["to_id"].str.removeprefix("grid-").astype(int)
    result["travel_time_seconds"] = (
        np.rint(result["travel_time"].astype(float) * 60).astype("Int64")
    )
    joined = pd.DataFrame({"grid_cell_id": destinations["grid_cell_id"]}).merge(
        result[["grid_cell_id", "travel_time_seconds"]], how="left", on="grid_cell_id"
    )
    joined["travel_time_seconds"] = joined["travel_time_seconds"].astype("Int64")
    return joined


def _spread(values: list[int], count: int) -> list[int]:
    if not values:
        return []
    if len(values) <= count:
        return values
    return [values[round(index * (len(values) - 1) / (count - 1))] for index in range(count)]


def _validation_points(
    rows: Any, destinations: Any, network: Any, *, include_unreachable: bool
) -> list[dict[str, object]]:
    """Choose deterministic, on-network comparison points plus an edge case.

    Direct-mode parity only compares cells within 100 m of R5's network snap.
    Transit retains an edge and an R5-unreachable cell deliberately, because
    those are useful reachability diagnostics rather than numerical parity.
    """
    lookup = destinations.set_index("grid_cell_id")
    snapped = network.snap_to_network(lookup.geometry)
    snap_distances = lookup.geometry.to_crs(GRID["crs"]).distance(
        snapped.to_crs(GRID["crs"])
    )
    reachable = rows[rows["travel_time_seconds"].notna()]["grid_cell_id"].astype(int).tolist()
    near_network = sorted(
        cell_id for cell_id in reachable if float(snap_distances.loc[cell_id]) <= 100
    )
    selected: list[int] = _spread(near_network or reachable, 8 if include_unreachable else 10)
    roles = {cell_id: "representative_on_network" for cell_id in selected}
    if include_unreachable:
        edge_candidates = [cell_id for cell_id in reachable if cell_id not in selected]
        if edge_candidates:
            edge = max(edge_candidates, key=lambda cell_id: float(snap_distances.loc[cell_id]))
            selected.append(edge)
            roles[edge] = "edge_snap_diagnostic"
        unreachable = rows[rows["travel_time_seconds"].isna()]["grid_cell_id"].tolist()
        if unreachable:
            cell_id = int(unreachable[0])
            selected.append(cell_id)
            roles[cell_id] = "r5_unreachable_diagnostic"
    records = []
    seconds = rows.set_index("grid_cell_id")["travel_time_seconds"]
    for cell_id in selected:
        point = lookup.loc[cell_id].geometry
        value = seconds.loc[cell_id]
        import pandas as pd

        records.append(
            {
                "grid_cell_id": int(cell_id),
                "latitude": round(float(point.y), 8),
                "longitude": round(float(point.x), 8),
                "r5_travel_time_seconds": int(value) if not pd.isna(value) else None,
                "r5_snap_distance_metres": round(float(snap_distances.loc[cell_id]), 2),
                "selection_role": roles[cell_id],
            }
        )
    return records


def run_surface(
    network: Any,
    *,
    r5py: Any,
    mode: str,
    origin: Any,
    destinations: Any,
    output: Path,
    departure_window: timedelta | None = None,
    max_travel_minutes: int = 120,
    suffix: str = "",
) -> tuple[dict[str, object], Any]:
    """Compute one origin-to-grid surface with explicitly aligned direct speeds."""
    kwargs: dict[str, object] = {
        "origins": origin,
        "destinations": destinations,
        "snap_to_network": True,
        "max_time": timedelta(minutes=max_travel_minutes),
        "speed_walking": 4.788,  # OTP router-config.json: 1.33 m/s
        "speed_cycling": 15.012,  # OTP router-config.json: 4.17 m/s
    }
    if mode == "walk":
        kwargs.update(
            {
                "transport_modes": [r5py.TransportMode.WALK],
                "access_modes": [r5py.TransportMode.WALK],
            }
        )
    elif mode == "bicycle":
        kwargs.update(
            {
                "transport_modes": [r5py.TransportMode.BICYCLE],
                # OTP's direct BICYCLE mode does not add a walking alternative.
                "access_modes": [r5py.TransportMode.BICYCLE],
                # R5's default is 3; OTP does not apply this R5-specific
                # traffic-stress cutoff, so 4 is the closest available match.
                "max_bicycle_traffic_stress": 4,
            }
        )
    elif mode == "transit":
        kwargs.update(
            {
                "transport_modes": [r5py.TransportMode.TRANSIT],
                "access_modes": [r5py.TransportMode.WALK],
                "egress_modes": [r5py.TransportMode.WALK],
                "departure": BENCHMARK_DATE,
                "departure_time_window": departure_window,
                "percentiles": [50],
            }
        )
    else:
        raise ValueError(f"Unsupported benchmark mode: {mode}")
    started = time.perf_counter()
    matrix = r5py.TravelTimeMatrix(network, **kwargs)
    elapsed = time.perf_counter() - started
    rows = _surface_rows(matrix, destinations)
    # R5 can include final street legs beyond its internal request cutoff.
    # Preserve a strict external surface contract for the experimental vector.
    rows.loc[
        rows["travel_time_seconds"] > max_travel_minutes * 60,
        "travel_time_seconds",
    ] = None
    encoded = encode_surface_seconds(rows)
    compressed = gzip.compress(encoded, mtime=0)
    property_id = int(origin.iloc[0]["property_id"])
    filename = f"r5-surface-{mode}-property-{property_id}{suffix}.u16.gz"
    (output / filename).write_bytes(compressed)
    reachable = int(rows["travel_time_seconds"].notna().sum())
    return (
        {
            "mode": mode,
            "origin_property_id": property_id,
            "origin_role": str(origin.iloc[0]["role"]),
            "departure": BENCHMARK_DATE.isoformat() if mode == "transit" else None,
            "departure_window_minutes": (
                round(departure_window.total_seconds() / 60, 2)
                if departure_window is not None
                else None
            ),
            "percentile": 50 if mode == "transit" else None,
            "travel_time_unit_from_r5": "minutes",
            "duration_seconds": round(elapsed, 3),
            "output_rows": len(rows),
            "reachable": reachable,
            "unreachable": len(rows) - reachable,
            "vector_filename": filename,
            "uint16_sentinel": UNREACHABLE_UINT16,
            "uncompressed_bytes": len(encoded),
            "gzip_bytes": len(compressed),
            "max_reachable_seconds": (
                int(rows["travel_time_seconds"].dropna().max()) if reachable else None
            ),
        },
        rows,
    )


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_report(output: Path) -> None:
    """Write a concise local artifact even when later OTP validation is pending."""
    network = json.loads((output / "r5-network-build.json").read_text())
    benchmark_path = output / "r5-surface-benchmark.json"
    benchmark = json.loads(benchmark_path.read_text()) if benchmark_path.exists() else {}
    audit = json.loads((output / "r5-gtfs-audit.json").read_text())
    report_path = output / "r5-poc-report.md"
    existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    validation_suffix = (
        "\n## Exact OTP validation\n" + existing.split("\n## Exact OTP validation\n", 1)[1]
        if "\n## Exact OTP validation\n" in existing
        else ""
    )
    lines = [
        "# R5/r5py local proof of concept",
        "",
        "This ignored artifact is analysis-only. OTP remains the exact point-to-point router.",
        "",
        f"- Network load/build: {network.get('network_load_seconds')} seconds",
        f"- Peak observed cgroup memory: {network.get('peak_memory_bytes')} bytes",
        f"- Grid: {GRID['cell_count']} deterministic EPSG:26917 200 m centroids",
        f"- GTFS extended times: {audit['times_at_or_after_24']} through {audit['maximum_clock_time']}",
        "- R5-only GTFS transform: blank `transfers.txt.transfer_type` values were explicitly set to 0; canonical feed preserved.",
    ]
    for record in benchmark.get("surfaces", []):
        lines.append(
            f"- {record['mode']} property {record['origin_property_id']}: "
            f"{record['duration_seconds']} s, {record['reachable']} reachable"
        )
    report_path.write_text(
        "\n".join(lines) + "\n" + validation_suffix, encoding="utf-8"
    )


def _environment(args: argparse.Namespace, *, r5py: Any) -> dict[str, object]:
    return {
        "r5py_version": r5py.__version__,
        "underlying_r5_version": UNDERLYING_R5_VERSION,
        "python_version": sys.version,
        "platform": platform.platform(),
        "java_version": _java_version(),
        "java_heap_cap": os.environ.get("R5_POC_JAVA_HEAP"),
        "container_memory_limit": os.environ.get("R5_POC_CONTAINER_MEMORY_LIMIT", "10g"),
        "osm_sha256": sha256(args.osm),
        "source_osm_sha256": sha256(args.source_osm) if args.source_osm else None,
        "derived_osm_extraction": DERIVED_OSM_EXTRACTION,
        "gtfs_sha256": sha256(args.gtfs),
        "grid_sha256": grid_fingerprint(),
        "script_sha256": sha256(Path(__file__)),
        "config": {
            "max_travel_minutes": args.max_travel_minutes,
            "walk_speed_kmh": 4.788,
            "bicycle_speed_kmh": 15.012,
            "bicycle_traffic_stress": 4,
            "transit_short_window_minutes": args.transit_short_window_minutes,
            "transit_period_window_minutes": args.transit_period_window_minutes,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm", type=Path, required=True)
    parser.add_argument("--gtfs", type=Path, required=True)
    parser.add_argument("--source-osm", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument("--prepare-compatible-gtfs", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--warm-network-only", action="store_true")
    parser.add_argument("--max-travel-minutes", type=int, default=120)
    parser.add_argument("--transit-short-window-minutes", type=int, default=10)
    parser.add_argument("--transit-period-window-minutes", type=int, default=60)
    args, _unknown = parser.parse_known_args()
    if args.max_travel_minutes <= 0 or args.max_travel_minutes * 60 > UINT16_MAX_SECONDS:
        parser.error("--max-travel-minutes must fit below the uint16 unreachable sentinel")
    if args.transit_short_window_minutes < 5 or args.transit_period_window_minutes < 5:
        parser.error("R5 transit windows must be at least five minutes")
    args.output.mkdir(parents=True, exist_ok=True)
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    if args.prepare_compatible_gtfs:
        destination = args.output / "london-transit-r5-compatible.gtfs.zip"
        compatibility = make_r5_compatible_gtfs(args.gtfs, destination)
        _write_json(args.output / "r5-gtfs-compatibility.json", compatibility)
        print(json.dumps(compatibility, indent=2))
        return

    # r5py parses process arguments on import. Give it only the bounded heap
    # option and set its cache root before importing it.
    heap = os.environ.get("R5_POC_JAVA_HEAP", "8G")
    os.environ.setdefault("XDG_CACHE_HOME", str(args.cache_dir))
    sys.argv = [sys.argv[0], "--max-memory", heap]
    import r5py

    environment = _environment(args, r5py=r5py)
    _write_json(args.output / "r5-environment.json", environment)
    audit = gtfs_audit(args.gtfs)
    _write_json(args.output / "r5-gtfs-audit.json", audit)
    destinations = grid_points()
    assert len(destinations) == GRID["cell_count"] and destinations.grid_cell_id.is_unique
    assert destinations.grid_cell_id.tolist() == list(range(GRID["cell_count"]))
    if args.inspect_only or not (args.execute or args.warm_network_only):
        print(
            json.dumps(
                {
                    **environment,
                    "transport_network_api": str(r5py.TransportNetwork),
                    "grid_rows": len(destinations),
                },
                indent=2,
            )
        )
        return

    before_cache = _cache_inventory(args.cache_dir)
    memory = MemorySampler()
    memory.start()
    started = time.perf_counter()
    network = r5py.TransportNetwork(args.osm, [args.gtfs])
    network_seconds = time.perf_counter() - started
    peak_memory = memory.stop()
    after_cache = _cache_inventory(args.cache_dir)
    cache_added = {
        key: value for key, value in after_cache.items() if before_cache.get(key) != value
    }
    network_record = {
        "network_load_seconds": round(network_seconds, 3),
        "mode": "warm_process_load" if args.warm_network_only else "cold_or_cache_miss_build",
        "peak_memory_bytes": peak_memory,
        "cache_before_bytes": sum(before_cache.values()),
        "cache_after_bytes": sum(after_cache.values()),
        "cache_changed_files": cache_added,
        "osm": {"path": str(args.osm), "sha256": sha256(args.osm)},
        "gtfs": {"path": str(args.gtfs), "sha256": sha256(args.gtfs)},
        "network_extent": str(network.extent),
        "r5py_version": r5py.__version__,
        "underlying_r5_version": UNDERLYING_R5_VERSION,
    }
    network_path = args.output / "r5-network-build.json"
    if args.warm_network_only and network_path.exists():
        saved = json.loads(network_path.read_text(encoding="utf-8"))
        saved["warm_process_load"] = network_record
        _write_json(network_path, saved)
        write_report(args.output)
        print(json.dumps(network_record, indent=2))
        return
    if network_path.exists():
        saved = json.loads(network_path.read_text(encoding="utf-8"))
        saved["cached_execution_network_load"] = network_record
        _write_json(network_path, saved)
    else:
        _write_json(network_path, network_record)

    all_origins = _origins()
    origin_snap = snap_summary(network, all_origins, street_mode=r5py.TransportMode.WALK)
    grid_snap = snap_summary(
        network, destinations, street_mode=r5py.TransportMode.WALK, detail_limit=20
    )
    surfaces: list[dict[str, object]] = []
    validation: dict[str, object] = {"origin_property_id": 3, "surfaces": {}}
    for mode in ("walk", "bicycle"):
        for _, origin_row in all_origins.iterrows():
            origin = all_origins[all_origins.id == origin_row.id]
            record, rows = run_surface(
                network, r5py=r5py, mode=mode, origin=origin,
                destinations=destinations, output=args.output,
                max_travel_minutes=args.max_travel_minutes,
            )
            surfaces.append(record)
            if int(origin_row.property_id) == 3:
                validation["surfaces"][mode] = _validation_points(
                    rows, destinations, network, include_unreachable=False
                )
        # A same-process repeat is the relevant lazy-surface warm timing.
        origin = all_origins[all_origins.id == "property-3"]
        warm, _ = run_surface(
            network, r5py=r5py, mode=mode, origin=origin, destinations=destinations,
            output=args.output, suffix="-warm-repeat",
            max_travel_minutes=args.max_travel_minutes,
        )
        warm["same_process_warm_repeat"] = True
        surfaces.append(warm)
    short_window = timedelta(minutes=args.transit_short_window_minutes)
    for _, origin_row in all_origins.iterrows():
        origin = all_origins[all_origins.id == origin_row.id]
        record, rows = run_surface(
            network, r5py=r5py, mode="transit", origin=origin,
            destinations=destinations, output=args.output, departure_window=short_window,
            suffix="-short-window", max_travel_minutes=args.max_travel_minutes,
        )
        surfaces.append(record)
        if int(origin_row.property_id) == 3:
            validation["surfaces"]["transit_short_window"] = _validation_points(
                rows, destinations, network, include_unreachable=True
            )
    origin = all_origins[all_origins.id == "property-3"]
    transit_warm, _ = run_surface(
        network, r5py=r5py, mode="transit", origin=origin, destinations=destinations,
        output=args.output, departure_window=short_window, suffix="-short-window-warm-repeat",
        max_travel_minutes=args.max_travel_minutes,
    )
    transit_warm["same_process_warm_repeat"] = True
    surfaces.append(transit_warm)
    period, _ = run_surface(
        network, r5py=r5py, mode="transit", origin=origin, destinations=destinations,
        output=args.output,
        departure_window=timedelta(minutes=args.transit_period_window_minutes),
        suffix="-period-window", max_travel_minutes=args.max_travel_minutes,
    )
    period["period_representative_window"] = True
    surfaces.append(period)
    validation["surfaces"]["transit_period_window"] = _validation_points(
        _, destinations, network, include_unreachable=True
    )

    benchmark = {
        "grid": GRID,
        "grid_sha256": grid_fingerprint(),
        "origins": [
            {"property_id": item[0], "latitude": item[1], "longitude": item[2], "role": item[3]}
            for item in ORIGINS
        ],
        "origin_snap": origin_snap,
        "grid_snap": grid_snap,
        "surfaces": surfaces,
        "transit_semantics": {
            "departure": BENCHMARK_DATE.isoformat(),
            "short_window_minutes": args.transit_short_window_minutes,
            "period_window_minutes": args.transit_period_window_minutes,
            "percentile": 50,
            "note": "R5 median travel time across the declared departure window; not equivalent to one arbitrary OTP departure.",
        },
    }
    _write_json(args.output / "r5-surface-benchmark.json", benchmark)
    _write_json(args.output / "r5-validation-points.json", validation)
    _write_json(
        args.output / "r5-storage-test.json",
        {
            "cell_count": GRID["cell_count"],
            "encoding": "little-endian uint16 seconds in grid_cell_id order",
            "unreachable_sentinel": UNREACHABLE_UINT16,
            "maximum_representable_seconds": UINT16_MAX_SECONDS,
            "surface_files": [
                {
                    "vector_filename": record["vector_filename"],
                    "uncompressed_bytes": record["uncompressed_bytes"],
                    "gzip_bytes": record["gzip_bytes"],
                }
                for record in surfaces
            ],
        },
    )
    write_report(args.output)
    print(json.dumps({"network": network_record, "surface_count": len(surfaces)}, indent=2))


if __name__ == "__main__":
    main()
