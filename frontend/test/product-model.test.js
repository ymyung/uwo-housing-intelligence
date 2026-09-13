import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  DEFAULT_FILTERS,
  COMMUTE_THRESHOLDS,
  apiParameters,
  comparisonRows,
  createSelectionState,
  filtersFromSearch,
  filtersToSearch,
  mapReadyListings,
  genderLabel,
  housingTypeLabel,
  isRoommatesWanted,
  locationVisibility,
  mapApiParameters,
  travelDisplay,
  mergeFilterOptions,
  selectedListingSummary,
  utilitiesLabel,
  withFilterValue,
  withPositiveRequirement,
} from "../src/domain/listings.js";
import {
  SHORTLIST_STORAGE_KEY,
  loadShortlist,
  saveShortlist,
  toggleComparison,
  toggleShortlist,
} from "../src/domain/shortlist.js";
import { MapAdapter, MockMapAdapter } from "../src/map/MapAdapter.js";
import {
  clusterInteraction,
  clusterListings,
  hasVisibleMapSize,
  MAX_CLUSTER_ZOOM,
  selectClusterListing,
} from "../src/map/mapModel.js";

const listings = [
  { listing_id: "1", latitude: 43.01, longitude: -81.27, map_ready: true, price_monthly: 800 },
  { listing_id: "2", latitude: 43.0101, longitude: -81.2701, map_ready: true, price_monthly: 900 },
  { listing_id: "3", latitude: null, longitude: null, map_ready: false, price_monthly: 700 },
];

test("map and cards derive from the same result set without hiding coordinate-less cards", () => {
  const state = createSelectionState(listings, "1");
  assert.deepEqual(state.visibleListingIds, ["1", "2", "3"]);
  assert.deepEqual(state.mapListingIds, ["1", "2"]);
  assert.equal(mapReadyListings(listings).length, 2);
});

test("selection state is provider-neutral for marker and card synchronization", () => {
  assert.equal(createSelectionState(listings, "2").selectedListingId, "2");
  assert.ok(!JSON.stringify(createSelectionState(listings, "2")).includes("leaflet"));
});

test("dense points cluster while high zoom separates them", () => {
  assert.equal(clusterListings(listings, 12).length, 1);
  assert.equal(clusterListings(listings, 18).length, 2);
});

test("cluster interaction zooms separable points and exposes max-zoom collisions", () => {
  const nearby = [
    { listing_id: "20", latitude: 43.01, longitude: -81.27, map_ready: true },
    { listing_id: "21", latitude: 43.01004, longitude: -81.27004, map_ready: true },
  ];
  const lowZoomCluster = clusterListings(nearby, 13)[0];
  assert.deepEqual(clusterInteraction(lowZoomCluster, 13), {
    kind: "zoom",
    listings: nearby,
    zoom: 15,
  });
  assert.equal(clusterListings(nearby, MAX_CLUSTER_ZOOM).length, 2);

  const sameCoordinate = [
    { listing_id: "42", latitude: 42.956955, longitude: -81.2878983, map_ready: true, price_monthly: 925 },
    { listing_id: "41", latitude: 42.956955, longitude: -81.2878983, map_ready: true, price_monthly: 850 },
  ];
  const collision = clusterListings(sameCoordinate, MAX_CLUSTER_ZOOM)[0];
  const chooser = clusterInteraction(collision, MAX_CLUSTER_ZOOM);
  assert.equal(chooser.kind, "choose");
  assert.deepEqual(chooser.listings.map((listing) => listing.listing_id), ["41", "42"]);
});

test("cluster chooser selection reuses map detail selection for every listing", () => {
  const selections = [];
  const onSelectListing = (listingId, source) => selections.push([listingId, source]);
  selectClusterListing(onSelectListing, "41");
  selectClusterListing(onSelectListing, "42");
  assert.deepEqual(selections, [["41", "map"], ["42", "map"]]);
  assert.equal(selectedListingSummary([], [{ listing_id: "42" }], "42").listing_id, "42");
});

