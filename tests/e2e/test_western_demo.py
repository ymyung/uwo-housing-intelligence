"""Real-browser coverage for the essential Western demonstration journeys."""

from __future__ import annotations

import os
from pathlib import Path
import re
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect, sync_playwright


BASE_URL = os.getenv("E2E_BASE_URL", "").rstrip("/")
OUTPUT_DIR = Path(os.getenv("E2E_OUTPUT_DIR", "data/demo-e2e/test-results"))

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BASE_URL, reason="E2E_BASE_URL is not configured"),
]


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


def _allow_local_only(route) -> None:
    host = urlparse(route.request.url).hostname
    if host in {"127.0.0.1", "localhost"}:
        route.continue_()
    else:
        route.abort()


@pytest.fixture
def page(browser, request):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.route("**/*", _allow_local_only)
    current = context.new_page()
    try:
        yield current
    except Exception:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        current.screenshot(
            path=OUTPUT_DIR / f"{request.node.name}.png", full_page=True
        )
        raise
    finally:
        context.close()


def _load(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    expect(page.locator(".listing-card").first).to_be_visible(timeout=15_000)
    expect(page.get_by_text("Loading housing listings…")).to_have_count(0)


def _result_count(page: Page) -> int:
    return int(page.locator(".results-summary strong").inner_text())


def _search_listing(page: Page, listing_id: str, address: str) -> None:
    page.get_by_label("Search listings").fill(address)
    expect(page.locator(f"#listing-{listing_id}")).to_be_visible(timeout=10_000)


def test_discovery_roommates_and_empty_state(page: Page) -> None:
    _load(page)
    initial = _result_count(page)

    page.get_by_role("button", name="Roommates Wanted").click()

    expect(page).to_have_url(re.compile(r"roommates_wanted=true"))
    expect(page.get_by_role("button", name="Roommates Wanted")).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.locator(".results-summary strong")).not_to_have_text(
        str(initial), timeout=10_000
    )
    assert 0 < _result_count(page) < initial

    page.get_by_label("Maximum monthly rent").fill("1")
    expect(page.get_by_text("No listings match these filters.")).to_be_visible(
        timeout=10_000
    )
    page.get_by_role("button", name="Reset filters").click()
    expect(page.locator(".listing-card").first).to_be_visible(timeout=10_000)


