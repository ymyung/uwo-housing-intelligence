"""
otp_travel_times.py

Stage 5 of the UWO housing pipeline.

Uses a running OpenTripPlanner server to calculate travel times from each
listing to Western University.

Input:
  data/processed/stage4_transit_scored_listings.csv

Output:
  data/processed/stage5_otp_travel_times.csv

Adds:
  otp_status
  otp_error
  walk_time_to_western_min
  transit_time_to_western_min
  transit_walk_time_min
  transit_bus_time_min
  transit_transfers
  otp_used_transit
  otp_route_summary

Important:
  This version avoids GraphQL variable type issues by building the OTP query
  inline. Your OTP error showed that this OTP build rejects DateTime and
  CoordinateValue variables, even though inline GraphiQL queries work.
"""

import argparse
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests


WESTERN_LAT = 43.0096
WESTERN_LON = -81.2737

DEFAULT_INPUT = Path("data/processed/stage4_transit_scored_listings.csv")
DEFAULT_OUTPUT = Path("data/processed/stage5_otp_travel_times.csv")

DEFAULT_OTP_URLS = [
    "http://localhost:8080/otp/gtfs/v1",
    "http://localhost:8080/otp/transmodel/v3",
    "http://localhost:8080/otp/routers/default/index/graphql",
]


