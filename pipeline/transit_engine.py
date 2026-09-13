"""
transit_engine.py

Stage 4 of the UWO housing pipeline.

Transit v1:
- Reads London Transit GTFS zip
- Finds nearest bus stops for each map-ready listing
- Finds routes serving nearby stops
- Detects whether nearby routes also serve Western University area
- Calculates a simple transit_score

Input:
  data/processed/stage3_geocoded_listings_qc.csv

GTFS:
  data/transit/google_transit.zip

Output:
  data/processed/stage4_transit_scored_listings.csv

Adds:
  nearest_stop_id
  nearest_stop_name
  nearest_stop_distance_m
  walking_minutes_to_nearest_stop
  nearby_stop_count
  nearby_route_ids
  nearby_route_names
  western_route_ids
  western_route_names
  has_direct_western_route
  transit_score
"""

import argparse
import math
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

import pandas as pd


WESTERN_LAT = 43.0096
WESTERN_LON = -81.2737

DEFAULT_GTFS_ZIP = Path("data/transit/google_transit.zip")
DEFAULT_INPUT = Path("data/processed/stage3_geocoded_listings_qc.csv")
DEFAULT_OUTPUT = Path("data/processed/stage4_transit_scored_listings.csv")

# A normal walking speed is roughly 80 m/min.
WALKING_METERS_PER_MINUTE = 80

# Nearby stops considered useful for a listing.
NEARBY_STOP_RADIUS_M = 600

# Stops within this radius of Western count as "Western-serving stops".
WESTERN_STOP_RADIUS_M = 900


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    radius_m = 6371008.8

    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))

    d_phi = math.radians(float(lat2) - float(lat1))
    d_lambda = math.radians(float(lon2) - float(lon1))

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )

    return 2 * radius_m * math.asin(math.sqrt(a))


def read_gtfs_file(gtfs_zip: Path, filename: str) -> pd.DataFrame:
    with zipfile.ZipFile(gtfs_zip) as z:
        with z.open(filename) as f:
            return pd.read_csv(f, dtype=str)


def load_gtfs(gtfs_zip: Path) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if not gtfs_zip.exists():
        raise FileNotFoundError(f"GTFS zip not found: {gtfs_zip}")

    print(f"Loading GTFS from {gtfs_zip}")

    stops = read_gtfs_file(gtfs_zip, "stops.txt")
    routes = read_gtfs_file(gtfs_zip, "routes.txt")
    trips = read_gtfs_file(gtfs_zip, "trips.txt")
    stop_times = read_gtfs_file(gtfs_zip, "stop_times.txt")

    stops["stop_lat"] = pd.to_numeric(stops["stop_lat"], errors="coerce")
    stops["stop_lon"] = pd.to_numeric(stops["stop_lon"], errors="coerce")

    stops = stops.dropna(subset=["stop_lat", "stop_lon"]).copy()

    print(f"Stops: {len(stops)}")
    print(f"Routes: {len(routes)}")
    print(f"Trips: {len(trips)}")
    print(f"Stop times: {len(stop_times)}")

    return stops, routes, trips, stop_times


def build_route_lookup(
    routes: pd.DataFrame,
    trips: pd.DataFrame,
    stop_times: pd.DataFrame,
) -> Dict[str, Set[str]]:
    """
    Returns:
      stop_id -> set(route_id)

    Uses stop_times -> trips to determine which routes serve each stop.
    """
    print("Building stop → route lookup...")

    trip_routes = trips[["trip_id", "route_id"]].drop_duplicates()

    stop_routes = (
        stop_times[["trip_id", "stop_id"]]
        .drop_duplicates()
        .merge(trip_routes, on="trip_id", how="left")
        .dropna(subset=["route_id"])
    )

    lookup: Dict[str, Set[str]] = {}

    for stop_id, group in stop_routes.groupby("stop_id"):
        lookup[str(stop_id)] = set(group["route_id"].astype(str))

    return lookup