def test_sidebar_pagination_does_not_paginate_map_membership(page: Page) -> None:
    with page.expect_response(
        lambda response: "/api/listings/map?" in response.url
        and response.status == 200,
        timeout=15_000,
    ) as initial_map_response:
        _load(page)
    initial_map = initial_map_response.value.json()
    map_pane = page.get_by_label("Map results")
    expect(map_pane).to_have_attribute(
        "data-map-listing-count", str(initial_map["count"]), timeout=10_000
    )
    expect(page.locator(".results-summary")).to_contain_text(
        f"{_result_count(page)} results · {initial_map['count']} shown on map"
    )
    first_page_ids = set(
        page.locator(".listing-card").evaluate_all(
            "cards => cards.map(card => card.id.replace('listing-', ''))"
        )
    )
    map_ids = {row["listing_id"] for row in initial_map["listings"]}
    assert len(first_page_ids) == 100
    assert len(map_ids) == initial_map["count"]
    assert len(map_ids - first_page_ids) > 0

    map_requests: list[str] = []
    page.on(
        "request",
        lambda request: map_requests.append(request.url)
        if "/api/listings/map?" in request.url
        else None,
    )
    with page.expect_response(
        lambda response: "/api/listings?" in response.url
        and "page=2" in response.url
        and "/map?" not in response.url
        and response.status == 200
    ):
        page.get_by_role("button", name="Next").click()
    expect(page.get_by_text(re.compile(r"Page 2 of"))).to_be_visible()
    expect(page.locator(f"#listing-{next(iter(first_page_ids))}")).to_have_count(0)
    second_page_ids = set(
        page.locator(".listing-card").evaluate_all(
            "cards => cards.map(card => card.id.replace('listing-', ''))"
        )
    )
    assert first_page_ids != second_page_ids
    expect(map_pane).to_have_attribute(
        "data-map-listing-count", str(initial_map["count"])
    )
    with page.expect_response(
        lambda response: "/api/listings?" in response.url
        and "page=3" in response.url
        and "/map?" not in response.url
        and response.status == 200
    ):
        page.get_by_role("button", name="Next").click()
    expect(page.get_by_text(re.compile(r"Page 3 of"))).to_be_visible()
    expect(page.locator(f"#listing-{next(iter(second_page_ids))}")).to_have_count(0)
    third_page_ids = set(
        page.locator(".listing-card").evaluate_all(
            "cards => cards.map(card => card.id.replace('listing-', ''))"
        )
    )
    assert second_page_ids != third_page_ids
    expect(map_pane).to_have_attribute(
        "data-map-listing-count", str(initial_map["count"])
    )
    page.wait_for_timeout(500)
    assert map_requests == []

    coordinate_counts: dict[tuple[float, float], int] = {}
    for marker in initial_map["listings"]:
        key = (marker["latitude"], marker["longitude"])
        coordinate_counts[key] = coordinate_counts.get(key, 0) + 1
    off_page_marker = next(
        marker
        for marker in initial_map["listings"]
        if marker["listing_id"] not in third_page_ids
        and coordinate_counts[(marker["latitude"], marker["longitude"])] == 1
    )
    for _ in range(5):
        page.locator(".leaflet-control-zoom-in").click()
    marker = page.locator(
        f'.leaflet-marker-icon[title="Listing {off_page_marker["listing_id"]}"]'
    )
    expect(marker).to_have_count(1)
    marker.dispatch_event("click")
    detail = page.get_by_label("Selected listing details")
    expect(detail).to_be_visible(timeout=10_000)
    expect(detail).to_contain_text(off_page_marker["address"])

    with page.expect_response(
        lambda response: "/api/listings/map?" in response.url
        and "roommates_wanted=true" in response.url
        and response.status == 200
    ) as filtered_map_response:
        page.get_by_role("button", name="Roommates Wanted").click()
    filtered_map = filtered_map_response.value.json()
    assert 0 < filtered_map["count"] < initial_map["count"]
    expect(map_pane).to_have_attribute(
        "data-map-listing-count", str(filtered_map["count"]), timeout=10_000
    )

    with page.expect_response(
        lambda response: "/api/listings/map?" in response.url
        and "roommates_wanted=true" in response.url
        and "max_walk_minutes=20" in response.url
        and response.status == 200
    ) as walk_map_response:
        page.get_by_label("Walk to Western").select_option("20")
    walk_map = walk_map_response.value.json()
    assert 0 < walk_map["count"] <= filtered_map["count"]
    expect(map_pane).to_have_attribute(
        "data-map-listing-count", str(walk_map["count"]), timeout=10_000
    )

    page.locator(".more-filters > summary").click()
    with page.expect_response(
        lambda response: "/api/listings/map?" in response.url
        and "roommates_wanted" not in response.url
        and "max_walk_minutes" not in response.url
        and response.status == 200
    ):
        page.get_by_role("button", name="Reset all filters").click()
    expect(map_pane).to_have_attribute(
        "data-map-listing-count", str(initial_map["count"]), timeout=10_000
    )


def test_same_coordinate_cluster_exposes_each_listing(page: Page) -> None:
    with page.expect_response(
        lambda response: "/api/listings/map?" in response.url
        and response.status == 200,
        timeout=15_000,
    ) as map_response:
        _load(page)
    map_listings = map_response.value.json()["listings"]
    buckets: dict[tuple[float, float], list[dict]] = {}
    for listing in map_listings:
        key = (listing["latitude"], listing["longitude"])
        buckets.setdefault(key, []).append(listing)

    for _ in range(5):
        page.locator(".leaflet-control-zoom-in").click()
        page.wait_for_timeout(300)

    collision = next(
        group
        for group in buckets.values()
        if len(group) == 2
        and group[0].get("address")
        and group[0].get("address") == group[1].get("address")
        and page.get_by_title(
            f"2 listings at {group[0]['address']}", exact=True
        ).count() == 1
    )
    marker_title = f"2 listings at {collision[0]['address']}"

    def open_chooser():
        for _ in range(3):
            cluster_marker = page.get_by_title(marker_title, exact=True)
            expect(cluster_marker).to_have_count(1)
            cluster_marker.dispatch_event("click")
            page.wait_for_timeout(400)
            chooser = page.locator(".cluster-chooser")
            if chooser.count() and chooser.is_visible():
                return chooser
        raise AssertionError("same-coordinate cluster chooser did not open")

    chooser = open_chooser()
    expect(chooser.get_by_text("2 listings at this location", exact=True)).to_be_visible()

    for index, listing in enumerate(collision):
        listing_id = str(listing["listing_id"])
        with page.expect_response(
            lambda response, expected=listing_id: response.url.endswith(
                f"/api/listings/{expected}"
            )
            and response.status == 200,
            timeout=10_000,
        ):
            chooser.locator(f'button[data-listing-id="{listing_id}"]').click()
        detail = page.get_by_label("Selected listing details")
        expect(detail).to_be_visible(timeout=10_000)
        expect(detail).to_contain_text(listing["address"])
        detail.get_by_role("button", name="Close listing details").click()
        expect(detail).to_have_count(0)
        if index < len(collision) - 1:
            page.wait_for_timeout(600)
            chooser = open_chooser()


