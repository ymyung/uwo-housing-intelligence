"""
extract_listing_images.py

Scrapes image URLs from UWO off-campus listing detail pages.

Input:
  data/processed/stage5_otp_travel_times.csv

Output:
  data/processed/stage5_otp_travel_times_with_images.csv

Adds:
  image_url
  image_urls
  image_scrape_status
  image_scrape_count

This version is stricter:
- Avoids Western/UWO/site logos
- Avoids social preview images like og:image when they are likely branding
- Prioritizes real listing/property/gallery/upload images
- Stores image URLs only, not actual image files
"""

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup


DEFAULT_INPUT = Path("data/processed/stage5_otp_travel_times.csv")
DEFAULT_OUTPUT = Path("data/processed/stage5_otp_travel_times_with_images.csv")

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

BAD_IMAGE_WORDS = [
    "logo",
    "favicon",
    "icon",
    "sprite",
    "placeholder",
    "avatar",
    "facebook",
    "twitter",
    "instagram",
    "linkedin",
    "youtube",
    "print",
    "email",
    "map-marker",
    "marker",
    "western-logo",
    "westernu-logo",
    "uwo-logo",
    "western-shield",
    "shield",
    "brand",
    "branding",
    "header",
    "footer",
    "navbar",
    "nav-logo",
    "site-logo",
    "off-campus-logo",
    "offcampus-logo",
    "westernuniversity",
    "western-university-logo",
    "westernu",
    "university-logo",
    "crest",
]

GOOD_IMAGE_WORDS = [
    "listing",
    "listings",
    "property",
    "properties",
    "rental",
    "rentals",
    "photo",
    "photos",
    "image",
    "images",
    "upload",
    "uploads",
    "gallery",
    "house",
    "apartment",
    "unit",
    "room",
    "bedroom",
]


def clean_value(value: Any) -> Optional[str]:
    if pd.isna(value):
        return None

    text = str(value).strip()

    if not text or text.lower() in {"nan", "none", "null"}:
        return None

    return text


def normalize_url(raw_url: str, base_url: str) -> Optional[str]:
    if not raw_url:
        return None

    url = raw_url.strip().strip('"').strip("'")

    if not url:
        return None

    if url.startswith("data:"):
        return None

    if url.startswith("//"):
        url = "https:" + url

    url = urljoin(base_url, url)

    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        return None

    return url


def split_srcset(srcset: str) -> List[str]:
    urls = []

    for part in srcset.split(","):
        chunk = part.strip()

        if not chunk:
            continue

        url = chunk.split()[0].strip()

        if url:
            urls.append(url)

    return urls


def looks_like_image_url(url: str) -> bool:
    lower = url.lower()
    path = urlparse(lower).path

    if path.endswith(IMAGE_EXTENSIONS):
        return True

    if any(word in lower for word in GOOD_IMAGE_WORDS):
        return True

    return False


def is_bad_image_url(url: str) -> bool:
    lower = url.lower()
    path = urlparse(lower).path

    if any(word in lower for word in BAD_IMAGE_WORDS):
        return True

    bad_static_patterns = [
        "/content/images/",
        "/content/img/",
        "/assets/",
        "/static/",
        "/images/logo",
        "/img/logo",
        "/sitefinity/",
        "/bundles/",
        "/css/",
        "/scripts/",
    ]

    if any(pattern in lower for pattern in bad_static_patterns):
        # Static folders are usually logos/site assets.
        # Only allow if the URL also strongly suggests listing/property media.
        strong_listing_words = [
            "listing",
            "property",
            "rental",
            "upload",
            "uploads",
            "gallery",
            "photo",
            "photos",
        ]

        if not any(word in lower for word in strong_listing_words):
            return True

    if path.endswith((".svg", ".ico")):
        return True

    return False


def score_image_url(url: str, source: str, order: int) -> int:
    """
    Higher score = more likely to be an actual listing/property photo.
    Strongly penalizes logos, headers, and social preview images.
    """
    lower = url.lower()
    path = urlparse(lower).path

    score = 0

    if source in {"img", "srcset", "background", "raw"}:
        score += 35

    # Social preview images are often site logos, so do not trust them.
    if source in {"og:image", "twitter:image", "twitter:image:src"}:
        score -= 60

    if source == "jsonld":
        score += 5

    for word in GOOD_IMAGE_WORDS:
        if word in lower:
            score += 20

    if path.endswith((".jpg", ".jpeg", ".webp")):
        score += 10

    if path.endswith(".png"):
        score -= 5

    if is_bad_image_url(url):
        score -= 200

    score -= min(order, 20)

    return score


def dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    output = []

    for item in items:
        key = item.strip()

        if not key:
            continue

        if key in seen:
            continue

        seen.add(key)
        output.append(key)

    return output


def collect_from_jsonld(soup: BeautifulSoup, base_url: str) -> List[str]:
    urls = []

    def walk(obj: Any):
        if isinstance(obj, dict):
            for _, value in obj.items():
                walk(value)

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

        elif isinstance(obj, str):
            candidate = normalize_url(obj, base_url)

            if candidate and looks_like_image_url(candidate):
                urls.append(candidate)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.get_text(strip=True))
            walk(data)
        except Exception:
            continue

    return urls