test("cluster chooser membership follows current safe filtered map listings", () => {
  const current = [
    { listing_id: "41", latitude: 42.956955, longitude: -81.2878983, map_ready: true },
    { listing_id: "42", latitude: 42.956955, longitude: -81.2878983, map_ready: true },
    {
      listing_id: "unavailable",
      latitude: 42.956955,
      longitude: -81.2878983,
      location: { status: "unavailable", map_visible: false, route_available: false },
    },
  ];
  const fullChooser = clusterInteraction(
    clusterListings(current, MAX_CLUSTER_ZOOM)[0],
    MAX_CLUSTER_ZOOM,
  );
  assert.deepEqual(fullChooser.listings.map((listing) => listing.listing_id), ["41", "42"]);

  const filteredClusters = clusterListings([current[1], current[2]], MAX_CLUSTER_ZOOM);
  assert.equal(filteredClusters.length, 1);
  assert.deepEqual(filteredClusters[0].listings.map((listing) => listing.listing_id), ["42"]);

  const resetChooser = clusterInteraction(
    clusterListings(current, MAX_CLUSTER_ZOOM)[0],
    MAX_CLUSTER_ZOOM,
  );
  assert.deepEqual(resetChooser.listings.map((listing) => listing.listing_id), ["41", "42"]);
});

test("a hidden mobile map is not focused until it has visible dimensions", () => {
  assert.equal(hasVisibleMapSize({ offsetWidth: 0, offsetHeight: 600 }), false);
  assert.equal(hasVisibleMapSize({ offsetWidth: 400, offsetHeight: 0 }), false);
  assert.equal(hasVisibleMapSize({ offsetWidth: 400, offsetHeight: 600 }), true);
});

test("travel labels identify estimates and never fabricate transit", () => {
  const results = [
    { mode: "walking", status: "estimated", distance_meters: 2100, duration_seconds: 1560 },
    { mode: "cycling", status: "estimated", distance_meters: 2100, duration_seconds: 504 },
    { mode: "transit", status: "pending_provider", distance_meters: 2100, duration_seconds: null },
  ];
  assert.match(travelDisplay(results, "walking").primary, /Approximately 26 minutes/);
  assert.match(travelDisplay(results, "cycling").detail, /Estimated, not a route/);
  assert.match(travelDisplay(results, "transit").primary, /unavailable/);
});

test("persistent accessibility profiles show typical range and reuse labels", () => {
  const display = travelDisplay(
    {
      mode: "transit",
      result_type: "cached_exact_property",
      representative_duration_seconds: 1440,
      minimum_duration_seconds: 1260,
      maximum_duration_seconds: 1740,
      walking_duration_seconds: 360,
      transfer_count: 1,
      sample_count: 3,
      time_period: "weekday_morning_commute",
      distance_meters: 5200,
      provider: "fixture_transit",
      quality_status: "complete",
      network_version: "network-v1",
      schedule_version: "schedule-v1",
      calculated_at: "2026-08-01T12:00:00Z",
      is_estimate: false,
    },
    "transit",
  );
  assert.equal(display.primary, "Typical weekday-morning trip · 24 minutes");
  assert.equal(display.range, "Usually 21–29 minutes");
  assert.equal(display.walking, "6 min walking");
  assert.equal(display.transfers, "1 transfer");
  assert.equal(display.samples, "Based on 3 representative departures");
  assert.equal(display.quality, "Routing quality: complete");
  assert.equal(display.versions, "Network network-v1 · Schedule schedule-v1");
  assert.equal(display.resultLabel, "Cached exact route");
});

test("nearby and same-stop results are clearly estimates", () => {
  for (const result_type of ["nearby_origin_estimate", "same_stop_reuse"]) {
    const display = travelDisplay(
      { result_type, representative_duration_seconds: 1620, is_estimate: true },
      result_type === "same_stop_reuse" ? "transit" : "walking",
    );
    assert.match(display.primary, /Approximately/);
    assert.match(display.resultLabel, /estimate/i);
  }
});