def test_commute_query_updates_and_opens_a_listing(page: Page) -> None:
    _load(page)

    page.get_by_label("Bedrooms").select_option("4")
    page.get_by_label("Walk to Western").select_option("20")

    expect(page).to_have_url(re.compile(r"bedrooms=4"), timeout=10_000)
    expect(page).to_have_url(re.compile(r"max_walk_minutes=20"))
    expect(page.locator(".listing-card").first).to_be_visible(timeout=10_000)
    assert _result_count(page) > 0
    page.locator(".listing-card .card-select").first.click()
    expect(page.get_by_label("Selected listing details")).to_be_visible(timeout=10_000)


def test_ranked_listing_explains_its_value(page: Page) -> None:
    _load(page)
    _search_listing(page, "55796", "1235 Richmond Street")
    card = page.locator("#listing-55796")
    expect(card.get_by_text("Overall match", exact=True)).to_be_visible()
    expect(card.get_by_text("79/100", exact=True)).to_be_visible()
    expect(card.get_by_text("Price advantage", exact=True)).to_be_visible()
    expect(card.get_by_text(
        "Rent is 26% below the median for listings in this comparison group",
        exact=True,
    )).to_be_visible()
    card.locator(".card-select").click()

    detail = page.get_by_label("Selected listing details")
    expect(detail.get_by_text("Overall match", exact=True)).to_be_visible(timeout=10_000)
    expect(detail.get_by_text("79/100", exact=True)).to_be_visible()
    expect(detail.get_by_text("Price advantage", exact=True)).to_be_visible()
    expect(detail.get_by_text(
        "Rent is 26% below the median for listings in this comparison group",
        exact=True,
    )).to_be_visible()
    expect(detail.get_by_text("Why this listing?", exact=True)).to_be_visible(
        timeout=10_000
    )


def test_saved_listings_survive_filter_changes_and_can_be_removed(page: Page) -> None:
    _load(page)
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(".listing-card").first).to_be_visible(timeout=15_000)

    _search_listing(page, "62609", "559 St George")
    page.locator("#listing-62609").get_by_role("button", name="Save", exact=True).click()
    _search_listing(page, "61598", "1112 Sunset Street")
    page.locator("#listing-61598").get_by_role("button", name="Save", exact=True).click()
    page.get_by_role("button", name=re.compile(r"^Saved 2$")).click()

    expect(page.get_by_text("Showing all saved listings.", exact=False)).to_be_visible()
    expect(page.locator("#listing-62609")).to_be_visible(timeout=10_000)
    expect(page.locator("#listing-61598")).to_be_visible()
    page.locator("#listing-62609").get_by_role("button", name="Saved", exact=True).click()
    expect(page.locator("#listing-62609")).to_have_count(0, timeout=10_000)
    expect(page.locator("#listing-61598")).to_be_visible()


def test_comparison_requires_two_and_aligns_student_decision_fields(page: Page) -> None:
    _load(page)
    compare = page.get_by_role("button", name=re.compile(r"^Compare 0$"))
    expect(compare).to_be_disabled()

    _search_listing(page, "63094", "1122 Sunset St")
    page.locator("#listing-63094").get_by_role(
        "button", name="Compare", exact=True
    ).click()
    expect(page.get_by_role("button", name=re.compile(r"^Compare 1$"))).to_be_disabled()
    _search_listing(page, "62609", "559 St George")
    page.locator("#listing-62609").get_by_role(
        "button", name="Compare", exact=True
    ).click()
    page.get_by_role("button", name=re.compile(r"^Compare 2$")).click()

    dialog = page.get_by_role("dialog", name="Compare listings")
    expect(dialog).to_be_visible()
    for label in (
        "Monthly price",
        "Utilities",
        "Walk to Western",
        "Morning transit to Western",
        "Overall match",
        "Value",
    ):
        expect(dialog.get_by_role("rowheader", name=label)).to_be_visible()
    expect(dialog.get_by_text("Option A", exact=True)).to_be_visible()
    expect(dialog.get_by_text("Option B", exact=True)).to_be_visible()
    expect(
        dialog.get_by_role("row").filter(has_text="Morning transit to Western")
    ).not_to_contain_text("Not specified")