def build_route_name_lookup(routes: pd.DataFrame) -> Dict[str, str]:
    lookup = {}

    for _, row in routes.iterrows():
        route_id = str(row.get("route_id", "")).strip()
        short = str(row.get("route_short_name", "")).strip()
        long = str(row.get("route_long_name", "")).strip()

        label = short or long or route_id
        lookup[route_id] = label

    return lookup


def find_western_routes(
    stops: pd.DataFrame,
    stop_to_routes: Dict[str, Set[str]],
    western_radius_m: float,
) -> Set[str]:
    """
    A route is considered Western-serving if it has at least one stop
    within western_radius_m of the Western campus reference point.
    """
    western_routes: Set[str] = set()

    for _, stop in stops.iterrows():
        distance = haversine_m(
            stop["stop_lat"],
            stop["stop_lon"],
            WESTERN_LAT,
            WESTERN_LON,
        )

        if distance <= western_radius_m:
            stop_id = str(stop["stop_id"])
            western_routes.update(stop_to_routes.get(stop_id, set()))

    print(f"Western-serving routes found: {len(western_routes)}")
    return western_routes


def sorted_route_labels(route_ids: Iterable[str], route_name_lookup: Dict[str, str]) -> List[str]:
    labels = [route_name_lookup.get(str(route_id), str(route_id)) for route_id in route_ids]

    def sort_key(value: str):
        try:
            return (0, int(value))
        except Exception:
            return (1, value)

    return sorted(set(labels), key=sort_key)


def calculate_transit_score(
    nearest_stop_distance_m: float,
    nearby_route_count: int,
    western_route_count: int,
) -> int:
    """
    Simple MVP transit score.

    Components:
    - Close bus stop is valuable
    - More nearby routes are better
    - Direct Western-serving route is very valuable
    """

    if nearest_stop_distance_m is None or pd.isna(nearest_stop_distance_m):
        return 0

    # Stop-distance component, max 45 points.
    # Full points if <=150m, fades to 0 around 900m.
    distance_score = max(0, min(45, 45 * (1 - max(0, nearest_stop_distance_m - 150) / 750)))

    # Route variety, max 25 points.
    route_score = min(25, nearby_route_count * 5)

    # Western-serving routes, max 30 points.
    western_score = min(30, western_route_count * 15)

    return int(round(distance_score + route_score + western_score))


def score_listing_transit(
    listing_row: pd.Series,
    stops: pd.DataFrame,
    stop_to_routes: Dict[str, Set[str]],
    route_name_lookup: Dict[str, str],
    western_routes: Set[str],
    nearby_radius_m: float,
) -> dict:
    lat = pd.to_numeric(listing_row.get("latitude"), errors="coerce")
    lon = pd.to_numeric(listing_row.get("longitude"), errors="coerce")

    if pd.isna(lat) or pd.isna(lon):
        return {
            "nearest_stop_id": None,
            "nearest_stop_name": None,
            "nearest_stop_distance_m": None,
            "walking_minutes_to_nearest_stop": None,
            "nearby_stop_count": 0,
            "nearby_route_ids": None,
            "nearby_route_names": None,
            "western_route_ids": None,
            "western_route_names": None,
            "has_direct_western_route": False,
            "transit_score": 0,
        }

    distances = stops.apply(
        lambda stop: haversine_m(lat, lon, stop["stop_lat"], stop["stop_lon"]),
        axis=1,
    )

    nearest_idx = distances.idxmin()
    nearest_stop = stops.loc[nearest_idx]
    nearest_distance = float(distances.loc[nearest_idx])

    nearby_stops = stops[distances <= nearby_radius_m].copy()

    nearby_routes: Set[str] = set()

    for stop_id in nearby_stops["stop_id"].astype(str):
        nearby_routes.update(stop_to_routes.get(stop_id, set()))

    listing_western_routes = nearby_routes & western_routes

    nearby_route_names = sorted_route_labels(nearby_routes, route_name_lookup)
    western_route_names = sorted_route_labels(listing_western_routes, route_name_lookup)

    transit_score = calculate_transit_score(
        nearest_stop_distance_m=nearest_distance,
        nearby_route_count=len(nearby_routes),
        western_route_count=len(listing_western_routes),
    )

    return {
        "nearest_stop_id": str(nearest_stop.get("stop_id")),
        "nearest_stop_name": nearest_stop.get("stop_name"),
        "nearest_stop_distance_m": round(nearest_distance, 1),
        "walking_minutes_to_nearest_stop": round(nearest_distance / WALKING_METERS_PER_MINUTE, 1),
        "nearby_stop_count": int(len(nearby_stops)),
        "nearby_route_ids": ",".join(sorted(nearby_routes)) if nearby_routes else None,
        "nearby_route_names": ",".join(nearby_route_names) if nearby_route_names else None,
        "western_route_ids": ",".join(sorted(listing_western_routes)) if listing_western_routes else None,
        "western_route_names": ",".join(western_route_names) if western_route_names else None,
        "has_direct_western_route": bool(listing_western_routes),
        "transit_score": transit_score,
    }


