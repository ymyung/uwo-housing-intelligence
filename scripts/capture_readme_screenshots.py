"""Capture README screenshots from the local synthetic demo only."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect, sync_playwright


BASE_URL = os.getenv("SCREENSHOT_BASE_URL", "http://127.0.0.1:5173").rstrip("/")
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "images"
VIEWPORT = {"width": 1440, "height": 900}


def allow_local_only(route: Route, blocked_hosts: set[str]) -> None:
    parsed = urlparse(route.request.url)
    if parsed.scheme not in {"http", "https"}:
        route.continue_()
        return
    host = parsed.hostname
    if host in {"127.0.0.1", "localhost"}:
        route.continue_()
    else:
        blocked_hosts.add(host or "unknown-host")
        route.abort()


def load_demo(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    expect(page.locator(".listing-card").first).to_be_visible(timeout=15_000)
    expect(page.locator(".results-summary strong")).to_have_text("6")
    expect(page.locator(".leaflet-marker-icon").first).to_be_visible(timeout=10_000)


def capture() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        blocked_hosts: set[str] = set()
        context.route("**/*", lambda route: allow_local_only(route, blocked_hosts))
        page = context.new_page()

        load_demo(page)
        expect(page.locator(".listing-card")).to_have_count(6)
        page.screenshot(path=OUTPUT_DIR / "hero-overview.png")

        page.goto(BASE_URL, wait_until="domcontentloaded")
        expect(page.locator(".listing-card").first).to_be_visible(timeout=15_000)
        page.get_by_label("Maximum monthly rent").fill("1000")
        page.get_by_label("Bedrooms").select_option("2")
        expect(page.locator(".results-summary strong")).to_have_text("1", timeout=10_000)
        expect(page.locator("#listing-demo-same-stop")).to_be_visible()
        expect(page.get_by_label("Map results")).to_have_attribute(
            "data-map-listing-count", "1", timeout=10_000
        )
        page.screenshot(path=OUTPUT_DIR / "filtered-search.png")

        page.locator("#listing-demo-same-stop .card-select").click()
        detail = page.get_by_label("Selected listing details")
        expect(detail).to_be_visible(timeout=10_000)
        expect(detail).to_contain_text("104 Fixture Walk")
        expect(detail).to_contain_text("$950 per month")
        expect(detail.get_by_text("Why this listing?", exact=True)).to_be_visible()
        detail.get_by_role("button", name="Transit", exact=True).click()
        fixture_notice = detail.locator(".fixture-notice")
        expect(fixture_notice).to_be_visible(timeout=10_000)
        expect(detail.locator(".selected-transport-result")).to_contain_text(
            "Same-stop", timeout=10_000
        )
        fixture_notice.scroll_into_view_if_needed()
        page.screenshot(path=OUTPUT_DIR / "listing-intelligence.png")

        if blocked_hosts:
            raise RuntimeError(
                "Blocked non-loopback HTTP requests: "
                + ", ".join(sorted(blocked_hosts))
            )

        context.close()
        browser.close()


if __name__ == "__main__":
    capture()
