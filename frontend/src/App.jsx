import { useEffect, useMemo, useRef, useState } from "react";

import "./App.css";
import {
  fetchAccessibilityTimePeriods,
  fetchExactWalkingRoute,
  fetchHotspots,
  fetchListing,
  fetchListings,
  fetchMapListings,
  fetchTransportationOverview,
  fetchTransportationRoute,
  fetchWalkingSurface,
  fetchWalkingSurfaceGrid,
  fetchWalkingSurfaceValidity,
  fetchWalkingSurfaceValues,
} from "./api.js";
import { PRODUCT_CONFIG } from "./config.js";
import { RankingCardSummary, RankingDetails } from "./components/RankingDisplay.jsx";
import {
  DEFAULT_FILTERS,
  COMMUTE_THRESHOLDS,
  SORT_OPTIONS,
  apiParameters,
  comparisonRows,
  filtersFromSearch,
  filtersToSearch,
  formatDistance,
  formatPrice,
  genderLabel,
  housingTypeLabel,
  isRoommatesWanted,
  leaseTypeLabel,
  locationVisibility,
  mapApiParameters,
  mergeFilterOptions,
  qualityLabel,
  selectedListingSummary,
  utilitiesLabel,
  withFilterValue,
  withPositiveRequirement,
} from "./domain/listings.js";
import {
  loadShortlist,
  saveShortlist,
  toggleShortlist,
} from "./domain/shortlist.js";
import { historyEventText, observedDate } from "./domain/listingHistory.js";
import {
  distanceText,
  durationText,
  selectedModeSummary,
  transitLegRows,
  transportCardItems,
  transportStatusText,
} from "./domain/transportation.js";
import { canRenderSurface, decodeValidity, decodeWalkingValues, formatApproximateMinutes, nextSurfaceState } from "./domain/walkingSurface.js";
import ListingMap from "./map/ListingMap.jsx";

const TRAVEL_MODES = ["walking", "cycling", "transit"];

function initialUrlState() {
  const params = new URLSearchParams(window.location.search);
  return {
    filters: filtersFromSearch(window.location.search),
    sort: params.get("sort") || "recommended",
    hotspotId: params.get("hotspot") || "western-main-campus",
    page: Math.max(1, Number(params.get("page")) || 1),
  };
}

function boolText(value) {
  if (value === true || value === "true") return "Yes";
  if (value === false || value === "false") return "No";
  return "Unknown";
}

function fieldLabel(listing, field) {
  return listing.field_quality?.[field]?.label || "Status unavailable";
}

function FilterSelect({ label, value, onChange, children }) {
  return (
    <label className="field">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {children}
      </select>
    </label>
  );
}

function FilterCheckbox({ label, checked, onChange }) {
  return (
    <label className="filter-checkbox">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>{label}</span>
    </label>
  );
}