test("all accessibility result types have student-facing labels", () => {
  const labels = new Map([
    ["exact_route", "Exact route"],
    ["cached_exact_property", "Cached exact route"],
    ["same_stop_reuse", "Same-stop transit estimate"],
    ["nearby_origin_estimate", "Nearby-location estimate"],
    ["straight_line_fallback", "Straight-line estimate"],
    ["stale", "Stale route profile"],
  ]);
  for (const [result_type, label] of labels) {
    const display = travelDisplay(
      {
        mode: result_type === "same_stop_reuse" ? "transit" : "walking",
        result_type,
        representative_duration_seconds: 1500,
        distance_meters: 2000,
        freshness: result_type === "stale" ? "stale" : "current",
      },
      result_type === "same_stop_reuse" ? "transit" : "walking",
    );
    assert.equal(display.resultLabel, label);
  }
  assert.match(
    travelDisplay({ result_type: "unavailable" }, "transit").resultLabel,
    /Transit profile unavailable/,
  );
});

test("stale and fixture results include explicit warnings", () => {
  const display = travelDisplay(
    {
      result_type: "stale",
      representative_duration_seconds: 1800,
      fixture_notice: "Fixture durations are not live routes.",
    },
    "walking",
  );
  assert.match(display.primary, /Stale route profile/);
  assert.match(display.freshness, /refresh required/);
  assert.equal(display.notice, "Fixture durations are not live routes.");
});