def extract_image_urls_from_html(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")

    candidates = []
    order = 0

    def add_candidate(raw_url: Optional[str], source: str):
        nonlocal order

        if not raw_url:
            return

        normalized = normalize_url(raw_url, base_url)

        if not normalized:
            return

        if not looks_like_image_url(normalized):
            return

        candidates.append(
            {
                "url": normalized,
                "source": source,
                "order": order,
                "score": score_image_url(normalized, source, order),
            }
        )

        order += 1

    # Meta images are often logos/social previews, but still collect them with low score.
    for prop in ["og:image", "og:image:url", "twitter:image", "twitter:image:src"]:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )

        if tag:
            add_candidate(tag.get("content"), prop)

    # Regular image tags.
    for img in soup.find_all("img"):
        for attr in [
            "src",
            "data-src",
            "data-original",
            "data-lazy-src",
            "data-url",
            "data-image",
            "data-full",
            "data-large",
        ]:
            add_candidate(img.get(attr), "img")

        srcset = img.get("srcset") or img.get("data-srcset")

        if srcset:
            for srcset_url in split_srcset(srcset):
                add_candidate(srcset_url, "srcset")

    # Links to image files.
    for a in soup.find_all("a"):
        add_candidate(a.get("href"), "link")

    # CSS background images.
    for tag in soup.find_all(style=True):
        style = tag.get("style") or ""
        matches = re.findall(r"url\((['\"]?)(.*?)\1\)", style)

        for _, raw_url in matches:
            add_candidate(raw_url, "background")

    # JSON-LD images.
    for url in collect_from_jsonld(soup, base_url):
        add_candidate(url, "jsonld")

    # Raw embedded image URLs.
    raw_matches = re.findall(
        r"https?://[^\"'\s<>]+\.(?:jpg|jpeg|png|webp|gif)(?:\?[^\"'\s<>]*)?",
        html,
        flags=re.IGNORECASE,
    )

    for raw_url in raw_matches:
        add_candidate(raw_url, "raw")

    filtered = [
        candidate
        for candidate in candidates
        if candidate["score"] > 0 and not is_bad_image_url(candidate["url"])
    ]

    filtered.sort(key=lambda item: item["score"], reverse=True)

    urls = dedupe_preserve_order(candidate["url"] for candidate in filtered)

    return urls


def fetch_html(session: requests.Session, url: str, timeout: int = 25) -> str:
    last_error = None

    for attempt in range(3):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as exc:
            last_error = exc
            time.sleep(1 + attempt)

    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def scrape_images_for_listing(
    session: requests.Session,
    listing_url: str,
) -> dict:
    try:
        html = fetch_html(session, listing_url)
        image_urls = extract_image_urls_from_html(html, listing_url)

        return {
            "image_url": image_urls[0] if image_urls else None,
            "image_urls": "|".join(image_urls) if image_urls else None,
            "image_scrape_status": "ok" if image_urls else "no_listing_images_found",
            "image_scrape_count": len(image_urls),
        }

    except Exception as exc:
        return {
            "image_url": None,
            "image_urls": None,
            "image_scrape_status": f"error: {type(exc).__name__}: {exc}",
            "image_scrape_count": 0,
        }


def apply_image_scrape(
    input_csv: Path,
    output_csv: Path,
    limit: Optional[int],
    sleep_seconds: float,
    overwrite: bool,
) -> pd.DataFrame:
    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    df = pd.read_csv(input_csv)

    if "listing_url" not in df.columns:
        raise ValueError("Input CSV must contain listing_url")

    if "listing_id" not in df.columns:
        raise ValueError("Input CSV must contain listing_id")

    if limit is not None:
        df = df.head(limit).copy()
    else:
        df = df.copy()

    for col in ["image_url", "image_urls", "image_scrape_status", "image_scrape_count"]:
        if col not in df.columns:
            df[col] = None

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    for idx, row in df.iterrows():
        listing_id = row.get("listing_id")
        listing_url = clean_value(row.get("listing_url"))

        if not listing_url:
            df.at[idx, "image_scrape_status"] = "missing_listing_url"
            df.at[idx, "image_scrape_count"] = 0
            continue

        if not overwrite and clean_value(row.get("image_url")):
            continue

        print(f"[{idx + 1}/{len(df)}] Scraping images listing_id={listing_id}")

        result = scrape_images_for_listing(session, listing_url)

        for key, value in result.items():
            df.at[idx, key] = value

        time.sleep(sleep_seconds)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False)

    print(f"\nSaved image-enriched CSV → {output_csv}")
    print(f"Rows: {len(df)}")

    print("\nImage scrape status counts:")
    print(df["image_scrape_status"].value_counts(dropna=False))

    print("\nRows with primary image:")
    print(df["image_url"].notna().sum())

    return df


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape listing/property image URLs from UWO listing detail pages."
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
        "--limit",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.15,
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Rescrape even if image_url already exists.",
    )

    args = parser.parse_args()

    apply_image_scrape(
        input_csv=args.input_csv,
        output_csv=args.output_csv,
        limit=args.limit,
        sleep_seconds=args.sleep_seconds,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    main()