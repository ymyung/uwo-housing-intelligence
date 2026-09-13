"""
import_listing_images_to_supabase.py

Imports image_url and image_urls into Supabase for existing listings.

Input:
  data/processed/stage5_otp_travel_times_with_images.csv

Requires Supabase columns:
  image_url text
  image_urls text
"""

import argparse
import os
import time
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from dotenv import load_dotenv
from supabase import create_client


DEFAULT_INPUT = Path("data/processed/stage5_otp_travel_times_with_images.csv")


def clean_value(value: Any) -> Optional[str]:
    if pd.isna(value):
        return None

    text = str(value).strip()

    if not text or text.lower() in {"nan", "none", "null"}:
        return None

    return text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import listing image URLs into Supabase."
    )

    parser.add_argument(
        "--input-csv",
        type=Path,
        default=DEFAULT_INPUT,
    )

    parser.add_argument(
        "--only-with-images",
        action="store_true",
        default=True,
        help="Only update rows that have image_url.",
    )

    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.01,
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

    required = {"listing_id", "image_url", "image_urls"}

    missing = required - set(df.columns)

    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    supabase = create_client(supabase_url, service_key)

    updated = 0
    skipped = 0

    for idx, row in df.iterrows():
        listing_id = clean_value(row.get("listing_id"))
        image_url = clean_value(row.get("image_url"))
        image_urls = clean_value(row.get("image_urls"))

        if not listing_id:
            skipped += 1
            continue

        if args.only_with_images and not image_url:
            skipped += 1
            continue

        payload = {
            "image_url": image_url,
            "image_urls": image_urls,
        }

        (
            supabase
            .table("listings")
            .update(payload)
            .eq("listing_id", str(listing_id))
            .execute()
        )

        updated += 1

        if updated % 50 == 0:
            print(f"Updated {updated} rows...")

        time.sleep(args.sleep_seconds)

    print("\nDone.")
    print(f"Updated: {updated}")
    print(f"Skipped: {skipped}")


if __name__ == "__main__":
    main()