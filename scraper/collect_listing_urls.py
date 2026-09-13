"""
collect_listing_urls.py

Stage 0 of the UWO housing pipeline.

The UWO listings site does not expose normal pagination links.
Browser behavior is:

1. POST /Listings/SaveFilters with PageNumber=N
2. GET  /Listings/SearchListings
3. Results page contains listing detail links

This scraper mimics that flow and collects all listing detail URLs.

Output CSV required by pipeline/uwo_listing_enricher.py:
- item_page_link
"""

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from pipeline.run_context import RunContext, validate_discovery
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline.run_context import RunContext, validate_discovery


BASE_URL = "https://offcampus.uwo.ca"
START_URL = "https://offcampus.uwo.ca/listings/"
SAVE_FILTERS_URL = "https://offcampus.uwo.ca/Listings/SaveFilters"
SEARCH_URL = "https://offcampus.uwo.ca/Listings/SearchListings"

DETAIL_RE = re.compile(r"/Listings/Details/\d+", re.IGNORECASE)


@dataclass(frozen=True)
class DiscoveryResult:
    urls: list[str]
    pages_requested: int
    repeated_page_detected: bool
    maximum_page_limit_reached: bool

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
    "Origin": "https://offcampus.uwo.ca",
    "Referer": "https://offcampus.uwo.ca/listings/",
}


def extract_listing_urls(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        if DETAIL_RE.search(href):
            urls.add(urljoin(BASE_URL, href))

    return urls


def build_save_filters_payload(page_number: int) -> list[tuple[str, str]]:
    """
    Builds the same type of form payload observed in the browser network log.

    PageNumber appears to be zero-indexed:
      PageNumber=0 = first results page
      PageNumber=1 = second results page
      etc.
    """

    data: list[tuple[str, str]] = [
        ("Desc", "true"),
        ("Sort", ""),
        ("PageNumber", str(page_number)),
        ("requestType", "Normal"),
        ("MinimumRent", "0"),
        ("MaximumRent", "7000"),
        ("NumberOfBedrooms", "0"),
        ("TenantTypeId", "0"),
        ("Distance", "0"),
        ("Posted", "0"),
    ]

    # Select all known locations.
    # Your capture showed SelectedLocations repeated many times when all locations were active.
    location_ids = [
        "2",   # Downtown
        "1",   # Old North
        "3",   # Near South
        "4",   # Near West
        "5",   # Whitehills
        "6",   # Masonville
        "7",   # North London
        "8",   # Kipps Lane
        "10",  # East London
        "12",  # Old South
        "11",  # S. E. London
        "14",  # West London
        "13",  # S. W. London
        "9",   # N. E. London
        "15",  # Outside London
        "16",  # On Campus
    ]

    for location_id in location_ids:
        data.append(("SelectedLocations", location_id))

    # The captured browser request always included amenity IDs and IsSelected=false.
    amenity_ids = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "17"]

    for i, amenity_id in enumerate(amenity_ids):
        data.append((f"AmenitiesCheckList[{i}].Id", amenity_id))

    # The browser request includes another Desc=false later in the body.
    # We mimic it exactly because ASP.NET MVC model binding may care about ordering.
    data.append(("Desc", "false"))

    for i in range(len(amenity_ids)):
        data.append((f"AmenitiesCheckList[{i}].IsSelected", "false"))

    return data


