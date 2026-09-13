import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  WALKING_SURFACE_SENTINEL,
  canRenderSurface,
  cellFromLatLng,
  decodeValidity,
  decodeWalkingValues,
  formatApproximateMinutes,
  isValidCell,
  nextSurfaceState,
  surfaceCellIndex,
  utm17ToLatLng,
} from "../src/domain/walkingSurface.js";

const grid = { cell_count: 4, rows: 2, columns: 2, cell_size_metres: 200, extent: { min_x: 468327, min_y: 4742068 }, grid_fingerprint: "grid" };

test("walking vectors decode little-endian uint16 and preserve unavailable sentinel", () => {
  const bytes = new Uint8Array([60, 0, 255, 255, 16, 14, 0, 0]).buffer;
  const values = decodeWalkingValues(bytes, grid);
  assert.deepEqual([...values], [60, WALKING_SURFACE_SENTINEL, 3600, 0]);
  assert.equal(formatApproximateMinutes(values[0]), "≈ 1 min walk");
  assert.equal(formatApproximateMinutes(values[1]), "Walking estimate unavailable here");
  assert.throws(() => decodeWalkingValues(new ArrayBuffer(2), grid), /unexpected length/);
});

test("validity is independent from unavailable sentinel and row-major lookup is bounded", () => {
  const validity = decodeValidity(new Uint8Array([0b00000101]).buffer, grid);
  assert.equal(isValidCell(validity, 0), true);
  assert.equal(isValidCell(validity, 1), false);
  assert.equal(surfaceCellIndex(grid, 1, 0), 2);
  assert.equal(surfaceCellIndex(grid, 2, 0), null);
});

test("surface state exposes only walking-ready compatible data", () => {
  assert.equal(nextSurfaceState(null), "idle");
  assert.equal(nextSurfaceState({ status: "computing" }), "computing");
  assert.equal(nextSurfaceState({ status: "unavailable" }), "unavailable");
  assert.equal(canRenderSurface({ status: "ready", grid: { grid_fingerprint: "grid" } }, grid), true);
  assert.equal(canRenderSurface({ status: "ready", grid: { grid_fingerprint: "old" } }, grid), false);
});

test("EPSG:26917 cell lookup returns a deterministic row-major cell", () => {
  const point = utm17ToLatLng(grid.extent.min_x + 100, grid.extent.min_y + 100);
  assert.equal(cellFromLatLng(grid, point.latitude, point.longitude), 0);
});

test("a valid walking destination exposes the exact-route action above the listing detail", async () => {
  const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
  const css = await readFile(new URL("../src/App.css", import.meta.url), "utf8");
  assert.match(app, /mapMode === "walking" && <button disabled=\{exactRouteLoading\} onClick=\{showExactWalkingRoute\}>/);
  assert.match(app, /Show exact route/);
  assert.match(css, /\.surface-destination \{ z-index: 800;/);
  assert.match(css, /\.detail-panel \{ position: absolute; z-index: 700;/);
});
