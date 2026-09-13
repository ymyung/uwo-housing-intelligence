"""Small local operator tool for the accepted walking numerical surface."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.travel_time_surface_repository import PostgresSurfaceRepository
from backend.travel_time_surface_service import HttpR5SurfaceClient, WalkingSurfaceService
from backend.travel_time_surfaces import load_surface_policy


def property_origin(database_url: str, property_id: int) -> tuple[float, float]:
    import psycopg
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "select latitude, longitude from public.housing_properties where id = %s",
            (property_id,)
        ).fetchone()
    if row is None:
        raise ValueError("property has no trusted routing origin in current listings")
    return float(row[0]), float(row[1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "compute", "inspect"))
    parser.add_argument("--property-id", type=int, required=True)
    args = parser.parse_args()
    policy = load_surface_policy(PROJECT_ROOT / "config" / "travel-time-surface.example.toml")
    repository = PostgresSurfaceRepository.from_environment()
    service = WalkingSurfaceService(repository=repository, r5=HttpR5SurfaceClient(), policy=policy)
    database_url = os.getenv("DATABASE_URL", "").strip() or os.getenv("ACCESSIBILITY_DATABASE_URL", "").strip()
    latitude, longitude = property_origin(database_url, args.property_id)
    if args.command == "status":
        print(json.dumps({"r5": service.r5.health(), "property_id": args.property_id, "latitude": latitude, "longitude": longitude}, indent=2))
        return
    surface, cache_hit = service.get_or_compute(property_id=args.property_id, latitude=latitude, longitude=longitude)
    print(json.dumps(service.metadata(surface, cache_hit=cache_hit), indent=2))


if __name__ == "__main__":
    main()