def safe_float(value: Any) -> Optional[float]:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def escape_graphql_string(value: str) -> str:
    """
    Safely escape a string for insertion into a GraphQL query.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_plan_query(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    departure: str,
    first: int = 5,
) -> str:
    """
    Build the OTP GraphQL query with inline coordinate/date values.

    This avoids OTP schema issues where Python GraphQL variables like Float
    and DateTime may not match OTP's custom scalar types.
    """
    departure_safe = escape_graphql_string(departure)

    return f"""
{{
  planConnection(
    origin: {{
      location: {{
        coordinate: {{
          latitude: {origin_lat}
          longitude: {origin_lon}
        }}
      }}
    }}
    destination: {{
      location: {{
        coordinate: {{
          latitude: {dest_lat}
          longitude: {dest_lon}
        }}
      }}
    }}
    dateTime: {{
      earliestDeparture: "{departure_safe}"
    }}
    modes: {{
      direct: [WALK]
      transit: {{
        transit: [{{ mode: BUS }}]
      }}
    }}
    first: {first}
  ) {{
    edges {{
      node {{
        start
        end
        duration
        legs {{
          mode
          duration
          distance
          route {{
            shortName
            longName
          }}
          from {{
            name
          }}
          to {{
            name
          }}
        }}
      }}
    }}
  }}
}}
"""


def find_working_otp_url(candidate_urls: List[str]) -> str:
    """
    Test common OTP GraphQL endpoints and return the first working one.

    This only checks that the endpoint accepts GraphQL.
    The routing query itself is tested later.
    """
    body = {"query": "{ __typename }"}

    for url in candidate_urls:
        try:
            response = requests.post(url, json=body, timeout=10)

            if response.status_code != 200:
                continue

            payload = response.json()

            if "data" in payload:
                print(f"Using OTP GraphQL endpoint: {url}")
                return url

        except Exception:
            continue

    raise RuntimeError(
        "Could not find a working OTP GraphQL endpoint. "
        "Make sure OTP server is running on localhost:8080."
    )


def empty_otp_result(status: str, error: Optional[str] = None) -> Dict[str, Any]:
    return {
        "otp_status": status,
        "otp_error": error,
        "walk_time_to_western_min": None,
        "transit_time_to_western_min": None,
        "transit_walk_time_min": None,
        "transit_bus_time_min": None,
        "transit_transfers": None,
        "otp_used_transit": False,
        "otp_route_summary": None,
    }


def query_otp_trip(
    otp_url: str,
    origin_lat: float,
    origin_lon: float,
    departure: str,
    timeout: int = 30,
) -> Dict[str, Any]:
    query = build_plan_query(
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        dest_lat=WESTERN_LAT,
        dest_lon=WESTERN_LON,
        departure=departure,
        first=5,
    )

    response = requests.post(
        otp_url,
        json={"query": query},
        timeout=timeout,
    )

    response.raise_for_status()
    payload = response.json()

    if "errors" in payload:
        return empty_otp_result(
            status="graphql_error",
            error=str(payload["errors"])[:1000],
        )

    edges = (
        payload
        .get("data", {})
        .get("planConnection", {})
        .get("edges", [])
    )

    if not edges:
        return empty_otp_result(status="no_route")

    itineraries = [edge.get("node", {}) for edge in edges if edge.get("node")]

    if not itineraries:
        return empty_otp_result(status="no_itineraries")

    walk_only = None
    best_transit = None

    for itinerary in itineraries:
        legs = itinerary.get("legs", []) or []
        modes = [leg.get("mode") for leg in legs]

        has_transit = any(mode not in {"WALK", None} for mode in modes)

        if not has_transit and walk_only is None:
            walk_only = itinerary

        if has_transit and best_transit is None:
            best_transit = itinerary

    # If OTP does not return a separate walk-only itinerary, use the first itinerary
    # as a fallback for walking time.
    if walk_only is None:
        walk_only = itineraries[0]

    # Prefer transit itinerary if one exists. Otherwise use walking-only.
    selected = best_transit or walk_only
    selected_legs = selected.get("legs", []) or []

    transit_walk_seconds = 0
    transit_bus_seconds = 0
    route_names = []

    for leg in selected_legs:
        mode = leg.get("mode")
        duration = leg.get("duration") or 0

        if mode == "WALK":
            transit_walk_seconds += duration
        else:
            transit_bus_seconds += duration

            route = leg.get("route") or {}
            label = route.get("shortName") or route.get("longName")

            if label:
                route_names.append(str(label))

    used_transit = best_transit is not None
    transfers = max(0, len(route_names) - 1)

    walk_duration_seconds = walk_only.get("duration") or 0
    selected_duration_seconds = selected.get("duration") or 0

    return {
        "otp_status": "ok",
        "otp_error": None,
        "walk_time_to_western_min": round(walk_duration_seconds / 60, 1),
        "transit_time_to_western_min": round(selected_duration_seconds / 60, 1),
        "transit_walk_time_min": round(transit_walk_seconds / 60, 1),
        "transit_bus_time_min": round(transit_bus_seconds / 60, 1),
        "transit_transfers": transfers,
        "otp_used_transit": used_transit,
        "otp_route_summary": ",".join(sorted(set(route_names))) if route_names else None,
    }


def apply_otp_travel_times(
    input_csv: Path,
    output_csv: Path,
    otp_url: Optional[str],
    departure: str,
    limit: Optional[int],
    sleep_seconds: float,
    save_every: int,
) -> pd.DataFrame:
    df = pd.read_csv(input_csv)

    if limit is not None:
        df = df.head(limit).copy()
    else:
        df = df.copy()

    if otp_url is None:
        otp_url = find_working_otp_url(DEFAULT_OTP_URLS)
    else:
        print(f"Using provided OTP URL: {otp_url}")

    rows = []

    for idx, row in df.iterrows():
        listing_id = row.get("listing_id")
        lat = safe_float(row.get("latitude"))
        lon = safe_float(row.get("longitude"))

        out = row.to_dict()

        if lat is None or lon is None:
            result = empty_otp_result(status="missing_coordinates")
        else:
            print(f"[{idx + 1}/{len(df)}] OTP routing listing_id={listing_id}")

            try:
                result = query_otp_trip(
                    otp_url=otp_url,
                    origin_lat=lat,
                    origin_lon=lon,
                    departure=departure,
                )
            except Exception as exc:
                result = empty_otp_result(
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )

            time.sleep(sleep_seconds)

        out.update(result)
        rows.append(out)

        # Progressive save so a long run is not lost if interrupted.
        if save_every > 0 and len(rows) % save_every == 0:
            partial_df = pd.DataFrame(rows)
            output_csv.parent.mkdir(parents=True, exist_ok=True)
            partial_df.to_csv(output_csv, index=False)
            print(f"Progress saved → {output_csv} ({len(rows)} rows)")

    out_df = pd.DataFrame(rows)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(output_csv, index=False)

    print(f"\nSaved OTP travel times → {output_csv}")
    print(f"Rows: {len(out_df)}")

    print("\nOTP status counts:")
    print(out_df["otp_status"].value_counts(dropna=False))

    if "transit_time_to_western_min" in out_df.columns:
        print("\nTransit time summary:")
        print(out_df["transit_time_to_western_min"].describe())

    if "otp_used_transit" in out_df.columns:
        print("\nTransit used counts:")
        print(out_df["otp_used_transit"].value_counts(dropna=False))

    return out_df


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calculate OTP travel times for UWO listings."
    )

    parser.add_argument(
        "input_csv",
        type=Path,
        nargs="?",
        default=DEFAULT_INPUT,
    )

    parser.add_argument(
        "--output-csv",
        type=Path,
        default=DEFAULT_OUTPUT,
    )

    parser.add_argument(
        "--otp-url",
        type=str,
        default=None,
        help="OTP GraphQL endpoint, e.g. http://localhost:8080/otp/gtfs/v1",
    )

    parser.add_argument(
        "--departure",
        type=str,
        default="2026-05-08T08:30:00-04:00",
        help="ISO datetime with timezone offset.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.05,
    )

    parser.add_argument(
        "--save-every",
        type=int,
        default=50,
        help="Progressively save after this many rows. Use 0 to disable.",
    )

    args = parser.parse_args()

    apply_otp_travel_times(
        input_csv=args.input_csv,
        output_csv=args.output_csv,
        otp_url=args.otp_url,
        departure=args.departure,
        limit=args.limit,
        sleep_seconds=args.sleep_seconds,
        save_every=args.save_every,
    )


if __name__ == "__main__":
    main()