test("saved listings persist through replaceable local storage", () => {
  const values = new Map();
  const storage = { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
  const updated = toggleShortlist([], 7);
  saveShortlist(storage, updated);
  assert.deepEqual(loadShortlist(storage), ["7"]);
  assert.equal(values.has(SHORTLIST_STORAGE_KEY), true);
  assert.deepEqual(toggleShortlist(updated, 7), []);
});

test("comparison enforces a small limit and creates normalized rows", () => {
  assert.deepEqual(toggleComparison(["1", "2", "3"], "4", 3), ["1", "2", "3"]);
  const rows = comparisonRows([{ ...listings[0], address: "A", data_quality: { status: "confirmed" } }]);
  assert.equal(rows[0].price, "$800/month");
  assert.equal(rows[0].bedrooms, "Not specified");
  assert.equal(rows[0].parking, "Not specified");
  const accessible = comparisonRows([{
    ...listings[0],
    address: "A",
    transportation: {
      walking: { duration_minutes: 20 },
      transit: { status: "available", freshness: "current", duration_minutes: 14 },
      schedule: { freshness: "current" },
    },
    ranking: { overall_score: 77, components: { value: 82 } },
  }])[0];
  assert.equal(accessible.walk, "20 min");
  assert.equal(accessible.transit, "14 min");
  assert.equal(accessible.overall_match, "77/100");
  assert.equal(accessible.value, "82/100");
});

test("comparison distinguishes current transit from computed unavailable states", () => {
  const listing = { ...listings[0], address: "A" };
  const transitText = (transit, schedule = { freshness: "current" }) => comparisonRows([{
    ...listing,
    transportation: { transit, schedule },
  }])[0].transit;

  assert.equal(
    transitText({ status: "available", freshness: "current", duration_minutes: 17 }),
    "17 min",
  );
  assert.equal(transitText(null), "Not calculated yet");
  assert.equal(
    transitText({ status: "available", freshness: "stale", duration_minutes: 17 }),
    "Transit estimate needs refresh",
  );
  assert.equal(
    transitText(
      { status: "available", freshness: "stale_schedule", duration_minutes: 17 },
      { freshness: "expired" },
    ),
    "Transit estimate needs refresh",
  );
  assert.equal(
    transitText({ status: "no_route", freshness: "current", duration_minutes: null }),
    "No useful transit route was found for this period.",
  );
  assert.equal(
    transitText({
      status: "walking_better_than_transit",
      freshness: "current",
      duration_minutes: null,
    }),
    "Walking is faster or more practical than transit for this period.",
  );
});

test("comparison and ranking evidence preserve the same structured morning duration", () => {
  const representativeMinutes = 16.65;
  const listing = {
    ...listings[0],
    transportation: {
      transit: {
        status: "available",
        freshness: "current",
        duration_minutes: Math.round(representativeMinutes),
      },
      schedule: { freshness: "current" },
    },
    ranking: {
      signals: {
        transit_periods: [{
          time_period: "weekday_morning_commute",
          representative_minutes: representativeMinutes,
        }],
      },
    },
  };

  assert.equal(comparisonRows([listing])[0].transit, "17 min");
  assert.equal(
    comparisonRows([listing])[0].transit,
    `${Math.round(listing.ranking.signals.transit_periods[0].representative_minutes)} min`,
  );
});

test("location visibility is fail-closed when an explicit contract is invalid", () => {
  assert.deepEqual(
    locationVisibility({
      latitude: 43.01,
      longitude: -81.27,
      map_ready: true,
      location: {
        status: "unavailable",
        map_visible: false,
        route_available: false,
      },
    }),
    { status: "unavailable", map_visible: false, route_available: false },
  );
  assert.equal(mapReadyListings([{ ...listings[0], location: { status: "unavailable", map_visible: false, route_available: false } }]).length, 0);
});

test("filter state round-trips through URL and API parameters", () => {
  const filters = filtersFromSearch("?max_price=900&is_sublet=true&summer_available=false&roommates_wanted=true&max_walk_minutes=20&max_transit_minutes=30");
  assert.equal(filters.max_price, "900");
  assert.match(filtersToSearch(filters, { sort: "distance", hotspotId: "campus", page: 2 }), /is_sublet=true/);
  const api = apiParameters(filters, { sort: "distance", hotspotId: "campus", page: 2 });
  assert.equal(api.hotspot_id, "campus");
  assert.equal(api.page, 2);
  assert.equal(api.roommates_wanted, "true");
  assert.equal(api.max_walk_minutes, "20");
  assert.equal(api.max_transit_minutes, "30");
  assert.equal(
    apiParameters(DEFAULT_FILTERS, {
      sort: "recommended",
      hotspotId: "western-main-campus",
      page: 1,
      listingIds: ["62609", "61461"],
    }).listing_ids,
    "62609,61461",
  );
});

test("roommate and commute shortcuts compose without resetting detailed filters", () => {
  let filters = withFilterValue({ ...DEFAULT_FILTERS, bedrooms: "4" }, "roommates_wanted", "true");
  filters = withFilterValue(filters, "housing_type", "apartment_to_share");
  filters = withFilterValue(filters, "max_walk_minutes", "20");
  filters = withFilterValue(filters, "max_transit_minutes", "30");
  let parameters = apiParameters(filters, { sort: "recommended", hotspotId: "western-main-campus", page: 1 });
  assert.equal(parameters.roommates_wanted, "true");
  assert.equal(parameters.housing_type, "apartment_to_share");
  assert.equal(parameters.bedrooms, "4");
  assert.equal(parameters.max_walk_minutes, "20");
  assert.equal(parameters.max_transit_minutes, "30");

  filters = withFilterValue(filters, "roommates_wanted", "");
  parameters = apiParameters(filters, { sort: "recommended", hotspotId: "western-main-campus", page: 1 });
  assert.equal("roommates_wanted" in parameters, false);
  assert.equal(parameters.housing_type, "apartment_to_share");
  assert.equal(parameters.bedrooms, "4");
  assert.deepEqual(COMMUTE_THRESHOLDS, [15, 20, 30, 45]);
});

test("dropdown filters replace a selected value directly while preserving other filters", () => {
  const options = mergeFilterOptions(
    { housingTypes: [], leaseTypes: [], genders: [] },
    [
      { housing_type: "Apartment", lease_type: "Year", preferred_gender: "Any" },
      { housing_type: "House", lease_type: "Month", preferred_gender: "Women" },
    ],
  );
  assert.deepEqual(options.housingTypes, ["Apartment", "House"]);

  let filters = withFilterValue({ ...DEFAULT_FILTERS }, "housing_type", "Apartment");
  assert.equal(filters.housing_type, "Apartment"); // Any -> A
  filters = withFilterValue(filters, "housing_type", "House");
  assert.equal(filters.housing_type, "House"); // A -> B
  filters = withFilterValue(filters, "housing_type", "");
  assert.equal(filters.housing_type, ""); // B -> Any

  filters = withFilterValue({ ...DEFAULT_FILTERS, bedrooms: "2" }, "housing_type", "Apartment");
  filters = withFilterValue(filters, "housing_type", "House");
  const parameters = apiParameters(filters, { sort: "recommended", hotspotId: "western-main-campus", page: 1 });
  assert.equal(parameters.bedrooms, "2");
  assert.equal(parameters.housing_type, "House");
});

test("map query is filter-equivalent but independent of sidebar pagination", () => {
  const filters = { ...DEFAULT_FILTERS, housing_type: "room", laundry: "true" };
  const pageOne = apiParameters(filters, {
    sort: "recommended",
    hotspotId: "western-main-campus",
    page: 1,
  });
  const pageTwo = apiParameters(filters, {
    sort: "recommended",
    hotspotId: "western-main-campus",
    page: 2,
  });
  const mapParameters = mapApiParameters(filters, {
    sort: "recommended",
    hotspotId: "western-main-campus",
  });

  assert.equal(pageOne.page, 1);
  assert.equal(pageTwo.page, 2);
  assert.equal("page" in mapParameters, false);
  assert.equal("page_size" in mapParameters, false);
  assert.equal(mapParameters.housing_type, "room");
  assert.equal(mapParameters.laundry, "true");
});

test("off-page marker selection resolves through the shared listing detail flow", () => {
  const pageListings = [{ listing_id: "page-1", title: "Current page" }];
  const mapListings = [
    ...pageListings,
    { listing_id: "page-6", title: "Off-page marker" },
  ];

  assert.equal(
    selectedListingSummary(pageListings, mapListings, "page-6").title,
    "Off-page marker",
  );
  assert.equal(selectedListingSummary(pageListings, mapListings, "missing"), undefined);
});

test("filter and reset changes produce the corresponding independent map membership query", () => {
  const active = mapApiParameters(
    { ...DEFAULT_FILTERS, roommates_wanted: "true", max_walk_minutes: "20" },
    { sort: "recommended", hotspotId: "western-main-campus" },
  );
  const reset = mapApiParameters(DEFAULT_FILTERS, {
    sort: "recommended",
    hotspotId: "western-main-campus",
  });

  assert.equal(active.roommates_wanted, "true");
  assert.equal(active.max_walk_minutes, "20");
  assert.equal("roommates_wanted" in reset, false);
  assert.equal("max_walk_minutes" in reset, false);
});

test("positive requirement checkboxes only serialize supported positive evidence", () => {
  let filters = { ...DEFAULT_FILTERS, bedrooms: "2" };
  assert.equal("laundry" in apiParameters(filters, { sort: "recommended", hotspotId: "western-main-campus", page: 1 }), false);

  filters = withPositiveRequirement(filters, "laundry", true);
  filters = withPositiveRequirement(filters, "parking_available", true);
  filters = withPositiveRequirement(filters, "map_ready", true);
  let parameters = apiParameters(filters, { sort: "recommended", hotspotId: "western-main-campus", page: 1 });
  assert.equal(parameters.laundry, "true");
  assert.equal(parameters.parking_available, "true");
  assert.equal(parameters.map_ready, "true");
  assert.equal(parameters.bedrooms, "2");
  assert.match(filtersToSearch(filters), /laundry=true/);

  filters = withPositiveRequirement(filters, "laundry", false);
  parameters = apiParameters(filters, { sort: "recommended", hotspotId: "western-main-campus", page: 1 });
  assert.equal("laundry" in parameters, false);
  assert.equal(parameters.parking_available, "true");
  assert.equal(filtersFromSearch("?laundry=false&parking_available=true").laundry, "");
  assert.equal(filtersFromSearch("?laundry=false&parking_available=true").parking_available, "true");
});

test("source semantics have truthful student-facing labels", () => {
  assert.equal(genderLabel("male_preferred"), "Male preferred");
  assert.equal(genderLabel("female_preferred"), "Female preferred");
  assert.equal(genderLabel("male_only"), "Male only");
  assert.equal(genderLabel("female_only"), "Female only");
  assert.equal(genderLabel(null), "Not specified");
  assert.equal(housingTypeLabel("house_to_share"), "House to share");
  assert.equal(isRoommatesWanted({ housing_type: "house_to_share" }), true);
  assert.equal(isRoommatesWanted({ housing_type: "apartment_to_share" }), true);
  assert.equal(isRoommatesWanted({ housing_type: "house", description: "roommate" }), false);
  assert.equal(utilitiesLabel({ utilities_status: "partially_included" }), "Some utilities included");
  assert.equal(utilitiesLabel({ utilities_included: null }), "Utilities unknown");
});

test("map adapter can be mocked without provider-specific objects", () => {
  const adapter = new MockMapAdapter();
  assert.ok(adapter instanceof MapAdapter);
  adapter.setListings(listings);
  adapter.setHotspots([{ id: "western" }]);
  adapter.selectListing("1");
  adapter.showRoute({ distance_meters: 100 });
  adapter.fitToListings();
  adapter.destroy();
  assert.equal(adapter.state.selectedListingId, "1");
  assert.equal(adapter.state.destroyed, true);
});

test("UI source includes clear states, accessible controls, and no Google globals", async () => {
  const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
  const map = await readFile(new URL("../src/map/ListingMap.jsx", import.meta.url), "utf8");
  const main = await readFile(new URL("../src/main.jsx", import.meta.url), "utf8");
  assert.match(app, /Loading housing listings/);
  assert.match(app, /No listings match/);
  assert.match(app, /Try increasing your commute time or budget/);
  assert.match(app, /Roommates Wanted/);
  assert.match(app, /Walk to Western/);
  assert.match(app, /Morning transit to Western/);
  assert.match(app, /listingsResponse\.total/);
  assert.match(app, /fetchMapListings/);
  assert.match(app, /listings=\{mapListings\}/);
  assert.match(app, /shown on map/);
  assert.match(app, /role="alert"/);
  assert.match(app, /aria-label="Travel mode"/);
  assert.match(app, /Location is not shown/);
  assert.match(app, /popstate/);
  assert.match(app, /aria-label="Scheduled transit periods"/);
  assert.match(app, /Gender preference/);
  assert.match(app, /Must have/);
  assert.match(app, /FilterCheckbox/);
  assert.match(app, /label="Laundry"/);
  assert.match(app, /label="Parking"/);
  assert.match(app, /label="Map ready"/);
  assert.match(app, /Explicitly confirmed non-sublets/);
  assert.match(app, /fetchTransportationRoute/);
  assert.match(map, /routeSegments\(route\)/);
  assert.doesNotMatch(map, /\[route\.origin, route\.destination\]/);
  assert.match(app, /Morning transit to Western/);
  assert.match(app, /fixture-notice/);
  assert.match(map, /hasVisibleMapSize\(container\)/);
  assert.match(map, /new ResizeObserver\(focusSelectedListing\)/);
  assert.match(main, /import "\.\/index\.css"/);
  assert.doesNotMatch(app.toLowerCase(), /leave now/);
  assert.ok(!`${app}${map}`.includes("google.maps"));
  assert.ok(!`${app}${map}`.includes("Geoapify"));
});
