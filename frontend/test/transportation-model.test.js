import assert from "node:assert/strict";
import test from "node:test";

import {
  decodePolyline,
  routePoints,
  routeSegments,
  selectedModeSummary,
  transitLegRows,
  transportCardItems,
  transportStatusText,
} from "../src/domain/transportation.js";

const DIRECT_POLYLINE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@";

function routeResponse() {
  return {
    route: {
      itinerary: {
        geometry_available: true,
        legs: [
          {
            mode: "WALK",
            duration_seconds: 240,
            distance_meters: 300,
            encoded_polyline: DIRECT_POLYLINE,
          },
          {
            mode: "BUS",
            route_id: "27",
            route_short_name: "27",
            route_long_name: "Fanshawe - Western",
            duration_seconds: 600,
            distance_meters: 4000,
            encoded_polyline: DIRECT_POLYLINE,
            from_stop: { name: "University & Sunset" },
            to_stop: { name: "Natural Sciences" },
            scheduled_departure_at: "2026-06-15T12:00:00-04:00",
            scheduled_arrival_at: "2026-06-15T12:10:00-04:00",
          },
        ],
      },
    },
  };
}

test("encoded OTP polylines decode into Leaflet latitude/longitude points", () => {
  assert.deepEqual(decodePolyline(DIRECT_POLYLINE), [
    [38.5, -120.2],
    [40.7, -120.95],
    [43.252, -126.453],
  ]);
  assert.deepEqual(decodePolyline("bad"), []);
});

test("route rendering uses only persisted itinerary legs and distinct non-colour styles", () => {
  const segments = routeSegments(routeResponse());
  assert.equal(segments.length, 2);
  assert.equal(segments[0].mode, "WALK");
  assert.equal(segments[0].style.dashArray, "3 8");
  assert.equal(segments[1].mode, "BUS");
  assert.equal(segments[1].style.dashArray, null);
  assert.equal(routePoints(routeResponse()).length, 6);
  assert.deepEqual(routeSegments({ route: { origin: [43, -81], destination: [43.1, -81.2] } }), []);
});

test("transit itinerary text exposes the public route, stops, and scheduled times", () => {
  const rows = transitLegRows(routeResponse());
  assert.equal(rows[1].label, "Bus 27");
  assert.equal(rows[1].fromStop, "University & Sunset");
  assert.equal(rows[1].toStop, "Natural Sciences");
  assert.match(rows[1].departure, /12:00:00/);
});

test("special transport outcomes remain findings rather than fabricated zero values", () => {
  assert.match(transportStatusText({ status: "walking_better_than_transit" }), /Walking is faster/);
  assert.match(transportStatusText({ status: "no_route" }), /No useful transit route/);
  assert.match(transportStatusText({ status: "technical_failure" }), /temporarily unavailable/);
  assert.match(transportStatusText({ status: "partial" }), /partial scheduled estimate/);
  assert.match(transportStatusText({ review_required: true }), /review flag/);
  assert.equal(
    transportStatusText({ status: "available", result_type: "same_stop_reuse", duration_minutes: 22 }),
    "Same-stop estimate · 22 min",
  );
  assert.equal(transportCardItems({ availability: "unavailable" }).length, 0);
});

test("mode and period selection read the dedicated transportation contract", () => {
  const overview = {
    walking: { duration_minutes: 18 },
    cycling: { duration_minutes: 7 },
    transit: {
      periods: [
        { time_period: "weekday_morning_commute", duration_minutes: 16 },
        { time_period: "sunday_daytime", status: "no_route" },
      ],
    },
  };
  assert.equal(selectedModeSummary(overview, "walking").duration_minutes, 18);
  assert.equal(selectedModeSummary(overview, "cycling").duration_minutes, 7);
  assert.equal(
    selectedModeSummary(overview, "transit", "sunday_daytime").status,
    "no_route",
  );
});
