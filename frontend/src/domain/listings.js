import { transportStatusText } from "./transportation.js";

export const DEFAULT_FILTERS = Object.freeze({
  search: "",
  min_price: "",
  max_price: "",
  bedrooms: "",
  housing_type: "",
  roommates_wanted: "",
  lease_type: "",
  is_sublet: "",
  summer_available: "",
  preferred_gender: "",
  furnished: "",
  utilities_included: "",
  parking_available: "",
  laundry: "",
  map_ready: "",
  max_distance_km: "",
  max_walk_minutes: "",
  max_transit_minutes: "",
  data_quality: "",
  ranking_status: "",
  min_score: "",
});

const POSITIVE_REQUIREMENT_FILTERS = new Set([
  "parking_available",
  "laundry",
  "map_ready",
]);

export const COMMUTE_THRESHOLDS = Object.freeze([15, 20, 30, 45]);

export const SORT_OPTIONS = Object.freeze([
  ["recommended", "Recommended"],
  ["overall_score", "Best overall match"],
  ["value_score", "Best value"],
  ["campus_access_score", "Best campus access"],
  ["transit_score", "Best transit"],
  ["price_low", "Price: low to high"],
  ["price_high", "Price: high to low"],
  ["distance", "Distance to destination"],
  ["newest", "Newest"],
]);

const FILTER_OPTION_FIELDS = Object.freeze({
  housingTypes: "housing_type",
  leaseTypes: "lease_type",
  genders: "preferred_gender",
});

export function withFilterValue(filters, key, value) {
  return { ...filters, [key]: value };
}

export function withPositiveRequirement(filters, key, checked) {
  return withFilterValue(filters, key, checked ? "true" : "");
}

export function mergeFilterOptions(current, listings) {
  return Object.fromEntries(
    Object.entries(FILTER_OPTION_FIELDS).map(([optionKey, listingKey]) => [
      optionKey,
      [...new Set([
        ...(current?.[optionKey] || []),
        ...listings.map((listing) => listing[listingKey]).filter(Boolean),
      ])].sort(),
    ]),
  );
}

export function numberOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function boolOrNull(value) {
  if (value === true || value === "true" || value === "1") return true;
  if (value === false || value === "false" || value === "0") return false;
  return null;
}

export function monthlyPrice(listing) {
  return numberOrNull(listing.price_monthly);
}

export function isRoommatesWanted(listing) {
  return ["house_to_share", "apartment_to_share"].includes(
    listing?.housing_type,
  );
}

const GENDER_LABELS = Object.freeze({
  male_preferred: "Male preferred",
  female_preferred: "Female preferred",
  male_only: "Male only",
  female_only: "Female only",
  any: "Any gender",
  not_specified: "Not specified",
});

const HOUSING_TYPE_LABELS = Object.freeze({
  house: "House",
  house_to_share: "House to share",
  apartment: "Apartment",
  apartment_to_share: "Apartment to share",
  apt_to_share: "Apartment to share",
  bachelor_apartment: "Bachelor apartment",
  bachelor_apt: "Bachelor apartment",
  room: "Room",
  sublet: "Sublet",
  sublets: "Sublet",
  townhouse: "Townhouse",
  condo: "Condo",
  duplex: "Duplex",
});

const LEASE_TYPE_LABELS = Object.freeze({
  standard: "12-month lease",
  fixed_term: "8–10 month fixed term",
  short_term: "Short term",
  sublet: "Sublet",
  unknown: "Unknown",
});

export function genderLabel(value) {
  return GENDER_LABELS[value] || value || "Not specified";
}

export function housingTypeLabel(value) {
  return HOUSING_TYPE_LABELS[value] || value || "Unknown";
}

export function leaseTypeLabel(value) {
  return LEASE_TYPE_LABELS[value] || value || "Unknown";
}

export function utilitiesLabel(listing) {
  const labels = {
    all_included: "All utilities included",
    partially_included: "Some utilities included",
    not_included: "Utilities extra",
    unknown: "Utilities unknown",
  };
  if (labels[listing.utilities_status]) return labels[listing.utilities_status];
  const included = boolOrNull(listing.utilities_included);
  if (included === true) return "All utilities included";
  if (included === false) return "Utilities extra";
  return "Utilities unknown";
}

export function locationVisibility(listing) {
  const status = listing?.location?.status;
  if (["available", "limited", "unavailable"].includes(status)) {
    return listing.location;
  }
  const coordinatesAvailable =
    numberOrNull(listing?.latitude) !== null &&
    numberOrNull(listing?.longitude) !== null;
  if (coordinatesAvailable && boolOrNull(listing?.map_ready) === true) {
    return { status: "available", map_visible: true, route_available: true };
  }
  if (coordinatesAvailable) {
    return { status: "limited", map_visible: true, route_available: false };
  }
  return { status: "unavailable", map_visible: false, route_available: false };
}

