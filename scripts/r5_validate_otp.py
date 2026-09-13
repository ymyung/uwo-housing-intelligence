"""Compare ignored R5 POC surfaces with local exact OTP routing.

This is a bounded validation harness, not a provider and not a production
worker.  It reads the deterministic R5 validation points emitted by
``r5_surface_poc.py`` and writes only ignored POC artifacts.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.domain import Coordinates, Location, RouteRequest, TravelMode
from backend.routing_provider import (
    NoRouteError,
    OpenTripPlannerProvider,
    RoutingGraphMetadata,
    RoutingProviderError,
)


OUTPUT = PROJECT_ROOT / "data" / "travel-time-surface-validation" / "r5"
ORIGIN = Coordinates(42.99120169375, -81.24700425)
REFERENCE_DEPARTURE = datetime(2026, 6, 15, 8, tzinfo=ZoneInfo("America/Toronto"))
PROJECT_THREE_SAMPLE_DEPARTURES = tuple(
    datetime(2026, 6, 15, hour, minute, tzinfo=ZoneInfo("America/Toronto"))
    for hour, minute in ((7, 30), (8, 0), (8, 30))
)


def percentile(values: Iterable[float], value: float) -> float | None:
    """Use deterministic linear interpolation without a numpy dependency."""
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * value
    lower, upper = int(index), min(int(index) + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def summary(records: list[dict[str, object]]) -> dict[str, object]:
    mutually_reachable = [
        float(item["absolute_difference_minutes"])
        for item in records
        if item["absolute_difference_minutes"] is not None
    ]
    counts = {"reachable_both": 0, "unreachable_both": 0, "r5_only": 0, "otp_only": 0}
    for item in records:
        r5_reachable = item["r5_travel_time_seconds"] is not None
        otp_reachable = item["otp_statistic_seconds"] is not None
        if r5_reachable and otp_reachable:
            counts["reachable_both"] += 1
        elif not r5_reachable and not otp_reachable:
            counts["unreachable_both"] += 1
        elif r5_reachable:
            counts["r5_only"] += 1
        else:
            counts["otp_only"] += 1
    return {
        **counts,
        "absolute_error_minutes": {
            "median": percentile(mutually_reachable, 0.5),
            "p75": percentile(mutually_reachable, 0.75),
            "p90": percentile(mutually_reachable, 0.9),
            "max": max(mutually_reachable) if mutually_reachable else None,
        },
        "over_five_minutes": [
            int(item["grid_cell_id"])
            for item in records
            if item["absolute_difference_minutes"] is not None
            and float(item["absolute_difference_minutes"]) > 5
        ],
    }


def _route_seconds(
    provider: OpenTripPlannerProvider, destination: Coordinates, mode: TravelMode
) -> tuple[int | None, str | None]:
    try:
        return provider.get_route(RouteRequest(ORIGIN, destination, mode)).duration_seconds, None
    except NoRouteError:
        return None, "no_route"
    except RoutingProviderError as error:
        return None, f"{type(error).__name__}: {error}"


def _transit_seconds(
    provider: OpenTripPlannerProvider,
    destination: Coordinates,
    departures: list[datetime],
) -> tuple[list[object], str | None]:
    try:
        samples = provider.get_samples(
            Location("property-3", "Validation origin", ORIGIN),
            Location("grid", "Validation cell", destination),
            departures,
        )
    except RoutingProviderError as error:
        return [], f"{type(error).__name__}: {error}"
    return [item for item in samples if item.duration_seconds is not None], None


def _record(
    *,
    mode: str,
    point: dict[str, object],
    otp_values: list[int],
    otp_error: str | None,
    window_minutes: int | None = None,
    three_sample_values: list[int] | None = None,
    transit_samples: list[object] | None = None,
) -> dict[str, object]:
    r5_seconds = point["r5_travel_time_seconds"]
    otp_seconds = round(statistics.median(otp_values)) if otp_values else None
    difference = (
        round(abs(int(r5_seconds) - otp_seconds) / 60, 3)
        if r5_seconds is not None and otp_seconds is not None
        else None
    )
    representative = None
    if transit_samples and otp_seconds is not None:
        representative = min(
            transit_samples,
            key=lambda item: abs(int(item.duration_seconds) - otp_seconds),
        )
    return {
        "mode": mode,
        "origin_property_id": 3,
        "grid_cell_id": int(point["grid_cell_id"]),
        "latitude": point["latitude"],
        "longitude": point["longitude"],
        "r5_travel_time_seconds": r5_seconds,
        "r5_snap_distance_metres": point.get("r5_snap_distance_metres"),
        "otp_statistic_seconds": otp_seconds,
        "otp_available_departures": len(otp_values),
        "otp_window_minutes": window_minutes,
        "otp_three_sample_median_seconds": (
            round(statistics.median(three_sample_values)) if three_sample_values else None
        ),
        "absolute_difference_minutes": difference,
        "otp_error": otp_error,
        "otp_representative_transfer_count": (
            representative.transfer_count if representative is not None else None
        ),
        "otp_representative_walking_seconds": (
            representative.walking_duration_seconds if representative is not None else None
        ),
        "selection_role": point.get("selection_role"),
    }


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = [
        "mode",
        "origin_property_id",
        "grid_cell_id",
        "latitude",
        "longitude",
        "r5_travel_time_seconds",
        "r5_snap_distance_metres",
        "otp_statistic_seconds",
        "otp_available_departures",
        "otp_window_minutes",
        "otp_three_sample_median_seconds",
        "absolute_difference_minutes",
        "otp_error",
        "otp_representative_transfer_count",
        "otp_representative_walking_seconds",
        "selection_role",
    ]
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--otp-base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--transit-window-minutes", type=int, default=10)
    args = parser.parse_args()
    if args.transit_window_minutes < 1:
        parser.error("--transit-window-minutes must be positive")
    validation = json.loads((OUTPUT / "r5-validation-points.json").read_text())
    manifest = json.loads((PROJECT_ROOT / "data" / "routing" / "build-manifest.json").read_text())
    metadata = RoutingGraphMetadata(
        manifest["router_version"],
        manifest["network_version"],
        manifest["schedule_version"],
        datetime.fromisoformat(manifest["graph_built_at"]),
    )
    provider = OpenTripPlannerProvider(
        base_url=args.otp_base_url, router_id="default", timeout_seconds=30, metadata=metadata
    )
    provider.preflight(expected=metadata)

    records_by_mode: dict[str, list[dict[str, object]]] = {}
    for r5_mode, otp_mode, filename in (
        ("walk", TravelMode.WALKING, "r5-accuracy-walk.csv"),
        ("bicycle", TravelMode.CYCLING, "r5-accuracy-bike.csv"),
    ):
        records: list[dict[str, object]] = []
        for point in validation["surfaces"][r5_mode]:
            destination = Coordinates(float(point["latitude"]), float(point["longitude"]))
            seconds, error = _route_seconds(provider, destination, otp_mode)
            records.append(
                _record(
                    mode=r5_mode,
                    point=point,
                    otp_values=[seconds] if seconds is not None else [],
                    otp_error=error,
                )
            )
        records_by_mode[r5_mode] = records
        _write_csv(OUTPUT / filename, records)

    transit_records: list[dict[str, object]] = []
    window_departures = [
        REFERENCE_DEPARTURE + timedelta(minutes=minute)
        for minute in range(args.transit_window_minutes)
    ]
    for point in validation["surfaces"]["transit_short_window"]:
        destination = Coordinates(float(point["latitude"]), float(point["longitude"]))
        window_samples, error = _transit_seconds(provider, destination, window_departures)
        project_samples, project_error = _transit_seconds(
            provider, destination, list(PROJECT_THREE_SAMPLE_DEPARTURES)
        )
        window_values = [int(item.duration_seconds) for item in window_samples]
        project_values = [int(item.duration_seconds) for item in project_samples]
        transit_records.append(
            _record(
                mode="transit",
                point=point,
                otp_values=window_values,
                otp_error=error or project_error,
                window_minutes=args.transit_window_minutes,
                three_sample_values=project_values,
                transit_samples=window_samples,
            )
        )
    records_by_mode["transit"] = transit_records
    _write_csv(OUTPUT / "r5-accuracy-transit.csv", transit_records)

    summaries = {mode: summary(records) for mode, records in records_by_mode.items()}
    reachability = [
        {"mode": mode, "grid_cell_id": item["grid_cell_id"], **summary([item])}
        for mode, records in records_by_mode.items()
        for item in records
    ]
    with (OUTPUT / "r5-reachability-comparison.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "mode", "grid_cell_id", "reachable_both", "unreachable_both", "r5_only", "otp_only",
                "absolute_error_minutes", "over_five_minutes",
            ],
        )
        writer.writeheader()
        writer.writerows(reachability)
    (OUTPUT / "r5-otp-validation-summary.json").write_text(
        json.dumps(
            {
                "origin_property_id": 3,
                "otp": {"router_version": manifest["router_version"], "base_url": args.otp_base_url},
                "transit_equivalent_window": {
                    "start": REFERENCE_DEPARTURE.isoformat(),
                    "minute_by_minute_departures": len(window_departures),
                    "statistic": "median of available exact OTP departures",
                },
                "secondary_project_three_sample": [
                    value.isoformat() for value in PROJECT_THREE_SAMPLE_DEPARTURES
                ],
                "summaries": summaries,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    report_path = OUTPUT / "r5-poc-report.md"
    report = report_path.read_text(encoding="utf-8") if report_path.exists() else "# R5/r5py local proof of concept\n"
    report = report.split("\n## Exact OTP validation\n", 1)[0].rstrip()
    report += "\n\n## Exact OTP validation\n\n"
    report += (
        "Ten deterministic property-3 cells were checked per mode against local OTP. "
        "Walk/bicycle use one exact departure-independent route; transit is the median "
        "of the 10 starts from 08:00 through 08:09 local time.\n"
    )
    for mode, result in summaries.items():
        error = result["absolute_error_minutes"]
        report += (
            f"- {mode}: both={result['reachable_both']}, R5-only={result['r5_only']}, "
            f"OTP-only={result['otp_only']}, median/p90/max error="
            f"{error['median']}/{error['p90']}/{error['max']} minutes.\n"
        )
    report += (
        "Large edge-cell differences must be interpreted with their recorded R5 snapping distances; "
        "R5 is not accepted here as an exact-route replacement.\n"
    )
    report_path.write_text(report, encoding="utf-8")
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
