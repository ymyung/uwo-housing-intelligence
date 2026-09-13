import assert from "node:assert/strict";
import test from "node:test";

import {
  fetchExactWalkingRoute,
  fetchListings,
  fetchMapListings,
  fetchWalkingSurfaceValues,
} from "../src/api.js";
import { PRODUCT_CONFIG } from "../src/config.js";
import { apiUrl } from "../src/domain/apiUrl.js";

test("API URLs default to same-origin and preserve an explicit origin override", () => {
  assert.equal(PRODUCT_CONFIG.apiBaseUrl, "");
  assert.equal(apiUrl("/api/listings?page=1"), "/api/listings?page=1");
  assert.equal(
    apiUrl("/api/listings", "https://api.example.test/"),
    "https://api.example.test/api/listings",
  );
});

test("listing, binary surface, and exact-route requests remain same-origin", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    return {
      ok: true,
      json: async () => ({ listings: [], route: { status: "available" } }),
      arrayBuffer: async () => new ArrayBuffer(0),
    };
  };

  try {
    await fetchListings({ page: 2, page_size: 100 });
    await fetchMapListings({ housing_type: "room", roommates_wanted: true });
    await fetchWalkingSurfaceValues("surface/one");
    await fetchExactWalkingRoute("listing/one", {
      latitude: 43.0096,
      longitude: -81.2737,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(
    requests.map(({ url }) => url),
    [
      "/api/listings?page=2&page_size=100",
      "/api/listings/map?housing_type=room&roommates_wanted=true",
      "/api/travel-time-surfaces/surface%2Fone/values",
      "/api/listings/listing%2Fone/routes/walking",
    ],
  );
  assert.equal(requests[3].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[3].options.body), {
    destination_latitude: 43.0096,
    destination_longitude: -81.2737,
  });
});