export function mapReadyListings(listings) {
  return listings.filter(
    (listing) =>
      locationVisibility(listing).map_visible === true &&
      numberOrNull(listing.latitude) !== null &&
      numberOrNull(listing.longitude) !== null,
  );
}

export function filtersFromSearch(search) {
  const params = new URLSearchParams(search);
  return Object.fromEntries(
    Object.keys(DEFAULT_FILTERS).map((key) => {
      const value = params.get(key) || "";
      return [
        key,
        POSITIVE_REQUIREMENT_FILTERS.has(key) && value !== "true" ? "" : value,
      ];
    }),
  );
}

export function filtersToSearch(filters, { sort, hotspotId, page = 1 } = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== "" && value !== null && value !== undefined) params.set(key, value);
  }
  if (sort && sort !== "recommended") params.set("sort", sort);
  if (hotspotId && hotspotId !== "western-main-campus") params.set("hotspot", hotspotId);
  if (page > 1) params.set("page", String(page));
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function apiParameters(
  filters,
  { sort, hotspotId, page, pageSize = 100, listingIds = [] },
) {
  const parameters = {
    ...Object.fromEntries(
      Object.entries(filters).filter(([, value]) => value !== ""),
    ),
    sort,
    hotspot_id: hotspotId,
    page,
    page_size: pageSize,
  };
  if (listingIds.length) parameters.listing_ids = listingIds.join(",");
  return parameters;
}

export function mapApiParameters(
  filters,
  { hotspotId, listingIds = [] },
) {
  const parameters = apiParameters(filters, {
    sort: undefined,
    hotspotId,
    page: 1,
    listingIds,
  });
  delete parameters.page;
  delete parameters.page_size;
  return parameters;
}

export function selectedListingSummary(pageListings, mapListings, listingId) {
  return [...pageListings, ...mapListings].find(
    (listing) => String(listing.listing_id) === String(listingId),
  );
}

export function formatPrice(listing) {
  const price = monthlyPrice(listing);
  return price === null ? "Price unavailable" : `$${price.toLocaleString()}/month`;
}

export function formatDistance(listing) {
  const distance = numberOrNull(listing.selected_hotspot_distance_km);
  return distance === null ? "Distance unavailable" : `${distance.toFixed(1)} km straight-line`;
}

export function travelDisplay(results, mode) {
  const result = Array.isArray(results)
    ? results.find((candidate) => candidate.mode === mode)
    : results;
  if (!result) return { primary: "Information unavailable", detail: "No estimate returned" };
  if (result.status === "invalid_origin") {
    return { primary: "Address not map-ready", detail: "Travel estimate unavailable" };
  }
  if (result.result_type === "unavailable" || result.status === "unavailable") {
    return {
      primary: mode === "transit" ? "Transit profile unavailable" : "Travel profile unavailable",
      detail: "The listing does not have a usable origin for this request",
      resultLabel: mode === "transit" ? "Transit profile unavailable" : "Travel profile unavailable",
      notice: result.fixture_notice || null,
    };
  }
  if (
    result.status === "pending_provider" ||
    result.status === "unavailable" ||
    result.result_type === "pending_provider"
  ) {
    return {
      primary: mode === "transit" ? "Transit profile unavailable" : "Live routing not configured",
      detail: "No safe cached profile exists; provider calculation is pending",
      resultLabel: mode === "transit" ? "Transit profile unavailable" : "Routing unavailable",
      notice: result.fixture_notice || null,
    };
  }
  const duration = result.representative_duration_seconds ?? result.duration_seconds;
  const minutes = duration == null ? null : Math.max(1, Math.round(duration / 60));
  const distance = result.distance_meters == null ? null : (result.distance_meters / 1000).toFixed(1);
  const provider = String(result.provider || "estimate").replaceAll("_", " ");
  const calculated = result.calculated_at
    ? ` · Updated ${new Date(result.calculated_at).toLocaleDateString()}`
    : "";
  const resultLabels = {
    exact_route: "Exact route",
    cached_exact_property: "Cached exact route",
    cached_exact_origin: "Cached exact route",
    same_stop_reuse: "Same-stop transit estimate",
    nearby_origin_estimate: "Nearby-location estimate",
    straight_line_fallback: "Straight-line estimate",
    stale: "Stale route profile",
  };
  const estimatedTypes = new Set([
    "same_stop_reuse",
    "nearby_origin_estimate",
    "straight_line_fallback",
  ]);
  const isEstimate =
    result.is_estimate ||
    result.status === "estimated" ||
    estimatedTypes.has(result.result_type);
  const straightLine =
    result.result_type === "straight_line_fallback" ||
    result.distance_type === "straight_line" ||
    (result.status === "estimated" && !result.result_type);
  const periodLabels = {
    weekday_morning_commute: "weekday-morning",
    weekday_midday: "weekday-midday",
    weekday_evening_commute: "weekday-evening",
    weekday_late_evening: "weekday late-evening",
    saturday_daytime: "Saturday daytime",
    sunday_daytime: "Sunday daytime",
  };
  const periodLabel = periodLabels[result.time_period] || "representative";
  const stale = result.result_type === "stale" || result.freshness === "stale";
  const versionParts = [
    result.network_version ? `Network ${result.network_version}` : null,
    result.schedule_version ? `Schedule ${result.schedule_version}` : null,
  ].filter(Boolean);
  const qualityLabels = {
    complete: "Routing quality: complete",
    partial: "Routing quality: partial",
    insufficient_samples: "Routing quality: insufficient samples",
    no_route: "Routing quality: no route",
    provider_error: "Routing quality: provider error",
  };
  return {
    primary:
      minutes === null
        ? "Duration unavailable"
        : stale
          ? `Stale route profile · ${minutes} minutes`
        : mode === "transit" && !isEstimate
          ? `Typical ${periodLabel} trip · ${minutes} minutes`
          : `${isEstimate ? "Approximately " : ""}${minutes} minutes`,
    detail: `${distance ?? "?"} km${straightLine ? " straight-line · Estimated, not a route" : " route distance"} · ${provider}${calculated}`,
    range:
      result.minimum_duration_seconds != null && result.maximum_duration_seconds != null
        ? `Usually ${Math.round(result.minimum_duration_seconds / 60)}–${Math.round(result.maximum_duration_seconds / 60)} minutes`
        : null,
    walking:
      result.walking_duration_seconds != null
        ? `${Math.round(result.walking_duration_seconds / 60)} min walking`
        : null,
    transfers:
      result.transfer_count != null
        ? `${result.transfer_count} transfer${result.transfer_count === 1 ? "" : "s"}`
        : null,
    samples:
      result.sample_count > 0
        ? `Based on ${result.sample_count} representative departure${result.sample_count === 1 ? "" : "s"}`
        : null,
    quality: qualityLabels[result.quality_status] || null,
    versions: versionParts.length ? versionParts.join(" · ") : null,
    resultLabel: resultLabels[result.result_type] || (isEstimate ? "Estimated result" : "Available route"),
    freshness: stale ? "Stale — refresh required" : calculated ? calculated.replace(" · ", "") : null,
    explanation: result.reuse_explanation || null,
    notice: result.fixture_notice || null,
    straightLine,
  };
}

