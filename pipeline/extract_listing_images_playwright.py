"""
extract_listing_images_playwright.py

Scrapes actual rendered carousel/gallery images from UWO listing pages.

Targets pages where images appear as:
- large main gallery image
- row of thumbnails
- left/right carousel arrows

Output columns:
  image_url
  image_urls
  image_scrape_status
  image_scrape_count

This version caps saved images per listing to MAX_IMAGES_PER_LISTING so
image_urls does not contain 20+ repeated thumbnails/resources.
"""

import argparse
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

import pandas as pd
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


DEFAULT_INPUT = Path("data/processed/stage5_otp_travel_times.csv")
DEFAULT_OUTPUT = Path("data/processed/stage5_otp_travel_times_with_images.csv")

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
MAX_IMAGES_PER_LISTING = 6

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
    "carousel",
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

    url = str(raw_url).strip().strip('"').strip("'")

    if not url:
        return None

    if url.startswith("data:") or url.startswith("blob:"):
        return None

    if url.startswith("//"):
        url = "https:" + url

    url = urljoin(base_url, url)

    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        return None

    return url


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

    if path.endswith((".svg", ".ico")):
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

    return False


def image_dedupe_key(url: str) -> str:
    """
    Normalize image URL so repeated thumbnails/variants do not count separately.
    """
    parsed = urlparse(url)
    path = parsed.path.lower()
    filename = path.split("/")[-1]

    filename = re.sub(r"(_thumb|_thumbnail|thumb)", "", filename)
    filename = re.sub(r"[-_]\d+x\d+", "", filename)
    filename = re.sub(r"@\d+x", "", filename)

    return filename or path


def score_candidate(candidate: Dict[str, Any]) -> int:
    url = candidate["url"]
    source = candidate.get("source", "")
    width = int(candidate.get("width") or 0)
    height = int(candidate.get("height") or 0)
    rendered_width = int(candidate.get("rendered_width") or 0)
    rendered_height = int(candidate.get("rendered_height") or 0)
    order = int(candidate.get("order") or 0)

    lower = url.lower()
    path = urlparse(lower).path

    if is_bad_image_url(url):
        return -999

    score = 0

    if source.startswith("main-gallery"):
        score += 120

    if source.startswith("thumbnail-click-main"):
        score += 115

    if source.startswith("next-click-main"):
        score += 110

    if source in {"visible-img", "visible-background", "visible-img-after-scroll"}:
        score += 70

    if source in {"dom-img", "dom-picture"}:
        score += 40

    if source in {"network-image", "performance-resource"}:
        score += 20

    for word in GOOD_IMAGE_WORDS:
        if word in lower:
            score += 16

    if path.endswith((".jpg", ".jpeg", ".webp")):
        score += 14

    if path.endswith(".png"):
        score -= 6

    if rendered_width >= 450 and rendered_height >= 250:
        score += 80
    elif rendered_width >= 250 and rendered_height >= 150:
        score += 35
    elif rendered_width and rendered_height and (
        rendered_width < 80 or rendered_height < 60
    ):
        score -= 70

    if width >= 700 and height >= 400:
        score += 55
    elif width >= 350 and height >= 220:
        score += 25
    elif width and height and (width < 100 or height < 80):
        score -= 70

    score -= min(order, 30)

    return score


