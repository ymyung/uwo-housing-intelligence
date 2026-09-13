"""Local-only travel-time-surface engine decision benchmark.

This is deliberately not a production provider or cache.  It records the
capabilities of the active OTP build and a bounded normal-routing baseline;
it never writes PostgreSQL or calls a remote routing service.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import psycopg
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.domain import Coordinates, Location, RouteRequest, TravelMode
from backend.routing_provider import OpenTripPlannerProvider, RoutingGraphMetadata


OUTPUT_ROOT = PROJECT_ROOT / "data" / "travel-time-surface-validation"
NATIVE_SRID = 26917
CELL_METRES = 200
TRANSIT_DEPARTURE = datetime(2026, 6, 15, 8, tzinfo=ZoneInfo("America/Toronto"))


class TravelTimeSurfaceBenchmarkProvider(Protocol):
    """Minimal benchmark-only shape; deliberately not a production API."""

    name: str

    def capability(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class Origin:
    property_id: int
    latitude: float
    longitude: float
    role: str

    @property
    def coordinates(self) -> Coordinates:
        return Coordinates(self.latitude, self.longitude)


class OtpAnalysisProbe:
    name = "otp_2_6_sandbox_travel_time"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def capability(self) -> dict[str, object]:
        path = "/otp/traveltime/surface?location=43.0096,-81.2737&time=2026-06-15T08:00:00Z&modes=WALK&cutoff=PT60M"
        started = time.perf_counter()
        try:
            response = requests.get(self.base_url + path, timeout=20)
            return {"available": response.status_code == 200, "status_code": response.status_code, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}
        except requests.RequestException as error:
            return {"available": False, "error": type(error).__name__, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}


class RepeatedOtpBaseline:
    name = "otp_2_6_repeated_exact_baseline"

    def __init__(self, base_url: str, metadata: RoutingGraphMetadata) -> None:
        self.provider = OpenTripPlannerProvider(base_url=base_url, router_id="default", timeout_seconds=30, metadata=metadata)

    def capability(self) -> dict[str, object]:
        return {"available": True, "one_to_many": False, "purpose": "bounded scaling baseline only"}

    def route(self, origin: Origin, destination: Coordinates, mode: TravelMode) -> int | None:
        if mode is TravelMode.TRANSIT:
            sample = self.provider.get_samples(
                Location("origin", "Benchmark origin", origin.coordinates),
                Location("cell", "Benchmark grid cell", destination),
                [TRANSIT_DEPARTURE],
            )[0]
            return sample.duration_seconds
        return self.provider.get_route(RouteRequest(origin.coordinates, destination, mode)).duration_seconds


def _database_url(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} to the approved local PostgreSQL URL")
    if "127.0.0.1" not in value and "localhost" not in value:
        raise RuntimeError("Benchmark database must be local")
    return value


def _origins(connection) -> tuple[Origin, ...]:
    wanted = ((4, "near_western"), (3, "medium_distance"), (658, "far_less_transit_dense"))
    output: list[Origin] = []
    for property_id, role in wanted:
        row = connection.execute("""select id, latitude, longitude from public.housing_properties
            where id=%s and address_complete and latitude is not null and longitude is not null and geocode_confidence >= 0.9""", (property_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"Trusted benchmark property {property_id} is unavailable")
        output.append(Origin(int(row[0]), float(row[1]), float(row[2]), role))
    return tuple(output)


def _grid(connection, cell_metres: int) -> tuple[dict[str, float], list[tuple[float, float]]]:
    row = connection.execute("""select st_xmin(box), st_ymin(box), st_xmax(box), st_ymax(box)
      from (select st_extent(geometry)::box2d box from reference_data.municipal_addresses a
      join reference_data.dataset_runs r on r.id=a.dataset_run_id where r.is_current) q""").fetchone()
    min_x, min_y, max_x, max_y = map(float, row)
    columns = math.ceil((max_x - min_x) / cell_metres)
    rows = math.ceil((max_y - min_y) / cell_metres)
    cells = [(min_x + (column + .5) * cell_metres, min_y + (row_index + .5) * cell_metres) for row_index in range(rows) for column in range(columns)]
    return {"min_x": min_x, "min_y": min_y, "max_x": max_x, "max_y": max_y, "columns": columns, "rows": rows, "cell_count": len(cells), "cell_metres": cell_metres, "crs": "EPSG:26917"}, cells


def _sample_coordinates(connection, cells: list[tuple[float, float]], sample_size: int) -> list[Coordinates]:
    selected = [cells[round(index * (len(cells) - 1) / (sample_size - 1))] for index in range(sample_size)]
    rows = connection.execute("""select st_y(st_transform(st_setsrid(st_makepoint(x,y),26917),4326)),
      st_x(st_transform(st_setsrid(st_makepoint(x,y),26917),4326))
      from unnest(%s::double precision[], %s::double precision[]) as points(x,y)""", ([point[0] for point in selected], [point[1] for point in selected])).fetchall()
    return [Coordinates(float(latitude), float(longitude)) for latitude, longitude in rows]


def _benchmark(baseline: RepeatedOtpBaseline, origin: Origin, destinations: list[Coordinates], full_grid_cells: int) -> tuple[dict[str, object], list[dict[str, object]]]:
    results: dict[str, object] = {}
    records: list[dict[str, object]] = []
    for mode in (TravelMode.WALKING, TravelMode.CYCLING, TravelMode.TRANSIT):
        elapsed: list[float] = []
        reachable = 0
        for index, destination in enumerate(destinations):
            started = time.perf_counter()
            try:
                seconds = baseline.route(origin, destination, mode)
            except Exception as error:  # A no-route/OTP failure is a baseline outcome, not a benchmark crash.
                seconds, error_name = None, type(error).__name__
            else:
                error_name = None
            elapsed.append((time.perf_counter() - started) * 1000)
            reachable += seconds is not None
            records.append({"engine": baseline.name, "mode": mode.value, "origin_property_id": origin.property_id, "destination_index": index, "surface_seconds": None, "exact_otp_seconds": seconds, "absolute_error_minutes": None, "percentage_error": None, "error": error_name})
        results[mode.value] = {"cold_ms": round(elapsed[0], 1), "warm_median_ms": round(statistics.median(elapsed[1:]), 1), "warm_p90_ms": round(statistics.quantiles(elapsed[1:], n=10)[8], 1), "requests": len(destinations), "reachable": reachable, "unreachable": len(destinations) - reachable, "raw_output_bytes": len(json.dumps(records[-len(destinations):], separators=(",", ":")).encode()), "full_grid_extrapolated_seconds": round(statistics.median(elapsed[1:]) * full_grid_cells / 1000, 1), "accuracy": "not_applicable: repeated OTP is the exact comparator, not a surface"}
    return results, records


def _storage(grid: dict[str, float]) -> dict[str, object]:
    cells = int(grid["cell_count"])
    # Compact arrays use a fixed grid order.  Index+value is shown for sparse generic storage.
    representations = {"uint16_seconds_dense": cells * 2, "uint32_seconds_dense": cells * 4, "uint32_index_uint16_seconds_sparse": cells * 6}
    return {"cell_count": cells, "representations_bytes": representations, "estimated_compressed_binary_bytes": {key: round(value * .45) for key, value in representations.items()}, "surfaces": {str(count): {key: value * count for key, value in representations.items()} for count in (1, 500, 3000)}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-env", default="ACCESSIBILITY_DATABASE_URL")
    parser.add_argument("--otp-base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--sample-size", type=int, default=50)
    args = parser.parse_args()
    if args.sample_size < 10:
        parser.error("--sample-size must be at least 10")
    manifest = json.loads((PROJECT_ROOT / "data" / "routing" / "build-manifest.json").read_text(encoding="utf-8"))
    metadata = RoutingGraphMetadata(manifest["router_version"], manifest["network_version"], manifest["schedule_version"], datetime.fromisoformat(manifest["graph_built_at"]))
    with psycopg.connect(_database_url(args.database_url_env)) as connection:
        origins = _origins(connection)
        grid, cells = _grid(connection, CELL_METRES)
        sample = _sample_coordinates(connection, cells, args.sample_size)
    analysis = OtpAnalysisProbe(args.otp_base_url).capability()
    baseline = RepeatedOtpBaseline(args.otp_base_url, metadata)
    timing, records = _benchmark(baseline, origins[1], sample, int(grid["cell_count"]))
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    engine = {"otp": {"image": "opentripplanner/opentripplanner:2.6.0", "metadata": manifest, "analysis_probe": analysis}, "r5": {"available": False, "reason": "No local Java runtime or R5 executable is installed; not added as a production dependency."}, "repeated_otp_baseline": {"origin": origins[1].__dict__, "capability": baseline.capability(), "timings": timing}}
    (OUTPUT_ROOT / "engine-benchmark.json").write_text(json.dumps(engine, indent=2) + "\n", encoding="utf-8")
    (OUTPUT_ROOT / "grid-benchmark.json").write_text(json.dumps({"grid": grid, "origins": [origin.__dict__ for origin in origins], "alternative_resolution_cell_counts": {str(size): math.ceil((grid["max_x"]-grid["min_x"])/size)*math.ceil((grid["max_y"]-grid["min_y"])/size) for size in (100,200,250)}}, indent=2) + "\n", encoding="utf-8")
    (OUTPUT_ROOT / "storage-estimate.json").write_text(json.dumps(_storage(grid), indent=2) + "\n", encoding="utf-8")
    with (OUTPUT_ROOT / "accuracy-comparison.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=records[0].keys()); writer.writeheader(); writer.writerows(records)
    report = f"""# Local travel-time surface benchmark\n\nOTP is `{manifest['router_version']}`. Its sandbox travel-time surface endpoint returned `{analysis.get('status_code')}`; it is not a candidate in this configured build. R5 was not run because this host has no Java runtime or R5 executable.\n\nThe EPSG:26917 City-address extent is {grid['columns']} x {grid['rows']} cells at 200 m ({grid['cell_count']} cells). The only timed engine is a {args.sample_size}-destination sequential exact-OTP baseline for trusted property {origins[1].property_id}; it proves scaling only and is not a surface. Surface-vs-exact accuracy is therefore not applicable.\n\nRecommendation: use an isolated, containerized R5 proof-of-concept using the existing OSM PBF and London Transit GTFS, retain OTP for exact route details, and do not enable the unconfigured OTP sandbox API as a production dependency.\n"""
    (OUTPUT_ROOT / "benchmark-report.md").write_text(report, encoding="utf-8")
    print(json.dumps({"output_root": str(OUTPUT_ROOT), "grid_cells": grid["cell_count"], "timings": timing}, indent=2))


if __name__ == "__main__":
    main()
