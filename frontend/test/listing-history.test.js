import test from "node:test";
import assert from "node:assert/strict";

import { historyEventText, observedDate } from "../src/domain/listingHistory.js";

test("student history formats a comparable monthly price change", () => {
  assert.equal(
    historyEventText({
      type: "PRICE_CHANGED",
      previous_value: 850,
      current_value: 800,
    }),
    "Price changed · $850 → $800",
  );
});

test("student history formats an availability change without internal metadata", () => {
  assert.equal(
    historyEventText({
      type: "AVAILABILITY_CHANGED",
      previous_value: { availability_text: "September" },
      current_value: { availability_text: "Available now" },
    }),
    "Availability changed · September → Available now",
  );
});

test("platform observation dates are explicit and stable", () => {
  assert.equal(observedDate("2026-08-01T23:30:00-04:00"), "Aug 2, 2026");
  assert.equal(observedDate(null), "Unknown");
});
