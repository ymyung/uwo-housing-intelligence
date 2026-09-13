import { PRODUCT_CONFIG } from "./config.js";
import { apiUrl } from "./domain/apiUrl.js";

async function request(path, { signal } = {}) {
  const response = await fetch(apiUrl(path, PRODUCT_CONFIG.apiBaseUrl), { signal });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // A user-safe status is enough when the server did not return JSON.
    }
    throw new Error(message);
  }
  return response.json();
}

export function fetchListings(parameters, options) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(parameters)) {
    if (value !== "" && value !== null && value !== undefined) {
      query.set(key, String(value));
    }
  }
  return request(`/api/listings?${query}`, options);
}

export function fetchMapListings(parameters, options) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(parameters)) {
    if (value !== "" && value !== null && value !== undefined) {
      query.set(key, String(value));
    }
  }
  return request(`/api/listings/map?${query}`, options);
}

export function fetchListing(listingId, options) {
  return request(`/api/listings/${encodeURIComponent(listingId)}`, options);
}

export function fetchHotspots(options) {
  return request("/api/hotspots", options);
}

export function fetchTransportationOverview(listingId, hotspotId, options) {
  const query = new URLSearchParams({ hotspot_id: hotspotId });
  return request(
    `/api/listings/${encodeURIComponent(listingId)}/accessibility?${query}`,
    options,
  );
}

export function fetchTransportationRoute(listingId, hotspotId, mode, timePeriod, options) {
  const query = new URLSearchParams({ hotspot_id: hotspotId, mode, include_geometry: "true" });
  if (mode === "transit" && timePeriod) query.set("time_period", timePeriod);
  return request(
    `/api/listings/${encodeURIComponent(listingId)}/accessibility?${query}`,
    options,
  );
}

export const fetchAccessibility = fetchTransportationRoute;

export function fetchAccessibilityTimePeriods(options) {
  return request("/api/accessibility/time-periods", options);
}

export function fetchWalkingSurfaceGrid(options) {
  return request("/api/travel-time-surfaces/grid", options);
}

export function fetchWalkingSurface(listingId, options) {
  return request(`/api/listings/${encodeURIComponent(listingId)}/travel-time-surface?mode=walking`, options);
}

async function binaryRequest(path, { signal } = {}) {
  const response = await fetch(apiUrl(path, PRODUCT_CONFIG.apiBaseUrl), { signal });
  if (!response.ok) throw new Error(response.status === 409 ? "Walking map is still being calculated." : "Walking map data is unavailable.");
  return response.arrayBuffer(); // Browsers transparently decode Content-Encoding: gzip.
}

export function fetchWalkingSurfaceValues(surfaceId, options) {
  return binaryRequest(`/api/travel-time-surfaces/${encodeURIComponent(surfaceId)}/values`, options);
}

export function fetchWalkingSurfaceValidity(surfaceId, options) {
  return binaryRequest(`/api/travel-time-surfaces/${encodeURIComponent(surfaceId)}/validity`, options);
}

export async function fetchExactWalkingRoute(listingId, destination, { signal } = {}) {
  const response = await fetch(apiUrl(`/api/listings/${encodeURIComponent(listingId)}/routes/walking`, PRODUCT_CONFIG.apiBaseUrl), {
    method: "POST", signal, headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ destination_latitude: destination.latitude, destination_longitude: destination.longitude }),
  });
  if (!response.ok) throw new Error(response.status === 422 ? "Exact walking route unavailable for this destination." : "Exact walking route is temporarily unavailable.");
  return response.json();
}