def apply_transit_engine(
    input_csv: Path,
    output_csv: Path,
    gtfs_zip: Path,
    limit: int | None,
    nearby_radius_m: float,
    western_radius_m: float,
) -> pd.DataFrame:
    listings = pd.read_csv(input_csv)

    if limit is not None:
        listings = listings.head(limit).copy()
    else:
        listings = listings.copy()

    stops, routes, trips, stop_times = load_gtfs(gtfs_zip)

    stop_to_routes = build_route_lookup(routes, trips, stop_times)
    route_name_lookup = build_route_name_lookup(routes)
    western_routes = find_western_routes(
        stops=stops,
        stop_to_routes=stop_to_routes,
        western_radius_m=western_radius_m,
    )

    transit_rows = []

    for idx, row in listings.iterrows():
        listing_id = row.get("listing_id")
        print(f"[{idx + 1}/{len(listings)}] scoring listing_id={listing_id}")

        transit = score_listing_transit(
            listing_row=row,
            stops=stops,
            stop_to_routes=stop_to_routes,
            route_name_lookup=route_name_lookup,
            western_routes=western_routes,
            nearby_radius_m=nearby_radius_m,
        )

        merged = row.to_dict()
        merged.update(transit)
        transit_rows.append(merged)

    out_df = pd.DataFrame(transit_rows)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(output_csv, index=False)

    print(f"\nSaved transit-scored listings → {output_csv}")
    print(f"Rows: {len(out_df)}")
    print("\nTransit score summary:")
    print(out_df["transit_score"].describe())
    print("\nDirect Western route counts:")
    print(out_df["has_direct_western_route"].value_counts(dropna=False))

    return out_df


def main() -> None:
    parser = argparse.ArgumentParser(description="Add London Transit GTFS scoring to listings.")
    parser.add_argument(
        "input_csv",
        type=Path,
        default=DEFAULT_INPUT,
        nargs="?",
    )
    parser.add_argument(
        "--gtfs-zip",
        type=Path,
        default=DEFAULT_GTFS_ZIP,
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=DEFAULT_OUTPUT,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--nearby-radius-m",
        type=float,
        default=NEARBY_STOP_RADIUS_M,
    )
    parser.add_argument(
        "--western-radius-m",
        type=float,
        default=WESTERN_STOP_RADIUS_M,
    )

    args = parser.parse_args()

    apply_transit_engine(
        input_csv=args.input_csv,
        output_csv=args.output_csv,
        gtfs_zip=args.gtfs_zip,
        limit=args.limit,
        nearby_radius_m=args.nearby_radius_m,
        western_radius_m=args.western_radius_m,
    )


if __name__ == "__main__":
    main()