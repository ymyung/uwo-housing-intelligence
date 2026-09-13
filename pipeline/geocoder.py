"""
geocoder.py

Stage 3 of the UWO housing pipeline.

Input:
  data/processed/stage2_ai_enriched_reviewed.csv
  OR data/processed/stage2_ai_enriched.csv

Output:
  data/processed/stage3_geocoded_listings.csv

Adds:
  latitude
  longitude
  geocode_status
  geocode_confidence
  geocode_match_type
  geocode_result_type
  geocode_formatted
  geocode_city
  geocode_postcode
  geocode_country_code
  distance_to_western_km

Uses Geoapify Forward Geocoding API with caching.
"""

import argparse
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import requests
from dotenv import load_dotenv

try:
    from pipeline.run_context import RunContext
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline.run_context import RunContext


GEOAPIFY_URL = "https://api.geoapify.com/v1/geocode/search"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Western University approximate main-campus coordinate
WESTERN_LAT = 43.0096
WESTERN_LON = -81.2737

DEFAULT_CACHE_PATH = Path("data/processed/geocode_cache.csv")
DEFAULT_CONFIDENCE_THRESHOLD = 0.8


def safe_str(value: Any) -> Optional[str]:
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text if text else None


def normalize_address(address: Any) -> Optional[str]:
    """
    Make site addresses more geocoder-friendly.

    Many UWO listings only have partial addresses like:
      "170 Huron Street"
    so we append:
      London, ON, Canada
    """
    text = safe_str(address)
    if not text:
        return None

    text = " ".join(text.split())

    lower = text.lower()

    if "london" not in lower:
        text = f"{text}, London"

    if "ontario" not in lower and ", on" not in lower:
        text = f"{text}, ON"

    if "canada" not in lower:
        text = f"{text}, Canada"

    return text


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Great-circle distance in kilometres.
    This is straight-line distance, not walking/transit distance.
    """
    radius_km = 6371.0088

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)

    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )

    return 2 * radius_km * math.asin(math.sqrt(a))


def load_cache(cache_path: Path) -> Dict[str, Dict[str, Any]]:
    if not cache_path.exists():
        return {}

    df = pd.read_csv(cache_path)
    cache: Dict[str, Dict[str, Any]] = {}

    for _, row in df.iterrows():
        query = safe_str(row.get("geocode_query"))
        if not query:
            continue
        cache[query] = row.to_dict()

    return cache


def save_cache(cache: Dict[str, Dict[str, Any]], cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    rows = list(cache.values())
    if rows:
        pd.DataFrame(rows).to_csv(cache_path, index=False)
    else:
        pd.DataFrame(columns=[
            "geocode_query",
            "latitude",
            "longitude",
            "geocode_status",
            "geocode_confidence",
            "geocode_match_type",
            "geocode_result_type",
            "geocode_formatted",
            "geocode_city",
            "geocode_postcode",
            "geocode_country_code",
            "geocode_error",
        ]).to_csv(cache_path, index=False)


def should_refresh_cached_result(
    cached: Dict[str, Any],
    *,
    refresh_errors: bool = False,
    refresh_low_confidence: bool = False,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> bool:
    status = safe_str(cached.get("geocode_status"))
    if refresh_errors and status in {"error", "not_found"}:
        return True
    confidence = pd.to_numeric(cached.get("geocode_confidence"), errors="coerce")
    return bool(
        refresh_low_confidence
        and pd.notna(confidence)
        and float(confidence) < confidence_threshold
    )


def cache_miss_result(query: str) -> Dict[str, Any]:
    return {
        "geocode_query": query,
        "latitude": None,
        "longitude": None,
        "geocode_status": "cache_miss",
        "geocode_confidence": None,
        "geocode_match_type": None,
        "geocode_result_type": None,
        "geocode_formatted": None,
        "geocode_city": None,
        "geocode_postcode": None,
        "geocode_country_code": None,
        "geocode_error": "No cached geocode result; network disabled by --cache-only.",
    }


def select_run_input(run_context: RunContext) -> Path:
    if run_context.paths.stage2_reviewed.exists():
        return run_context.paths.stage2_reviewed
    if run_context.paths.stage2_enriched.exists():
        return run_context.paths.stage2_enriched
    raise FileNotFoundError("Neither reviewed nor enriched Stage 2 output exists.")


def geocode_address(
    query: str,
    api_key: str,
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    Calls Geoapify Forward Geocoding API.

    We bias and filter to Canada because all UWO listings should be near London, ON.
    """
    params = {
        "text": query,
        "apiKey": api_key,
        "limit": 1,
        "format": "json",
        "filter": "countrycode:ca",
        # Bias toward Western/London area.
        "bias": f"proximity:{WESTERN_LON},{WESTERN_LAT}",
    }

    try:
        response = requests.get(GEOAPIFY_URL, params=params, timeout=timeout)
        response.raise_for_status()
        data = response.json()

        results = data.get("results", [])

        if not results:
            return {
                "geocode_query": query,
                "latitude": None,
                "longitude": None,
                "geocode_status": "not_found",
                "geocode_confidence": None,
                "geocode_match_type": None,
                "geocode_result_type": None,
                "geocode_formatted": None,
                "geocode_city": None,
                "geocode_postcode": None,
                "geocode_country_code": None,
                "geocode_error": None,
            }

        result = results[0]
        rank = result.get("rank", {}) or {}

        return {
            "geocode_query": query,
            "latitude": result.get("lat"),
            "longitude": result.get("lon"),
            "geocode_status": "ok",
            "geocode_confidence": rank.get("confidence"),
            "geocode_match_type": rank.get("match_type"),
            "geocode_result_type": result.get("result_type"),
            "geocode_formatted": result.get("formatted"),
            "geocode_city": result.get("city"),
            "geocode_postcode": result.get("postcode"),
            "geocode_country_code": result.get("country_code"),
            "geocode_error": None,
        }

    except Exception as exc:
        return {
            "geocode_query": query,
            "latitude": None,
            "longitude": None,
            "geocode_status": "error",
            "geocode_confidence": None,
            "geocode_match_type": None,
            "geocode_result_type": None,
            "geocode_formatted": None,
            "geocode_city": None,
            "geocode_postcode": None,
            "geocode_country_code": None,
            "geocode_error": f"{type(exc).__name__}: {exc}",
        }


