"""
import_listings_to_supabase.py

Imports the final housing dataset into Supabase.

Works with:
  data/processed/stage3_geocoded_listings_qc.csv
  data/processed/stage4_transit_scored_listings.csv
  data/processed/stage5_otp_travel_times.csv

Table:
  public.listings

This script upserts by listing_id, so rerunning it updates existing listings
instead of creating duplicates.
"""

import argparse
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from dotenv import load_dotenv
from supabase import create_client


DEFAULT_INPUT = Path("data/processed/stage5_otp_travel_times.csv")


CORE_COLUMNS = [
    # Identity / source
    "listing_id",
    "listing_url",

    # Basic listing info
    "title",
    "address",
    "price_numeric",
    "price_text",
    "price_period",
    "housing_type",
    "bedrooms",

    # Lease / rental structure
    "lease_type",
    "lease_term_months",
    "is_sublet",

    # Amenities / attributes
    "furnished",
    "utilities_included",
    "utilities_status",
    "parking_available",
    "parking_spaces",
    "laundry",
    "dishwasher",
    "air_conditioning",
    "bathroom_type",
    "bathrooms",
    "preferred_gender",
    "tenant_type",

    # Geocoding
    "latitude",
    "longitude",
    "distance_to_western_km",
    "map_ready",
    "geocode_status",
    "geocode_confidence",
    "geocode_quality_issue",

    # GTFS transit v1
    "nearest_stop_id",
    "nearest_stop_name",
    "nearest_stop_distance_m",
    "walking_minutes_to_nearest_stop",
    "nearby_stop_count",
    "nearby_route_ids",
    "nearby_route_names",
    "western_route_ids",
    "western_route_names",
    "has_direct_western_route",
    "transit_score",

    # OTP travel times
    "otp_status",
    "otp_error",
    "walk_time_to_western_min",
    "transit_time_to_western_min",
    "transit_walk_time_min",
    "transit_bus_time_min",
    "transit_transfers",
    "otp_used_transit",
    "otp_route_summary",

    # Text content
    "description",
    "amenities",

    # Scrape status
    "scraped_ok",
    "scrape_error",

    "available_from",
    "available_to",
    "availability_text",
    "availability_category",
]


INT_COLUMNS = [
    "bedrooms",
    "lease_term_months",
    "parking_spaces",
    "nearby_stop_count",
    "transit_score",
    "transit_transfers",
]


FLOAT_COLUMNS = [
    "price_numeric",
    "bathrooms",
    "latitude",
    "longitude",
    "distance_to_western_km",
    "geocode_confidence",
    "nearest_stop_distance_m",
    "walking_minutes_to_nearest_stop",
    "walk_time_to_western_min",
    "transit_time_to_western_min",
    "transit_walk_time_min",
    "transit_bus_time_min",
]


BOOL_COLUMNS = [
    "is_sublet",
    "furnished",
    "utilities_included",
    "parking_available",
    "laundry",
    "dishwasher",
    "air_conditioning",
    "map_ready",
    "scraped_ok",
    "has_direct_western_route",
    "otp_used_transit",
]


def clean_value(value: Any) -> Any:
    """
    Convert pandas/CSV values into JSON-safe database values.
    """
    if pd.isna(value):
        return None

    if isinstance(value, str):
        text = value.strip()

        if text == "":
            return None

        lower = text.lower()

        if lower in {"nan", "none", "null"}:
            return None

        if lower == "true":
            return True

        if lower == "false":
            return False

        return text

    return value


def to_int(value: Any) -> Optional[int]:
    value = clean_value(value)

    if value is None:
        return None

    try:
        return int(float(value))
    except Exception:
        return None


def to_float(value: Any) -> Optional[float]:
    value = clean_value(value)

    if value is None:
        return None

    try:
        return float(value)
    except Exception:
        return None


def to_bool(value: Any) -> Optional[bool]:
    value = clean_value(value)

    if value is None:
        return None

    if isinstance(value, bool):
        return value

    text = str(value).strip().lower()

    if text in {"true", "1", "yes", "y"}:
        return True

    if text in {"false", "0", "no", "n"}:
        return False

    return None


def build_record(row: pd.Series) -> Dict[str, Any]:
    """
    Convert one CSV row into one Supabase record.

    Any CSV column not explicitly mapped is still preserved in raw.
    """
    raw: Dict[str, Any] = {}

    for col, value in row.items():
        raw[col] = clean_value(value)

    record: Dict[str, Any] = {}

    for col in CORE_COLUMNS:
        if col in row.index:
            record[col] = clean_value(row.get(col))
        else:
            record[col] = None

    # Required identity fields
    listing_id = clean_value(row.get("listing_id"))
    listing_url = clean_value(row.get("listing_url"))

    record["listing_id"] = str(listing_id) if listing_id is not None else None
    record["listing_url"] = listing_url

    # Type conversions
    for col in INT_COLUMNS:
        record[col] = to_int(row.get(col))

    for col in FLOAT_COLUMNS:
        record[col] = to_float(row.get(col))

    for col in BOOL_COLUMNS:
        record[col] = to_bool(row.get(col))

    # Preserve everything for debugging/reprocessing.
    record["raw"] = raw

    return record


def chunked(items: List[Dict[str, Any]], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def main() -> None:
    parser = argparse.ArgumentParser(description="Import listings CSV into Supabase.")

    parser.add_argument(
        "--input-csv",
        type=Path,
        default=DEFAULT_INPUT,
        help="Input CSV to import. Default is stage5_otp_travel_times.csv.",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
    )

    args = parser.parse_args()

    load_dotenv()

    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not supabase_url:
        raise RuntimeError("Missing SUPABASE_URL in .env")

    if not service_key:
        raise RuntimeError("Missing SUPABASE_SERVICE_ROLE_KEY in .env")

    if not args.input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {args.input_csv}")

    df = pd.read_csv(args.input_csv)

    if "listing_id" not in df.columns:
        raise ValueError("CSV must contain listing_id")

    if "listing_url" not in df.columns:
        raise ValueError("CSV must contain listing_url")

    records: List[Dict[str, Any]] = []

    skipped = 0

    for _, row in df.iterrows():
        listing_id = clean_value(row.get("listing_id"))
        listing_url = clean_value(row.get("listing_url"))

        if not listing_id or not listing_url:
            skipped += 1
            continue

        records.append(build_record(row))

    print(f"Prepared {len(records)} records for import.")
    print(f"Skipped {skipped} rows missing listing_id or listing_url.")

    supabase = create_client(supabase_url, service_key)

    imported = 0

    for batch in chunked(records, args.batch_size):
        (
            supabase
            .table("listings")
            .upsert(batch, on_conflict="listing_id")
            .execute()
        )

        imported += len(batch)
        print(f"Imported/upserted {imported}/{len(records)}")

    print("\nDone.")
    print(f"Total imported/upserted: {imported}")


if __name__ == "__main__":
    main()