def fetch_results_page(session: requests.Session, page_number: int, timeout: int = 30) -> str:
    payload = build_save_filters_payload(page_number)

    save_response = session.post(
        SAVE_FILTERS_URL,
        data=payload,
        timeout=timeout,
        headers={
            **HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    save_response.raise_for_status()

    search_response = session.get(
        SEARCH_URL,
        timeout=timeout,
        headers=HEADERS,
    )
    search_response.raise_for_status()

    return search_response.text


def collect_listing_urls_with_metadata(
    max_pages: int = 100, delay: float = 0.25
) -> DiscoveryResult:
    session = requests.Session()
    session.headers.update(HEADERS)

    # Start session and cookies.
    print(f"Opening session: {START_URL}")
    start_response = session.get(START_URL, timeout=30)
    start_response.raise_for_status()

    all_urls: set[str] = set()
    seen_page_signatures: set[tuple[str, ...]] = set()
    pages_requested = 0
    repeated_page_detected = False
    stopped_before_limit = False

    for page_number in range(max_pages):
        pages_requested += 1
        html = fetch_results_page(session, page_number=page_number)
        page_urls = extract_listing_urls(html)

        signature = tuple(sorted(page_urls))
        new_urls = page_urls - all_urls

        print(
            f"[page {page_number}] "
            f"found={len(page_urls)} "
            f"new={len(new_urls)} "
            f"total={len(all_urls | page_urls)}"
        )

        if not page_urls:
            print("No listings found on this page. Stopping.")
            stopped_before_limit = True
            break

        if signature in seen_page_signatures:
            print("Repeated result page detected. Stopping.")
            repeated_page_detected = True
            stopped_before_limit = True
            break

        seen_page_signatures.add(signature)
        all_urls.update(page_urls)

        if len(new_urls) == 0 and page_number > 0:
            print("No new URLs found. Stopping.")
            stopped_before_limit = True
            break

        time.sleep(delay)

    return DiscoveryResult(
        urls=sorted(all_urls),
        pages_requested=pages_requested,
        repeated_page_detected=repeated_page_detected,
        maximum_page_limit_reached=max_pages > 0 and not stopped_before_limit,
    )


def collect_listing_urls(max_pages: int = 100, delay: float = 0.25) -> list[str]:
    """Backward-compatible discovery API returning only listing URLs."""
    return collect_listing_urls_with_metadata(max_pages=max_pages, delay=delay).urls


def write_csv(urls: list[str], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["item_page_link"])
        writer.writeheader()

        for url in urls:
            writer.writerow({"item_page_link": url})

    print(f"\nSaved {len(urls)} unique listing URLs → {output_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect all UWO listing detail URLs.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/raw/listing_links.csv"),
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="New or explicitly resumed versioned run directory.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=100,
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.25,
    )
    parser.add_argument("--minimum-listings", type=int, default=1)
    parser.add_argument("--previous-successful-count", type=int)
    parser.add_argument("--substantial-drop-ratio", type=float, default=0.5)

    return parser


def main() -> None:
    parser = build_parser()

    args = parser.parse_args()

    if args.resume and args.overwrite:
        parser.error("--resume and --overwrite are mutually exclusive")
    if (args.resume or args.overwrite) and args.run_dir is None:
        parser.error("--resume and --overwrite require --run-dir")

    context = None
    output_path = args.output
    allow_existing = False

    if args.run_dir is not None:
        configuration = {
            "max_pages": args.max_pages,
            "delay": args.delay,
            "minimum_listings": args.minimum_listings,
            "previous_successful_count": args.previous_successful_count,
            "substantial_drop_ratio": args.substantial_drop_ratio,
        }
        context = RunContext.open_for_cli(
            args.run_dir,
            resume=args.resume,
            overwrite=args.overwrite,
            command=sys.argv,
            configuration={"stage0": configuration},
        )
        output_path = context.paths.stage0_listing_links
        allow_existing = args.resume or args.overwrite
        context.ensure_outputs_available([output_path], allow_existing=allow_existing)
        context.manifest["configuration"]["stage0"] = configuration
        context.start_stage("stage0", output_paths=[output_path])

    try:
        result = collect_listing_urls_with_metadata(
            max_pages=args.max_pages,
            delay=args.delay,
        )

        write_csv(result.urls, output_path)

        if context is not None:
            warnings = validate_discovery(
                len(result.urls),
                minimum_count=args.minimum_listings,
                previous_successful_count=args.previous_successful_count,
                substantial_drop_ratio=args.substantial_drop_ratio,
                repeated_page_detected=result.repeated_page_detected,
                maximum_page_limit_reached=result.maximum_page_limit_reached,
            )
            context.finish_stage(
                "stage0",
                output_rows=len(result.urls),
                warnings=warnings,
                metrics={
                    "pages_requested": result.pages_requested,
                    "repeated_page_detected": result.repeated_page_detected,
                    "maximum_page_limit_reached": result.maximum_page_limit_reached,
                    "discovered_listing_count": len(result.urls),
                },
            )
    except Exception as exc:
        if context is not None:
            context.fail_stage("stage0", exc)
        raise


if __name__ == "__main__":
    main()