def apply_geocoding(
    input_csv: Path,
    output_csv: Path,
    cache_csv: Path,
    api_key: Optional[str],
    limit: Optional[int] = None,
    sleep_seconds: float = 0.15,
    cache_only: bool = False,
    refresh_errors: bool = False,
    refresh_low_confidence: bool = False,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    stats: Optional[Dict[str, int]] = None,
) -> pd.DataFrame:
    df = pd.read_csv(input_csv)

    if limit is not None:
        df = df.head(limit).copy()
    else:
        df = df.copy()

    if "address" not in df.columns:
        raise ValueError("Input CSV must contain an 'address' column.")

    cache = load_cache(cache_csv)

    rows = []
    unique_new_calls = 0
    cache_hits = 0
    cache_misses = 0

    for idx, row in df.iterrows():
        listing_id = row.get("listing_id")
        raw_address = row.get("address")
        query = normalize_address(raw_address)

        out = row.to_dict()
        out["geocode_query"] = query

        if not query:
            geo = {
                "geocode_query": None,
                "latitude": None,
                "longitude": None,
                "geocode_status": "missing_address",
                "geocode_confidence": None,
                "geocode_match_type": None,
                "geocode_result_type": None,
                "geocode_formatted": None,
                "geocode_city": None,
                "geocode_postcode": None,
                "geocode_country_code": None,
                "geocode_error": None,
            }

        elif query in cache and (
            cache_only
            or not should_refresh_cached_result(
                cache[query],
                refresh_errors=refresh_errors,
                refresh_low_confidence=refresh_low_confidence,
                confidence_threshold=confidence_threshold,
            )
        ):
            geo = dict(cache[query])
            cache_hits += 1
            print(f"[{idx + 1}/{len(df)}] id={listing_id} cache hit: {query}")

        elif cache_only:
            geo = cache_miss_result(query)
            cache_misses += 1

        else:
            if not api_key:
                raise RuntimeError("GEOAPIFY_API_KEY is required unless --cache-only is used.")
            print(f"[{idx + 1}/{len(df)}] id={listing_id} geocoding: {query}")
            geo = geocode_address(query=query, api_key=api_key)
            cache[query] = geo
            unique_new_calls += 1

            # Save progressively so you do not lose paid/API work if interrupted.
            if unique_new_calls % 25 == 0:
                save_cache(cache, cache_csv)

            time.sleep(sleep_seconds)

        for key, value in geo.items():
            out[key] = value

        lat = out.get("latitude")
        lon = out.get("longitude")

        try:
            if pd.notna(lat) and pd.notna(lon):
                out["distance_to_western_km"] = round(
                    haversine_km(float(lat), float(lon), WESTERN_LAT, WESTERN_LON),
                    3,
                )
            else:
                out["distance_to_western_km"] = None
        except Exception:
            out["distance_to_western_km"] = None

        rows.append(out)

    if not cache_only or unique_new_calls:
        save_cache(cache, cache_csv)

    out_df = pd.DataFrame(rows)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(output_csv, index=False)

    print(f"\nSaved geocoded listings -> {output_csv}")
    print(f"Rows: {len(out_df)}")
    print(f"New Geoapify calls this run: {unique_new_calls}")
    print("Geocode status counts:")
    if "geocode_status" in out_df:
        print(out_df["geocode_status"].value_counts(dropna=False))

    confidence = pd.to_numeric(
        out_df.get(
            "geocode_confidence", pd.Series(index=out_df.index, dtype=float)
        ),
        errors="coerce",
    )
    status = out_df.get(
        "geocode_status", pd.Series(index=out_df.index, dtype=object)
    ).astype(str)
    computed_stats = {
        "cache_hit_count": cache_hits,
        "new_api_call_count": unique_new_calls,
        "cache_miss_count": cache_misses,
        "missing_address_count": int(status.eq("missing_address").sum()),
        "failed_geocode_count": int(status.isin(["error", "not_found", "cache_miss"]).sum()),
        "low_confidence_count": int((confidence < confidence_threshold).sum()),
    }
    if stats is not None:
        stats.update(computed_stats)

    return out_df


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Geocode UWO housing listings with Geoapify.")
    parser.add_argument(
        "input_csv",
        nargs="?",
        type=Path,
        help="Input enriched CSV, usually data/processed/stage2_ai_enriched_reviewed.csv",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
    )
    parser.add_argument(
        "--cache-csv",
        type=Path,
        default=DEFAULT_CACHE_PATH,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process first N rows for testing.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.15,
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--refresh-errors", action="store_true")
    parser.add_argument("--refresh-low-confidence", action="store_true")
    parser.add_argument(
        "--confidence-threshold", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.resume and args.overwrite:
        parser.error("--resume and --overwrite are mutually exclusive")
    if (args.resume or args.overwrite) and args.run_dir is None:
        parser.error("--resume and --overwrite require --run-dir")
    if not 0 <= args.confidence_threshold <= 1:
        parser.error("--confidence-threshold must be between 0 and 1")

    context = None
    if args.run_dir is not None:
        if args.input_csv is not None or args.output_csv is not None:
            parser.error("Explicit input/output paths cannot be combined with --run-dir")
        configuration = {
            "cache_path": str(args.cache_csv.resolve()),
            "cache_only": args.cache_only,
            "refresh_errors": args.refresh_errors,
            "refresh_low_confidence": args.refresh_low_confidence,
            "confidence_threshold": args.confidence_threshold,
            "limit": args.limit,
            "sleep_seconds": args.sleep_seconds,
        }
        context = RunContext.open_for_cli(
            args.run_dir,
            resume=args.resume,
            overwrite=args.overwrite,
            command=sys.argv,
            configuration={"stage3": configuration},
        )
        input_csv = select_run_input(context)
        output_csv = context.paths.stage3_geocoded
        context.ensure_outputs_available(
            [output_csv], allow_existing=args.resume or args.overwrite
        )
        context.manifest["configuration"]["stage3"] = {
            **configuration,
            "selected_input": str(input_csv),
        }
        context.start_stage("stage3", input_paths=[input_csv], output_paths=[output_csv])
    else:
        if args.input_csv is None:
            parser.error("input_csv is required unless --run-dir is used")
        input_csv = args.input_csv
        output_csv = args.output_csv or Path("data/processed/stage3_geocoded_listings.csv")

    stats: Dict[str, int] = {}
    try:
        load_dotenv(PROJECT_ROOT / ".env")
        api_key = os.getenv("GEOAPIFY_API_KEY")
        if not api_key and not args.cache_only:
            raise RuntimeError(
                "Missing GEOAPIFY_API_KEY environment variable. "
                "Set it before running Stage 3."
            )
        input_rows = len(pd.read_csv(input_csv))
        if args.limit is not None:
            input_rows = min(input_rows, args.limit)
        out_df = apply_geocoding(
            input_csv=input_csv,
            output_csv=output_csv,
            cache_csv=args.cache_csv,
            api_key=api_key,
            limit=args.limit,
            sleep_seconds=args.sleep_seconds,
            cache_only=args.cache_only,
            refresh_errors=args.refresh_errors,
            refresh_low_confidence=args.refresh_low_confidence,
            confidence_threshold=args.confidence_threshold,
            stats=stats,
        )
        if context is not None:
            warnings = []
            for key in (
                "cache_miss_count",
                "missing_address_count",
                "failed_geocode_count",
                "low_confidence_count",
            ):
                if stats.get(key, 0):
                    warnings.append(f"{key}={stats[key]}")
            context.finish_stage(
                "stage3",
                input_rows=input_rows,
                output_rows=len(out_df),
                warnings=warnings,
                error_count=stats.get("failed_geocode_count", 0),
                metrics=stats,
            )
    except Exception as exc:
        if context is not None:
            context.fail_stage("stage3", exc)
        raise


if __name__ == "__main__":
    main()