function TransportationCardSummary({ transportation }) {
  const items = transportCardItems(transportation);
  if (!items.length) {
    return <p className="transport-card-unavailable">Transportation estimate unavailable</p>;
  }
  return (
    <div className="transport-card-summary" aria-label="Getting to Western">
      <span>Getting to Western</span>
      <dl>
        {items.map((item) => (
          <div key={item.label} title={item.status.replaceAll("_", " ")}>
            <dt>{item.label}</dt><dd>{item.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function ListingCard({
  listing,
  selected,
  saved,
  comparing,
  compareDisabled,
  onSelect,
  onSave,
  onCompare,
}) {
  const quality = qualityLabel(listing);
  const location = locationVisibility(listing);
  return (
    <article
      id={`listing-${listing.listing_id}`}
      className={`listing-card ${selected ? "selected" : ""}`}
      aria-current={selected ? "true" : undefined}
    >
      <button className="card-select" onClick={onSelect} aria-label={`Open ${listing.address || listing.title}`}>
        <div className="card-heading">
          <div>
            <p className="price">{formatPrice(listing)}</p>
            <h3>{listing.address || listing.title || "Untitled listing"}</h3>
          </div>
          <span className={`quality-chip quality-${quality.toLowerCase().replaceAll(" ", "-")}`}>
            {quality}
          </span>
        </div>
        <p className="card-meta">
          {listing.bedrooms ?? "?"} bed · {housingTypeLabel(listing.housing_type)} · {formatDistance(listing)}
        </p>
        <div className="chip-row">
          {isRoommatesWanted(listing) && <span>Roommates wanted</span>}
          {listing.is_sublet === true && <span>Explicit sublet</span>}
          {listing.summer_available === true && <span>Summer availability</span>}
          {listing.furnished === true && <span>Furnished</span>}
          {listing.utilities_included === true && <span>Utilities included</span>}
          {listing.laundry === true && <span>Laundry</span>}
        </div>
        <RankingCardSummary ranking={listing.ranking} />
        <TransportationCardSummary transportation={listing.transportation} />
        {location.status !== "available" && (
          <p className="map-warning">{listing.location?.message || (location.map_visible ? "Approximate map location only; routes are unavailable." : "Location is not shown; listing details remain available.")}</p>
        )}
      </button>
      <div className="card-actions">
        <button onClick={onSave} aria-pressed={saved}>
          {saved ? "Saved" : "Save"}
        </button>
        <button onClick={onCompare} aria-pressed={comparing} disabled={compareDisabled && !comparing}>
          {comparing ? "Comparing" : "Compare"}
        </button>
      </div>
    </article>
  );
}

function DetailPanel({
  listing,
  transportation,
  selectedRoute,
  routeLoading,
  mode,
  onMode,
  timePeriod,
  timePeriods,
  onTimePeriod,
  onClose,
  onShowMap,
  onSave,
  saved,
}) {
  if (!listing) return null;
  const overview = transportation?.transportation;
  const selectedSummary = selectedModeSummary(overview, mode, timePeriod);
  const route = selectedRoute?.route;
  const itineraryRows = transitLegRows(selectedRoute);
  const location = locationVisibility(listing);
  const amenities = Array.isArray(listing.amenities)
    ? listing.amenities
    : String(listing.amenities || "").split("|").filter(Boolean);
  const historyEvents = Array.isArray(listing.history?.events)
    ? listing.history.events
    : [];
  return (
    <aside className="detail-panel" aria-label="Selected listing details">
      <div className="detail-toolbar">
        <span className="quality-chip">{qualityLabel(listing)}</span>
        <div><button className="show-map-mobile" onClick={onShowMap}>Show map</button><button onClick={onClose} aria-label="Close listing details">Close</button></div>
      </div>
      <h2>{listing.address || listing.title || "Housing listing"}</h2>
      <p className="detail-price">{formatPrice(listing)}</p>
      {listing.price_text && <p className="subtle">Original advertisement: {listing.price_text}</p>}
      <dl className="detail-facts">
        <div><dt>Bedrooms</dt><dd>{listing.bedrooms ?? "Unknown"}<small>{fieldLabel(listing, "bedrooms")}</small></dd></div>
        <div><dt>Housing</dt><dd>{housingTypeLabel(listing.housing_type)}</dd></div>
        <div><dt>Lease</dt><dd>{leaseTypeLabel(listing.lease_type)}</dd></div>
        <div><dt>Sublet</dt><dd>{listing.is_sublet === true ? "Explicitly stated" : listing.is_sublet === false ? "Explicitly confirmed not a sublet" : "Unknown"}<small>{fieldLabel(listing, "is_sublet")}</small></dd></div>
        <div><dt>Summer</dt><dd>{listing.summer_available === true ? "Available" : listing.summer_available === false ? "No" : "Unknown"}</dd></div>
        <div><dt>Furnished</dt><dd>{boolText(listing.furnished)}<small>{fieldLabel(listing, "furnished")}</small></dd></div>
        <div><dt>Utilities</dt><dd>{utilitiesLabel(listing)}</dd></div>
        <div><dt>Gender</dt><dd>{genderLabel(listing.preferred_gender)}</dd></div>
        <div><dt>Bathrooms</dt><dd>{listing.bathrooms ?? "Unknown"}{listing.bathroom_type && listing.bathroom_type !== "unknown" ? ` · ${listing.bathroom_type}` : ""}</dd></div>
        <div><dt>First observed</dt><dd>{observedDate(listing.freshness?.first_observed_at || listing.first_seen_at)}</dd></div>
        <div><dt>Last observed</dt><dd>{observedDate(listing.freshness?.last_observed_at || listing.last_seen_at)}</dd></div>
      </dl>

      {historyEvents.length > 0 && (
        <section aria-labelledby="listing-history-title">
          <h3 id="listing-history-title">Listing history</h3>
          <ul className="amenity-list">
            {historyEvents.map((event) => (
              <li key={`${event.type}-${event.observed_at}`}>
                <strong>{historyEventText(event)}</strong>
                <small>{observedDate(event.observed_at)}</small>
              </li>
            ))}
          </ul>
        </section>
      )}

      <RankingDetails ranking={listing.ranking} />

      {location.route_available ? <section className="travel-card getting-to-western" aria-labelledby="travel-title">
        <p className="eyebrow" id="travel-title">Getting to Western</p>
        <div className="transport-overview" aria-label="Transportation summary">
          {[
            ["Walk", overview?.walking],
            ["Bike", overview?.cycling],
            ["Transit", overview?.transit?.periods?.find((period) => period.time_period === "weekday_morning_commute")],
          ].map(([label, value]) => (
            <div key={label}>
              <span>{label}</span>
              <strong>{value?.duration_minutes == null ? "—" : durationText(value.duration_minutes)}</strong>
            </div>
          ))}
        </div>
        <div className="mode-tabs" role="group" aria-label="Travel mode">
          {TRAVEL_MODES.map((candidate) => (
            <button
              key={candidate}
              className={mode === candidate ? "active" : ""}
              aria-pressed={mode === candidate}
              onClick={() => onMode(candidate)}
            >
              {candidate === "cycling" ? "Bike" : candidate[0].toUpperCase() + candidate.slice(1)}
            </button>
          ))}
        </div>
        {mode === "transit" && (
          <div className="transit-periods" aria-label="Scheduled transit periods">
            {timePeriods.map((period) => {
              const value = overview?.transit?.periods?.find(
                (candidate) => candidate.time_period === period.id,
              );
              return (
                <button
                  key={period.id}
                  className={timePeriod === period.id ? "active" : ""}
                  aria-pressed={timePeriod === period.id}
                  onClick={() => onTimePeriod(period.id)}
                >
                  <span>{period.label}</span>
                  <strong>{value?.duration_minutes == null ? "—" : durationText(value.duration_minutes)}</strong>
                </button>
              );
            })}
          </div>
        )}
        <div className="selected-transport-result" aria-live="polite">
          {routeLoading ? <p>Loading routed path…</p> : (
            <>
              <strong>{transportStatusText(route || selectedSummary, mode)}</strong>
              {mode === "transit" && route?.minimum_duration_seconds != null && route?.maximum_duration_seconds != null && (
                <p>{Math.round(route.minimum_duration_seconds / 60)}–{Math.round(route.maximum_duration_seconds / 60)} min typical range</p>
              )}
              {mode === "transit" && route?.result_type === "same_stop_reuse" && route?.reuse_explanation && (
                <p>{route.reuse_explanation}</p>
              )}
              {(route || selectedSummary)?.distance_meters != null && (
                <p>{distanceText((route || selectedSummary).distance_meters)} routed distance</p>
              )}
              {mode === "transit" && (route || selectedSummary)?.walking_duration_seconds != null && (
                <p>{Math.round((route || selectedSummary).walking_duration_seconds / 60)} min walking</p>
              )}
              {mode === "transit" && (route || selectedSummary)?.transfer_count != null && (
                <p>{(route || selectedSummary).transfer_count === 0 ? "Direct bus · no transfers" : `${(route || selectedSummary).transfer_count} transfer${(route || selectedSummary).transfer_count === 1 ? "" : "s"}`}</p>
              )}
              {(route || selectedSummary)?.high_walking_share && (
                <p className="transport-warning">Some transit trips require substantial walking.</p>
              )}
              {(route || selectedSummary)?.review_required && (
                <p className="transport-warning">Transit estimate has a review flag.</p>
              )}
              {(route || selectedSummary)?.freshness === "stale_schedule" && (
                <p className="transport-warning">Static schedule coverage has expired; treat this as historical service guidance.</p>
              )}
            </>
          )}
        </div>
        {itineraryRows.length > 0 && (
          <ol className="itinerary-legs" aria-label="Text route itinerary">
            {itineraryRows.map((leg) => (
              <li key={leg.key} className={`leg-${leg.mode.toLowerCase()}`}>
                <strong>{leg.label}</strong>
                <span>{[leg.duration, leg.distance].filter(Boolean).join(" · ")}</span>
                {leg.fromStop && <span>Board at {leg.fromStop}</span>}
                {leg.toStop && <span>Exit at {leg.toStop}</span>}
                {leg.departure && leg.mode === "BUS" && <time dateTime={leg.departure}>Scheduled {new Date(leg.departure).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</time>}
              </li>
            ))}
          </ol>
        )}
        <small>Transit times are scheduled estimates from static London Transit data, not live departures.</small>
        {transportation?.fixture_notice && <p className="fixture-notice">{transportation.fixture_notice}</p>}
      </section> : <section className="travel-card location-unavailable" aria-label="Location information unavailable"><p className="eyebrow">Getting to Western</p><strong>Travel times unavailable</strong><p>{listing.location?.message || "This location cannot be used for route estimates."}</p></section>}

      <section>
        <h3>Amenities</h3>
        {amenities.length ? <ul className="amenity-list">{amenities.map((item) => <li key={item}>{item}</li>)}</ul> : <p className="subtle">Information unavailable</p>}
      </section>
      <section>
        <h3>Description</h3>
        <p className="description">{listing.description || "Information unavailable"}</p>
      </section>
      <div className="detail-actions">
        <button className="secondary" onClick={onSave}>{saved ? "Remove saved listing" : "Save listing"}</button>
        {listing.listing_url && <a href={listing.listing_url} target="_blank" rel="noreferrer">View source listing</a>}
      </div>
    </aside>
  );
}

function Comparison({ listings, onClose }) {
  const rows = comparisonRows(listings);
  if (!rows.length) return null;
  const fields = [
    ["price", "Monthly price"],
    ["bedrooms", "Bedrooms"],
    ["housing_type", "Housing type"],
    ["lease", "Lease"],
    ["utilities", "Utilities"],
    ["furnished", "Furnished"],
    ["parking", "Parking"],
    ["walk", "Walk to Western"],
    ["transit", "Morning transit to Western"],
    ["overall_match", "Overall match"],
    ["value", "Value"],
    ["availability", "Availability"],
  ];
  return (
    <div className="comparison-backdrop" role="dialog" aria-modal="true" aria-labelledby="compare-title">
      <section className="comparison-panel">
        <div className="panel-title"><div><p className="eyebrow">Shortlist</p><h2 id="compare-title">Compare listings</h2></div><button onClick={onClose}>Close</button></div>
        <div className="comparison-scroll">
          <table>
            <thead><tr><th>Feature</th>{rows.map((row, index) => <th key={row.listing_id}><span>Option {String.fromCharCode(65 + index)}</span><strong>{row.address}</strong></th>)}</tr></thead>
            <tbody>{fields.map(([field, label]) => <tr key={field}><th>{label}</th>{rows.map((row) => <td key={row.listing_id}>{row[field]}</td>)}</tr>)}</tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

export default function App() {
  const initial = useMemo(() => initialUrlState(), []);
  const [filters, setFilters] = useState(initial.filters);
  const [sort, setSort] = useState(initial.sort);
  const [hotspotId, setHotspotId] = useState(initial.hotspotId);
  const [page, setPage] = useState(initial.page);
  const [listingsResponse, setListingsResponse] = useState({ listings: [], total: 0, pages: 0 });
  const [mapResponse, setMapResponse] = useState({ listings: [], count: 0 });
  const [filterOptions, setFilterOptions] = useState({ housingTypes: [], leaseTypes: [], genders: [] });
  const [hotspots, setHotspots] = useState([]);
  const [timePeriods, setTimePeriods] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [transportationOverview, setTransportationOverview] = useState(null);
  const [selectedRoute, setSelectedRoute] = useState(null);
  const [routeLoading, setRouteLoading] = useState(false);
  const [routeError, setRouteError] = useState("");
  const [travelMode, setTravelMode] = useState("walking");
  const [timePeriod, setTimePeriod] = useState("weekday_morning_commute");
  const [savedIds, setSavedIds] = useState(() => loadShortlist(localStorage));
  const [comparisonListings, setComparisonListings] = useState([]);
  const [showComparison, setShowComparison] = useState(false);
  const [savedOnly, setSavedOnly] = useState(false);
  const [mobilePane, setMobilePane] = useState("list");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [mapError, setMapError] = useState("");
  const [retry, setRetry] = useState(0);
  const [fitRequest, setFitRequest] = useState(0);
  const [mapMode, setMapMode] = useState("listings");
  const [surfaceGrid, setSurfaceGrid] = useState(null);
  const [surfaceState, setSurfaceState] = useState({ status: "idle", metadata: null, values: null, validity: null, error: "" });
  const [surfaceHover, setSurfaceHover] = useState(null);
  const [surfaceDestination, setSurfaceDestination] = useState(null);
  const [exactSurfaceRoute, setExactSurfaceRoute] = useState(null);
  const [exactRouteLoading, setExactRouteLoading] = useState(false);
  const requestSequence = useRef(0);
  const mapRequestSequence = useRef(0);
  const applyingHistory = useRef(false);
  const routeCache = useRef(new Map());
  const surfaceCache = useRef(new Map());
  const exactRouteRequest = useRef(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      fetchHotspots({ signal: controller.signal }),
      fetchAccessibilityTimePeriods({ signal: controller.signal }),
    ])
      .then(([hotspotResponse, periodResponse]) => {
        setHotspots(hotspotResponse.hotspots || []);
        setTimePeriods(periodResponse.time_periods || []);
      })
      .catch((fetchError) => {
        if (fetchError.name !== "AbortError") setError(fetchError.message);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (mapMode !== "walking" || surfaceGrid) return undefined;
    const controller = new AbortController();
    fetchWalkingSurfaceGrid({ signal: controller.signal }).then(setSurfaceGrid).catch(() => {
      if (!controller.signal.aborted) setSurfaceState({ status: "failed", metadata: null, values: null, validity: null, error: "Walking map configuration is unavailable." });
    });
    return () => controller.abort();
  }, [mapMode, surfaceGrid]);

  useEffect(() => {
    if (mapMode !== "walking" || !selectedId || !surfaceGrid) return undefined;
    const selected = selectedListingSummary(
      listingsResponse.listings,
      mapResponse.listings,
      selectedId,
    );
    if (selected && !locationVisibility(selected).route_available) return undefined;
    let cancelled = false;
    let attempts = 0;
    let timer;
    const load = async () => {
      setSurfaceState((current) => current.status === "ready" ? current : { status: "loading", metadata: null, values: null, validity: null, error: "" });
      try {
        const metadata = await fetchWalkingSurface(selectedId);
        if (cancelled) return;
        const status = nextSurfaceState(metadata);
        if (status === "computing" && attempts++ < 12) {
          setSurfaceState({ status, metadata, values: null, validity: null, error: "Calculating walking map…" });
          timer = window.setTimeout(load, 2000);
          return;
        }
        if (!canRenderSurface(metadata, surfaceGrid)) throw new Error(status === "computing" ? "Walking map is taking longer than expected." : "Walking map is unavailable for this listing.");
        const key = `${metadata.surface_id}|${surfaceGrid.grid_fingerprint}`;
        const cached = surfaceCache.current.get(key);
        if (cached) { setSurfaceState({ status: "ready", metadata, ...cached, error: "" }); return; }
        const [valueBytes, validityBytes] = await Promise.all([fetchWalkingSurfaceValues(metadata.surface_id), fetchWalkingSurfaceValidity(metadata.surface_id)]);
        if (cancelled) return;
        const cachedSurface = { values: decodeWalkingValues(valueBytes, surfaceGrid), validity: decodeValidity(validityBytes, surfaceGrid) };
        surfaceCache.current.set(key, cachedSurface);
        setSurfaceState({ status: "ready", metadata, ...cachedSurface, error: "" });
      } catch (surfaceError) {
        if (!cancelled) setSurfaceState({ status: "failed", metadata: null, values: null, validity: null, error: surfaceError.message || "Walking map could not be loaded." });
      }
    };
    load();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [mapMode, selectedId, surfaceGrid, listingsResponse.listings, mapResponse.listings]);

  useEffect(() => {
    if (applyingHistory.current) {
      applyingHistory.current = false;
      return undefined;
    }
    const timer = window.setTimeout(() => {
      const query = filtersToSearch(filters, { sort, hotspotId, page });
      const nextUrl = `${window.location.pathname}${query}`;
      const currentUrl = `${window.location.pathname}${window.location.search}`;
      if (nextUrl !== currentUrl) window.history.pushState(null, "", nextUrl);
    }, 350);
    return () => window.clearTimeout(timer);
  }, [filters, sort, hotspotId, page]);

  useEffect(() => {
  function restoreUrlState() {
      const restored = initialUrlState();
      applyingHistory.current = true;
      setFilters(restored.filters);
      setSort(restored.sort);
      setHotspotId(restored.hotspotId);
      setPage(restored.page);
      setSelectedId(null);
      setDetail(null);
      setTransportationOverview(null);
      setSelectedRoute(null);
    }
    window.addEventListener("popstate", restoreUrlState);
    return () => window.removeEventListener("popstate", restoreUrlState);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const sequence = ++requestSequence.current;
    const timer = window.setTimeout(() => {
      if (savedOnly && !savedIds.length) {
        setListingsResponse({ listings: [], total: 0, pages: 0 });
        setLoading(false);
        setError("");
        return;
      }
      setLoading(true);
      setError("");
      fetchListings(
        apiParameters(savedOnly ? DEFAULT_FILTERS : filters, {
          sort,
          hotspotId,
          page: savedOnly ? 1 : page,
          pageSize: 100,
          listingIds: savedOnly ? savedIds : [],
        }),
        { signal: controller.signal },
      )
        .then((response) => {
          if (sequence === requestSequence.current) {
            setListingsResponse(response);
            setFilterOptions((current) => mergeFilterOptions(current, response.listings));
          }
        })
        .catch((fetchError) => {
          if (fetchError.name !== "AbortError" && sequence === requestSequence.current) {
            setError(fetchError.message || "Listings could not be loaded.");
          }
        })
        .finally(() => {
          if (sequence === requestSequence.current) setLoading(false);
        });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [filters, sort, hotspotId, page, savedIds, savedOnly, retry]);

  useEffect(() => {
    const controller = new AbortController();
    const sequence = ++mapRequestSequence.current;
    if (savedOnly && !savedIds.length) {
      let cancelled = false;
      queueMicrotask(() => {
        if (!cancelled && sequence === mapRequestSequence.current) {
          setMapResponse({ listings: [], count: 0 });
          setMapError("");
        }
      });
      return () => {
        cancelled = true;
        controller.abort();
      };
    }
    const timer = window.setTimeout(() => {
      setMapError("");
      setMapResponse({ listings: [], count: 0 });
      fetchMapListings(
        mapApiParameters(savedOnly ? DEFAULT_FILTERS : filters, {
          hotspotId,
          listingIds: savedOnly ? savedIds : [],
        }),
        { signal: controller.signal },
      )
        .then((response) => {
          if (sequence === mapRequestSequence.current) setMapResponse(response);
        })
        .catch((fetchError) => {
          if (fetchError.name !== "AbortError" && sequence === mapRequestSequence.current) {
            setMapError(fetchError.message || "Map listings could not be loaded.");
          }
        });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [filters, hotspotId, savedIds, savedOnly, retry]);

  useEffect(() => {
    saveShortlist(localStorage, savedIds);
  }, [savedIds]);

  useEffect(() => {
    if (!selectedId) {
      return undefined;
    }
    const controller = new AbortController();
    fetchListing(selectedId, { signal: controller.signal })
      .then(setDetail)
      .catch((fetchError) => {
        if (fetchError.name !== "AbortError") setError(fetchError.message);
      });
    return () => controller.abort();
  }, [selectedId]);

  useEffect(() => {
    if (!selectedId) return undefined;
    const controller = new AbortController();
    fetchTransportationOverview(selectedId, hotspotId, { signal: controller.signal })
      .then((response) => {
        setTransportationOverview(response);
        setComparisonListings((current) => current.map((listing) => (
          String(listing.listing_id) === String(selectedId)
            ? { ...listing, transportation: response.transportation }
            : listing
        )));
      })
      .catch((fetchError) => {
        if (fetchError.name !== "AbortError") setRouteError(fetchError.message);
      });
    return () => controller.abort();
  }, [selectedId, hotspotId]);

  useEffect(() => {
    if (!selectedId) return undefined;
    const selected = selectedListingSummary(
      listingsResponse.listings,
      mapResponse.listings,
      selectedId,
    );
    if (selected && !locationVisibility(selected).route_available) return undefined;
    const selectedPeriod = travelMode === "transit" ? timePeriod : "all_day";
    const key = `${selectedId}|${hotspotId}|${travelMode}|${selectedPeriod}`;
    const cached = routeCache.current.get(key);
    if (cached) {
      let cancelled = false;
      queueMicrotask(() => {
        if (!cancelled) {
          setSelectedRoute(cached.response);
          setRouteLoading(false);
        }
      });
      return () => {
        cancelled = true;
      };
    }
    const controller = new AbortController();
    fetchTransportationRoute(
      selectedId,
      hotspotId,
      travelMode,
      timePeriod,
      { signal: controller.signal },
    )
      .then((response) => {
        routeCache.current.set(key, {
          fingerprint: response.route?.routing_fingerprint || null,
          response,
        });
        setSelectedRoute(response);
      })
      .catch((fetchError) => {
        if (fetchError.name !== "AbortError") setRouteError(fetchError.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setRouteLoading(false);
      });
    return () => controller.abort();
  }, [selectedId, hotspotId, travelMode, timePeriod, listingsResponse.listings, mapResponse.listings]);

  const listings = listingsResponse.listings;
  const mapListings = mapResponse.listings;
  const selectedSummary = selectedListingSummary(listings, mapListings, selectedId);
  const selectedListing = detail && String(detail.listing_id) === String(selectedId) ? { ...selectedSummary, ...detail } : selectedSummary;
  const compareIds = comparisonListings.map((listing) => String(listing.listing_id));
  function clearSelection() {
    setSelectedId(null);
    setDetail(null);
    setTransportationOverview(null);
    setSelectedRoute(null);
    setRouteError("");
    setRouteLoading(false);
    setMapMode("listings");
    setSurfaceDestination(null);
    setSurfaceHover(null);
    exactRouteRequest.current?.abort();
    setExactSurfaceRoute(null);
    setExactRouteLoading(false);
  }

  function updateFilter(key, value) {
    setSavedOnly(false);
    setFilters((current) => withFilterValue(current, key, value));
    setPage(1);
    clearSelection();
  }

  function updatePositiveRequirement(key, checked) {
    setSavedOnly(false);
    setFilters((current) => withPositiveRequirement(current, key, checked));
    setPage(1);
    clearSelection();
  }

  function resetFilters() {
    setSavedOnly(false);
    setFilters({ ...DEFAULT_FILTERS });
    setSort("recommended");
    setPage(1);
    clearSelection();
  }

  function changeSort(value) {
    setSavedOnly(false);
    setSort(value);
    setPage(1);
    clearSelection();
  }

  function changeHotspot(value) {
    setSavedOnly(false);
    setHotspotId(value);
    setPage(1);
    clearSelection();
  }

  function changePage(value) {
    setPage(value);
    clearSelection();
  }

  function selectListing(listingId, source = "card") {
    setDetail(null);
    setTransportationOverview(null);
    setSelectedRoute(null);
    setRouteError("");
    setRouteLoading(true);
    setSelectedId(String(listingId));
    setSurfaceDestination(null);
    setSurfaceHover(null);
    exactRouteRequest.current?.abort();
    setExactSurfaceRoute(null);
    setExactRouteLoading(false);
    if (source === "map") {
      setMobilePane("list");
      window.setTimeout(() => document.getElementById(`listing-${listingId}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 0);
    }
  }

  function changeTravelMode(value) {
    setSelectedRoute(null);
    setRouteError("");
    setRouteLoading(true);
    setTravelMode(value);
  }

  function enableWalkingMap() {
    if (!selectedId || !locationVisibility(selectedListing).route_available) return;
    setMapMode("walking");
    setMobilePane("map");
    setSurfaceDestination(null);
  }

  async function showExactWalkingRoute() {
    if (!selectedId || !surfaceDestination || exactRouteLoading || !locationVisibility(selectedListing).route_available) return;
    exactRouteRequest.current?.abort();
    const controller = new AbortController();
    exactRouteRequest.current = controller;
    setExactRouteLoading(true);
    setRouteError("");
    try {
      const response = await fetchExactWalkingRoute(selectedId, surfaceDestination, { signal: controller.signal });
      if (exactRouteRequest.current !== controller) return;
      if (response.route?.status !== "available") throw new Error(response.message || "Exact walking route unavailable for this destination.");
      setExactSurfaceRoute(response);
      setMapMode("exact");
    } catch (routeFetchError) {
      if (routeFetchError.name !== "AbortError" && exactRouteRequest.current === controller) setRouteError(routeFetchError.message || "Exact walking route unavailable for this destination.");
    } finally {
      if (exactRouteRequest.current === controller) setExactRouteLoading(false);
    }
  }

  function changeTimePeriod(value) {
    setSelectedRoute(null);
    setRouteError("");
    setRouteLoading(true);
    setTimePeriod(value);
  }

  function toggleCompareListing(listing) {
    setComparisonListings((current) => {
      const id = String(listing.listing_id);
      if (current.some((candidate) => String(candidate.listing_id) === id)) {
        return current.filter((candidate) => String(candidate.listing_id) !== id);
      }
      return current.length >= PRODUCT_CONFIG.compareLimit ? current : [...current, listing];
    });
  }

  const mapCount = mapResponse.count;
  return (
    <div className="app-shell">
      <header className="topbar">
        <div><p className="eyebrow">Western University</p><h1>Find housing that fits your life</h1></div>
        <div className="top-actions">
          <button aria-pressed={savedOnly} onClick={() => { setSavedOnly((value) => !value); clearSelection(); }}>Saved <span>{savedIds.length}</span></button>
          <button disabled={compareIds.length < 2} title={compareIds.length < 2 ? "Select at least 2 listings to compare" : undefined} onClick={() => setShowComparison(true)}>Compare <span>{compareIds.length}</span></button>
        </div>
      </header>

      <section className="searchbar" aria-label="Housing filters">
        <label className="search-field"><span>Search listings</span><input value={filters.search} onChange={(event) => updateFilter("search", event.target.value)} placeholder="Address, housing type, amenity" /></label>
        <label className="field"><span>Maximum monthly rent</span><input type="number" min="0" value={filters.max_price} onChange={(event) => updateFilter("max_price", event.target.value)} placeholder="$1,000" /></label>
        <FilterSelect label="Bedrooms" value={filters.bedrooms} onChange={(value) => updateFilter("bedrooms", value)}><option value="">Any</option>{[1, 2, 3, 4, 5].map((value) => <option key={value}>{value}</option>)}</FilterSelect>
        <button className="shortcut-toggle" aria-pressed={filters.roommates_wanted === "true"} onClick={() => updateFilter("roommates_wanted", filters.roommates_wanted === "true" ? "" : "true")}>Roommates Wanted</button>
        <FilterSelect label="Walk to Western" value={filters.max_walk_minutes} onChange={(value) => updateFilter("max_walk_minutes", value)}><option value="">Any</option>{COMMUTE_THRESHOLDS.map((value) => <option key={value} value={value}>Within {value} min</option>)}</FilterSelect>
        <FilterSelect label="Sort results" value={sort} onChange={changeSort}>{SORT_OPTIONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</FilterSelect>
        <details className="more-filters">
          <summary>More filters</summary>
          <div className="filter-popover">
            <FilterSelect label="Destination" value={hotspotId} onChange={changeHotspot}>
              {hotspots.map((hotspot) => <option key={hotspot.id} value={hotspot.id}>{hotspot.name}</option>)}
              {!hotspots.length && <option value="western-main-campus">Western main campus</option>}
            </FilterSelect>
            <FilterSelect label="Morning transit to Western" value={filters.max_transit_minutes} onChange={(value) => updateFilter("max_transit_minutes", value)}><option value="">Any</option>{COMMUTE_THRESHOLDS.map((value) => <option key={value} value={value}>Within {value} min</option>)}</FilterSelect>
            <FilterSelect label="Housing type" value={filters.housing_type} onChange={(value) => updateFilter("housing_type", value)}><option value="">Any</option>{filterOptions.housingTypes.map((value) => <option key={value} value={value}>{housingTypeLabel(value)}</option>)}</FilterSelect>
            <FilterSelect label="Lease type" value={filters.lease_type} onChange={(value) => updateFilter("lease_type", value)}><option value="">Any</option>{filterOptions.leaseTypes.map((value) => <option key={value} value={value}>{leaseTypeLabel(value)}</option>)}</FilterSelect>
            <FilterSelect label="Gender preference" value={filters.preferred_gender} onChange={(value) => updateFilter("preferred_gender", value)}><option value="">Any</option>{filterOptions.genders.map((value) => <option key={value} value={value}>{genderLabel(value)}</option>)}</FilterSelect>
            <FilterSelect label="Sublet status" value={filters.is_sublet} onChange={(value) => updateFilter("is_sublet", value)}><option value="">Any</option><option value="true">Explicit sublets only</option><option value="false">Explicitly confirmed non-sublets</option></FilterSelect>
            <FilterSelect label="Summer availability" value={filters.summer_available} onChange={(value) => updateFilter("summer_available", value)}><option value="">Any</option><option value="true">Summer available</option><option value="false">Not summer-only</option></FilterSelect>
            <FilterSelect label="Furnished" value={filters.furnished} onChange={(value) => updateFilter("furnished", value)}><option value="">Any</option><option value="true">Yes</option><option value="false">No</option></FilterSelect>
            <FilterSelect label="Utilities" value={filters.utilities_included} onChange={(value) => updateFilter("utilities_included", value)}><option value="">Any</option><option value="true">All included</option><option value="false">Utilities extra</option></FilterSelect>
            <fieldset className="must-have-filters">
              <legend>Must have</legend>
              <FilterCheckbox label="Laundry" checked={filters.laundry === "true"} onChange={(checked) => updatePositiveRequirement("laundry", checked)} />
              <FilterCheckbox label="Parking" checked={filters.parking_available === "true"} onChange={(checked) => updatePositiveRequirement("parking_available", checked)} />
              <FilterCheckbox label="Map ready" checked={filters.map_ready === "true"} onChange={(checked) => updatePositiveRequirement("map_ready", checked)} />
            </fieldset>
            <FilterSelect label="Data quality" value={filters.data_quality} onChange={(value) => updateFilter("data_quality", value)}><option value="">Any</option><option value="confirmed">Verified</option><option value="parsed">Parsed</option><option value="needs-review">Needs review</option></FilterSelect>
            <FilterSelect label="Ranking availability" value={filters.ranking_status} onChange={(value) => updateFilter("ranking_status", value)}><option value="">Any ranking status</option><option value="ranked">Overall score available</option><option value="partial">Partial ranking data</option><option value="excluded">Not enough data to rank</option></FilterSelect>
            <FilterSelect label="Minimum overall match" value={filters.min_score} onChange={(value) => updateFilter("min_score", value)}><option value="">Any score</option><option value="50">50 or higher</option><option value="60">60 or higher</option><option value="70">70 or higher</option><option value="80">80 or higher</option></FilterSelect>
            <label className="field"><span>Maximum destination distance (km)</span><input type="number" min="0" step="0.5" value={filters.max_distance_km} onChange={(event) => updateFilter("max_distance_km", event.target.value)} /></label>
            <label className="field"><span>Minimum monthly rent</span><input type="number" min="0" value={filters.min_price} onChange={(event) => updateFilter("min_price", event.target.value)} /></label>
            <button className="reset-button" onClick={resetFilters}>Reset all filters</button>
          </div>
        </details>
      </section>

      <div className="mobile-toggle" role="group" aria-label="Results view">
        <button className={mobilePane === "list" ? "active" : ""} onClick={() => setMobilePane("list")}>List</button>
        <button className={mobilePane === "map" ? "active" : ""} onClick={() => setMobilePane("map")}>Map</button>
      </div>

      <main className="workspace">
        <section className={`results-pane ${mobilePane === "list" ? "mobile-active" : ""}`} aria-label="Housing results" aria-busy={loading}>
          <div className="results-summary">
            <div><strong>{listingsResponse.total}</strong> results · <span>{mapCount} shown on map</span></div>
            <button onClick={() => setFitRequest((value) => value + 1)}>Fit map to results</button>
          </div>
          {savedOnly && <div className="saved-view-note">Showing all saved listings. Filters are paused until you change one.</div>}
          {loading && <div className="state-card" role="status">Loading housing listings…</div>}
          {error && <div className="state-card error" role="alert"><strong>Listings could not be loaded.</strong><p>{error}</p><button onClick={() => setRetry((value) => value + 1)}>Try again</button></div>}
          {!loading && !error && !listings.length && <div className="state-card"><strong>{savedOnly ? "No saved listings yet." : "No listings match these filters."}</strong><p>{savedOnly ? "Use Save on a listing to build a shortlist without an account." : "Try increasing your commute time or budget, or reset a filter."}</p>{!savedOnly && <button onClick={resetFilters}>Reset filters</button>}</div>}
          <div className="listing-list">
            {listings.map((listing) => (
              <ListingCard
                key={listing.listing_id}
                listing={listing}
                selected={String(listing.listing_id) === String(selectedId)}
                saved={savedIds.includes(String(listing.listing_id))}
                comparing={compareIds.includes(String(listing.listing_id))}
                compareDisabled={compareIds.length >= PRODUCT_CONFIG.compareLimit}
                onSelect={() => selectListing(listing.listing_id)}
                onSave={() => setSavedIds((current) => toggleShortlist(current, listing.listing_id))}
                onCompare={() => toggleCompareListing(
                  String(listing.listing_id) === String(selectedId) && transportationOverview
                    ? { ...listing, transportation: transportationOverview.transportation }
                    : listing,
                )}
              />
            ))}
          </div>
          {!savedOnly && listingsResponse.pages > 1 && (
            <nav className="pagination" aria-label="Listing pages">
              <button disabled={page <= 1} onClick={() => changePage(page - 1)}>Previous</button>
              <span>Page {page} of {listingsResponse.pages}</span>
              <button disabled={page >= listingsResponse.pages} onClick={() => changePage(page + 1)}>Next</button>
            </nav>
          )}
        </section>

        <section
          className={`map-pane ${mobilePane === "map" ? "mobile-active" : ""}`}
          aria-label="Map results"
          data-map-listing-count={mapCount}
        >
          <div className="map-mode-control" role="group" aria-label="Map view">
            <button className={mapMode === "listings" ? "active" : ""} aria-pressed={mapMode === "listings"} onClick={() => { setMapMode("listings"); setSurfaceDestination(null); }}>Listings</button>
            <button className={mapMode !== "listings" ? "active" : ""} aria-pressed={mapMode !== "listings"} disabled={!selectedId || !locationVisibility(selectedListing).route_available} title={selectedId && !locationVisibility(selectedListing).route_available ? "Walking information is unavailable for this listing location" : undefined} onClick={enableWalkingMap}>Walk time</button>
          </div>
          {mapMode === "walking" && surfaceState.status !== "ready" && <div className={`surface-notice ${surfaceState.error ? "error" : ""}`} role={surfaceState.error ? "alert" : "status"}>{surfaceState.error || "Calculating walking map…"}</div>}
          {mapMode === "listings" && mapError && <div className="surface-notice error" role="alert">Map listings could not be loaded. {mapError}</div>}
          <ListingMap
            listings={mapListings}
            hotspots={hotspots}
            selectedListingId={selectedId}
            selectedHotspotId={hotspotId}
            onSelectListing={selectListing}
            onSelectHotspot={changeHotspot}
            route={exactSurfaceRoute || selectedRoute}
            fitRequest={fitRequest}
            walkingSurface={mapMode === "walking" && surfaceState.status === "ready" ? { grid: surfaceGrid, values: surfaceState.values, validity: surfaceState.validity } : null}
            onSurfaceHover={setSurfaceHover}
            onSurfaceDestination={(destination) => setSurfaceDestination({ ...destination, text: formatApproximateMinutes(destination.seconds, surfaceGrid?.display_rounding_seconds) })}
            surfaceDestination={surfaceDestination}
          />
          {mapMode === "listings" && <div className="map-legend"><span><i className="legend-listing" />Listing</span><span><i className="legend-hotspot" />Destination</span><small>Solid = bus · short dashes = walk · long dashes = bike</small></div>}
          {mapMode === "walking" && surfaceState.status === "ready" && <div className="surface-legend"><strong>Walking from {selectedListing?.address || "selected home"}</strong><small>Approximate walking area</small>{[["0–10", "#2d8f6f"], ["10–20", "#5aad54"], ["20–30", "#a6c74a"], ["30–45", "#f0c84b"], ["45–60", "#ef9541"], ["60–90", "#d85d43"], ["90–120 min", "#a93658"]].map(([label, colour]) => <span key={label}><i style={{ background: colour }} />{label}</span>)}</div>}
          {mapMode === "walking" && surfaceHover && <div className="surface-hover" aria-live="polite">{surfaceHover.text}</div>}
          {(mapMode === "walking" || mapMode === "exact") && surfaceDestination && <div className={`surface-destination ${selectedListing ? "with-detail" : ""}`}><strong>{mapMode === "exact" ? "Exact walking route" : "Approx. walk from this home"}</strong><span>{mapMode === "exact" ? `${Math.round(exactSurfaceRoute.route.duration_seconds / 60)} min · ${exactSurfaceRoute.route.distance_meters >= 1000 ? `${(exactSurfaceRoute.route.distance_meters / 1000).toFixed(1)} km` : `${exactSurfaceRoute.route.distance_meters} m`}` : surfaceDestination.text}</span>{mapMode === "walking" && <button disabled={exactRouteLoading} onClick={showExactWalkingRoute}>{exactRouteLoading ? "Finding exact walking route…" : "Show exact route"}</button>}{mapMode === "exact" && <button onClick={() => { setExactSurfaceRoute(null); setMapMode("walking"); }}>Back to walking map</button>}<button onClick={() => { setSurfaceDestination(null); setExactSurfaceRoute(null); setMapMode("walking"); }}>Clear destination</button></div>}
        </section>

        <DetailPanel
          listing={selectedListing}
          transportation={transportationOverview}
          selectedRoute={selectedRoute}
          routeLoading={routeLoading}
          mode={travelMode}
          onMode={changeTravelMode}
          timePeriod={timePeriod}
          timePeriods={timePeriods}
          onTimePeriod={changeTimePeriod}
          onClose={clearSelection}
          onShowMap={() => setMobilePane("map")}
          saved={selectedListing ? savedIds.includes(String(selectedListing.listing_id)) : false}
          onSave={() => selectedListing && setSavedIds((current) => toggleShortlist(current, selectedListing.listing_id))}
        />
        {routeError && <div className="route-error" role="alert">{routeError}</div>}
      </main>
      {showComparison && <Comparison listings={comparisonListings} onClose={() => setShowComparison(false)} />}
    </div>
  );
}