def test_walking_surface_exact_route_and_back(page: Page) -> None:
    _load(page)
    _search_listing(page, "55796", "1235 Richmond Street")
    page.locator("#listing-55796 .card-select").click()
    page.get_by_role("button", name="Walk time").click()
    expect(page.get_by_text("Approximate walking area", exact=True)).to_be_visible(
        timeout=15_000
    )

    map_box = page.locator(".listing-map").bounding_box()
    assert map_box is not None
    action = page.get_by_role("button", name="Show exact route")
    for x_fraction, y_fraction in (
        (0.55, 0.50),
        (0.45, 0.55),
        (0.60, 0.42),
        (0.38, 0.45),
    ):
        page.mouse.click(
            map_box["x"] + map_box["width"] * x_fraction,
            map_box["y"] + map_box["height"] * y_fraction,
        )
        try:
            action.wait_for(state="visible", timeout=1_500)
            break
        except Exception:
            continue
    expect(action).to_be_visible()
    action.click()
    expect(page.get_by_role("button", name="Back to walking map")).to_be_visible(
        timeout=15_000
    )
    expect(page.get_by_text("Exact walking route", exact=True)).to_be_visible()
    page.get_by_role("button", name="Back to walking map").click()
    expect(page.get_by_role("button", name="Show exact route")).to_be_visible()


def test_map_ready_checkbox_requires_a_safely_visible_location(page: Page) -> None:
    _load(page)
    page.locator(".more-filters > summary").click()
    with page.expect_response(
        lambda response: "/api/listings?" in response.url
        and "map_ready=true" in response.url
        and response.status == 200
    ):
        page.get_by_label("Map ready").check()
    expect(page).to_have_url(re.compile(r"map_ready=true"))
    page.locator(".more-filters > summary").click()

    first_card = page.locator(".listing-card").first
    expect(first_card.locator(".map-warning")).to_have_count(0)
    first_card.locator(".card-select").click()
    detail = page.get_by_label("Selected listing details")
    expect(detail).to_be_visible(timeout=10_000)
    expect(detail.locator(".location-unavailable")).to_have_count(0)
    expect(page.get_by_role("button", name="Walk time")).to_be_enabled()


def test_mobile_filters_detail_and_map_are_usable(browser, request) -> None:
    context = browser.new_context(viewport={"width": 390, "height": 844})
    context.route("**/*", _allow_local_only)
    page = context.new_page()
    try:
        _load(page)
        expect(page.locator(".results-summary")).to_contain_text(
            re.compile(r"\d+ results · \d+ shown on map")
        )
        expect(page.get_by_label("Maximum monthly rent")).to_be_visible()
        expect(page.get_by_label("Bedrooms")).to_be_visible()
        more = page.locator(".more-filters > summary")
        more.click()
        expect(page.get_by_label("Housing type")).to_be_visible()
        page.get_by_label("Laundry").check()
        expect(page).to_have_url(re.compile(r"laundry=true"), timeout=10_000)
        page.get_by_role("button", name="Reset all filters").click()
        expect(page.get_by_label("Laundry")).not_to_be_checked()
        more.click()
        expect(page.get_by_label("Housing type")).not_to_be_visible()

        _search_listing(page, "63094", "1122 Sunset St")
        page.locator("#listing-63094 .card-select").click()
        detail = page.get_by_label("Selected listing details")
        expect(detail).to_be_visible()
        expect(detail.get_by_text("First observed", exact=True)).to_be_visible()
        expect(detail.get_by_text("Last observed", exact=True)).to_be_visible()
        page.get_by_role("button", name="Show map", exact=True).click()
        expect(page.locator(".listing-map")).to_be_visible()
        expect(page.get_by_label("Selected listing details")).not_to_be_visible()
        dimensions = page.evaluate(
            "({client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth})"
        )
        assert dimensions["client"] == dimensions["scroll"] == 390
    except Exception:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=OUTPUT_DIR / f"{request.node.name}.png", full_page=True
        )
        raise
    finally:
        context.close()