def dedupe_candidates(candidates: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    output = []

    for candidate in candidates:
        url = candidate.get("url")

        if not url:
            continue

        key = url.strip()

        if key in seen:
            continue

        seen.add(key)
        output.append(candidate)

    return output


def final_gallery_urls(scored_candidates: List[Dict[str, Any]]) -> List[str]:
    """
    Keep only best unique gallery images.
    Prevents every thumbnail/preload/repeated resource from being saved.
    """
    urls = []
    seen_urls = set()
    seen_keys = set()

    for item in scored_candidates:
        url = item.get("url")

        if not url:
            continue

        clean_url = url.strip()
        clean_url_no_query = clean_url.split("?")[0].lower()
        key = image_dedupe_key(clean_url)

        if clean_url_no_query in seen_urls:
            continue

        if key in seen_keys:
            continue

        seen_urls.add(clean_url_no_query)
        seen_keys.add(key)
        urls.append(clean_url)

        if len(urls) >= MAX_IMAGES_PER_LISTING:
            break

    return urls


def normalize_candidates(raw_candidates: List[Dict[str, Any]], base_url: str) -> List[Dict[str, Any]]:
    normalized = []

    for item in raw_candidates:
        url = normalize_url(item.get("url"), base_url)

        if not url:
            continue

        if not looks_like_image_url(url):
            continue

        if is_bad_image_url(url):
            continue

        normalized.append(
            {
                "url": url,
                "source": item.get("source") or "unknown",
                "width": item.get("width") or 0,
                "height": item.get("height") or 0,
                "rendered_width": item.get("rendered_width") or 0,
                "rendered_height": item.get("rendered_height") or 0,
                "order": item.get("order") or 0,
            }
        )

    return normalized


def collect_rendered_image_candidates(page, base_url: str, source_label: str) -> List[Dict[str, Any]]:
    raw_candidates = page.evaluate(
        """
        (sourceLabel) => {
          const out = [];
          let order = 0;

          function isVisible(el) {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);

            return (
              rect.width > 0 &&
              rect.height > 0 &&
              style.display !== "none" &&
              style.visibility !== "hidden" &&
              Number(style.opacity || 1) > 0
            );
          }

          function add(url, source, width = 0, height = 0, renderedWidth = 0, renderedHeight = 0) {
            if (!url) return;

            out.push({
              url: String(url),
              source,
              width: Number(width) || 0,
              height: Number(height) || 0,
              rendered_width: Math.round(Number(renderedWidth) || 0),
              rendered_height: Math.round(Number(renderedHeight) || 0),
              order: order++
            });
          }

          function addSrcset(srcset, source, width = 0, height = 0, renderedWidth = 0, renderedHeight = 0) {
            if (!srcset) return;

            String(srcset).split(",").forEach(part => {
              const url = part.trim().split(/\\s+/)[0];
              if (url) add(url, source, width, height, renderedWidth, renderedHeight);
            });
          }

          document.querySelectorAll("img").forEach(img => {
            const rect = img.getBoundingClientRect();

            const width = img.naturalWidth || img.width || 0;
            const height = img.naturalHeight || img.height || 0;
            const renderedWidth = rect.width || 0;
            const renderedHeight = rect.height || 0;

            const visible = isVisible(img);
            const source = visible ? sourceLabel : "dom-img";

            [
              img.currentSrc,
              img.src,
              img.getAttribute("data-src"),
              img.getAttribute("data-original"),
              img.getAttribute("data-lazy-src"),
              img.getAttribute("data-url"),
              img.getAttribute("data-image"),
              img.getAttribute("data-full"),
              img.getAttribute("data-large")
            ].forEach(url => add(url, source, width, height, renderedWidth, renderedHeight));

            addSrcset(img.getAttribute("srcset"), source, width, height, renderedWidth, renderedHeight);
            addSrcset(img.getAttribute("data-srcset"), source, width, height, renderedWidth, renderedHeight);
          });

          document.querySelectorAll("source").forEach(sourceEl => {
            addSrcset(sourceEl.getAttribute("srcset"), "dom-picture", 0, 0, 0, 0);
            addSrcset(sourceEl.getAttribute("data-srcset"), "dom-picture", 0, 0, 0, 0);
          });

          document.querySelectorAll("*").forEach(el => {
            if (!isVisible(el)) return;

            const rect = el.getBoundingClientRect();

            if (rect.width < 180 || rect.height < 100) return;

            const style = window.getComputedStyle(el);
            const bg = style && style.backgroundImage;

            if (!bg || bg === "none") return;

            const matches = [...bg.matchAll(/url\\((['"]?)(.*?)\\1\\)/g)];

            matches.forEach(match => {
              add(match[2], "visible-background", 0, 0, rect.width, rect.height);
            });
          });

          return out;
        }
        """,
        source_label,
    )

    return normalize_candidates(raw_candidates, base_url)


def collect_main_gallery_image(page, base_url: str, source_label: str) -> List[Dict[str, Any]]:
    raw_candidates = page.evaluate(
        """
        (sourceLabel) => {
          const out = [];
          let best = null;

          function addCandidate(url, img, rect) {
            if (!url) return;

            const area = rect.width * rect.height;

            const candidate = {
              url: String(url),
              source: sourceLabel,
              width: Number(img.naturalWidth || img.width || 0),
              height: Number(img.naturalHeight || img.height || 0),
              rendered_width: Math.round(Number(rect.width || 0)),
              rendered_height: Math.round(Number(rect.height || 0)),
              order: 0,
              area
            };

            if (!best || candidate.area > best.area) {
              best = candidate;
            }
          }

          document.querySelectorAll("img").forEach(img => {
            const rect = img.getBoundingClientRect();
            const style = window.getComputedStyle(img);

            if (
              rect.width < 220 ||
              rect.height < 140 ||
              style.display === "none" ||
              style.visibility === "hidden" ||
              Number(style.opacity || 1) <= 0
            ) {
              return;
            }

            addCandidate(img.currentSrc, img, rect);
            addCandidate(img.src, img, rect);
            addCandidate(img.getAttribute("data-src"), img, rect);
            addCandidate(img.getAttribute("data-full"), img, rect);
            addCandidate(img.getAttribute("data-large"), img, rect);
          });

          if (best) out.push(best);

          return out;
        }
        """,
        source_label,
    )

    return normalize_candidates(raw_candidates, base_url)


def click_thumbnail_images(page, base_url: str, max_clicks: int = 20) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []

    count = page.locator("img").count()
    clicked = 0

    for i in range(min(count, 80)):
        if clicked >= max_clicks:
            break

        img = page.locator("img").nth(i)

        try:
            info = img.evaluate(
                """
                (el) => {
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);

                  return {
                    visible: rect.width > 0 && rect.height > 0 &&
                             style.display !== "none" &&
                             style.visibility !== "hidden" &&
                             Number(style.opacity || 1) > 0,
                    rendered_width: rect.width,
                    rendered_height: rect.height,
                    natural_width: el.naturalWidth || el.width || 0,
                    natural_height: el.naturalHeight || el.height || 0,
                    src: el.currentSrc || el.src || el.getAttribute("data-src") || ""
                  };
                }
                """
            )
        except Exception:
            continue

        if not info.get("visible"):
            continue

        rendered_width = float(info.get("rendered_width") or 0)
        rendered_height = float(info.get("rendered_height") or 0)
        src = normalize_url(info.get("src") or "", base_url)

        if not src or is_bad_image_url(src):
            continue

        is_thumbnail_shape = 35 <= rendered_width <= 180 and 35 <= rendered_height <= 140

        if not is_thumbnail_shape:
            continue

        try:
            img.evaluate(
                """
                (el) => {
                  const clickable = el.closest("a, button, [role='button'], li, div");
                  if (clickable) clickable.click();
                  else el.click();
                }
                """
            )

            page.wait_for_timeout(650)
            clicked += 1

            candidates.extend(
                collect_main_gallery_image(
                    page,
                    base_url,
                    source_label=f"thumbnail-click-main-{clicked}",
                )
            )

        except Exception:
            continue

    return candidates


def click_next_arrows(page, base_url: str, max_clicks: int = 12) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []

    for click_number in range(max_clicks):
        try:
            clicked = page.evaluate(
                """
                () => {
                  function isVisible(el) {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                           style.display !== "none" &&
                           style.visibility !== "hidden" &&
                           Number(style.opacity || 1) > 0;
                  }

                  const els = Array.from(document.querySelectorAll("button, a, span, div"));

                  const candidates = els.filter(el => {
                    if (!isVisible(el)) return false;

                    const text = (el.innerText || el.textContent || "").trim().toLowerCase();
                    const cls = String(el.className || "").toLowerCase();
                    const aria = String(el.getAttribute("aria-label") || "").toLowerCase();
                    const title = String(el.getAttribute("title") || "").toLowerCase();

                    const signal = `${text} ${cls} ${aria} ${title}`;

                    return (
                      signal.includes("next") ||
                      signal.includes("right") ||
                      signal.includes("carousel-control-next") ||
                      signal.includes("slick-next") ||
                      signal.includes("swiper-button-next") ||
                      text === ">" ||
                      text === "›" ||
                      text === "»"
                    );
                  });

                  if (!candidates.length) return false;

                  candidates.sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    return (br.width * br.height) - (ar.width * ar.height);
                  });

                  candidates[0].click();
                  return true;
                }
                """
            )
        except Exception:
            clicked = False

        if not clicked:
            break

        page.wait_for_timeout(750)

        candidates.extend(
            collect_main_gallery_image(
                page,
                base_url,
                source_label=f"next-click-main-{click_number + 1}",
            )
        )

    return candidates


def scroll_page(page, rounds: int = 4) -> None:
    for _ in range(rounds):
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(450)

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)