export function qualityLabel(listing) {
  const quality = listing.data_quality || {};
  if (quality.needs_review || quality.status === "needs-review") return "Needs review";
  if (quality.status === "confirmed") return "Verified";
  if (quality.status === "parsed") return "Parsed";
  return "Information incomplete";
}

export function createSelectionState(listings, selectedListingId) {
  return {
    selectedListingId,
    visibleListingIds: listings.map((listing) => String(listing.listing_id)),
    mapListingIds: mapReadyListings(listings).map((listing) => String(listing.listing_id)),
  };
}

export function comparisonRows(listings) {
  return listings.map((listing) => {
    const transportation = listing.transportation;
    const ranking = listing.ranking;
    const walk = numberOrNull(transportation?.walking?.duration_minutes);
    const overall = numberOrNull(ranking?.overall_score);
    const value = numberOrNull(ranking?.components?.value);
    const yesNo = (valueToFormat) => {
      const parsed = boolOrNull(valueToFormat);
      return parsed === null ? "Not specified" : parsed ? "Yes" : "No";
    };
    return {
      listing_id: listing.listing_id,
      address: listing.address || listing.title || "Untitled listing",
      price: formatPrice(listing),
      bedrooms: listing.bedrooms ?? "Not specified",
      housing_type: housingTypeLabel(listing.housing_type),
      lease: leaseTypeLabel(listing.lease_type),
      utilities: utilitiesLabel(listing).replace(" unknown", " not specified"),
      furnished: yesNo(listing.furnished),
      parking: yesNo(listing.parking_available),
      walk: walk === null ? "Not specified" : `${Math.round(walk)} min`,
      transit: morningTransitComparisonText(transportation),
      overall_match: overall === null ? "Not ranked" : `${Math.round(overall)}/100`,
      value: value === null ? "Not specified" : `${Math.round(value)}/100`,
      availability: listing.availability_text || "Not specified",
    };
  });
}

export function morningTransitComparisonText(transportation) {
  const transit = transportation?.transit;
  if (!transit) return "Not calculated yet";

  const stale =
    transit.status === "stale" ||
    ["stale", "stale_schedule"].includes(transit.freshness) ||
    transportation?.schedule?.freshness === "expired";
  if (stale) return "Transit estimate needs refresh";
  if (transit.freshness !== "current") return "Not calculated yet";

  if (["walking_better_than_transit", "no_route", "technical_failure"].includes(transit.status)) {
    return transportStatusText(transit);
  }

  const duration = numberOrNull(transit.duration_minutes);
  return duration !== null && ["available", "partial"].includes(transit.status)
    ? `${Math.round(duration)} min`
    : "Not calculated yet";
}