def collect_network_candidates(page, base_url: str) -> List[Dict[str, Any]]:
    raw_candidates = page.evaluate(
        """
        () => {
          const out = [];
          let order = 0;

          if (performance && performance.getEntriesByType) {
            performance.getEntriesByType("resource").forEach(entry => {
              const url = entry.name || "";

              out.push({
                url,
                source: "performance-resource",
                width: 0,
                height: 0,
                rendered_width: 0,
                rendered_height: 0,
                order: order++
              });
            });
          }

          return out;
        }
        """
    )

    return normalize_candidates(raw_candidates, base_url)


def scrape_listing_images_with_page(page, listing_url: str, timeout_ms: int) -> Dict[str, Any]:
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=timeout_ms)

        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(1500)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)

        candidates: List[Dict[str, Any]] = []

        candidates.extend(
            collect_main_gallery_image(
                page,
                listing_url,
                source_label="main-gallery-initial",
            )
        )

        candidates.extend(
            collect_rendered_image_candidates(
                page,
                listing_url,
                source_label="visible-img",
            )
        )

        candidates.extend(click_thumbnail_images(page, listing_url, max_clicks=24))
        candidates.extend(click_next_arrows(page, listing_url, max_clicks=15))

        scroll_page(page)

        candidates.extend(
            collect_rendered_image_candidates(
                page,
                listing_url,
                source_label="visible-img-after-scroll",
            )
        )

        candidates.extend(collect_network_candidates(page, listing_url))

        deduped = dedupe_candidates(candidates)

        scored = []

        for candidate in deduped:
            score = score_candidate(candidate)

            if score <= 0:
                continue

            candidate["score"] = score
            scored.append(candidate)

        scored.sort(key=lambda item: item["score"], reverse=True)

        urls = final_gallery_urls(scored)

        return {
            "image_url": urls[0] if urls else None,
            "image_urls": "|".join(urls) if urls else None,
            "image_scrape_status": "ok" if urls else "no_listing_images_found",
            "image_scrape_count": len(urls),
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
    start_index: int,
    sleep_seconds: float,
    overwrite: bool,
    headed: bool,
    timeout_ms: int,
    save_every: int,
) -> pd.DataFrame:
    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    df = pd.read_csv(input_csv)

    if "listing_url" not in df.columns:
        raise ValueError("Input CSV must contain listing_url")

    if "listing_id" not in df.columns:
        raise ValueError("Input CSV must contain listing_id")

    if limit is not None:
        df = df.iloc[start_index : start_index + limit].copy()
    elif start_index:
        df = df.iloc[start_index:].copy()
    else:
        df = df.copy()

    for col in ["image_url", "image_urls", "image_scrape_status", "image_scrape_count"]:
        if col not in df.columns:
            df[col] = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)

        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        for local_idx, (idx, row) in enumerate(df.iterrows(), start=1):
            listing_id = clean_value(row.get("listing_id"))
            listing_url = clean_value(row.get("listing_url"))

            if not listing_url:
                df.at[idx, "image_scrape_status"] = "missing_listing_url"
                df.at[idx, "image_scrape_count"] = 0
                continue

            if not overwrite and clean_value(row.get("image_url")):
                continue

            print(f"[{local_idx}/{len(df)}] Scraping gallery images listing_id={listing_id}")

            result = scrape_listing_images_with_page(
                page=page,
                listing_url=listing_url,
                timeout_ms=timeout_ms,
            )

            for key, value in result.items():
                df.at[idx, key] = value

            if save_every > 0 and local_idx % save_every == 0:
                output_csv.parent.mkdir(parents=True, exist_ok=True)
                df.to_csv(output_csv, index=False)
                print(f"Progress saved → {output_csv}")

            time.sleep(sleep_seconds)

        context.close()
        browser.close()

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False)

    print(f"\nSaved rendered image CSV → {output_csv}")
    print(f"Rows: {len(df)}")

    print("\nImage scrape status counts:")
    print(df["image_scrape_status"].value_counts(dropna=False))

    print("\nRows with primary image:")
    print(df["image_url"].notna().sum())

    return df


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape rendered listing carousel/gallery image URLs from UWO listing pages."
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
        "--start-index",
        type=int,
        default=0,
    )

    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.2,
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
    )

    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser while scraping. Useful for debugging.",
    )

    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=30000,
    )

    parser.add_argument(
        "--save-every",
        type=int,
        default=25,
    )

    args = parser.parse_args()

    apply_image_scrape(
        input_csv=args.input_csv,
        output_csv=args.output_csv,
        limit=args.limit,
        start_index=args.start_index,
        sleep_seconds=args.sleep_seconds,
        overwrite=args.overwrite,
        headed=args.headed,
        timeout_ms=args.timeout_ms,
        save_every=args.save_every,
    )


if __name__ == "__main__":
    main()