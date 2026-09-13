export const WALKING_SURFACE_SENTINEL = 65535;
export const WALKING_BANDS = [
  [10, "#2d8f6f"], [20, "#5aad54"], [30, "#a6c74a"], [45, "#f0c84b"],
  [60, "#ef9541"], [90, "#d85d43"], [120, "#a93658"],
];

export function decodeWalkingValues(buffer, grid) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength !== Number(grid?.cell_count) * 2) {
    throw new Error("Walking map data has an unexpected length.");
  }
  const values = new Uint16Array(buffer.byteLength / 2);
  const view = new DataView(buffer);
  for (let index = 0; index < values.length; index += 1) values[index] = view.getUint16(index * 2, true);
  return values;
}

export function decodeValidity(buffer, grid) {
  const bytes = Math.ceil(Number(grid?.cell_count) / 8);
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength !== bytes) throw new Error("Walking map validity data has an unexpected length.");
  return new Uint8Array(buffer);
}

export function isValidCell(validity, index) {
  return Boolean(validity?.[Math.floor(index / 8)] & (1 << (index % 8)));
}

export function surfaceCellIndex(grid, row, column) {
  if (!Number.isInteger(row) || !Number.isInteger(column) || row < 0 || column < 0 || row >= grid.rows || column >= grid.columns) return null;
  return row * grid.columns + column;
}

export function formatApproximateMinutes(seconds, roundingSeconds = 60) {
  if (!Number.isFinite(seconds) || seconds < 0 || seconds === WALKING_SURFACE_SENTINEL) return "Walking estimate unavailable here";
  const rounded = Math.round(seconds / roundingSeconds) * roundingSeconds;
  return `≈ ${Math.max(1, Math.round(rounded / 60))} min walk`;
}

export function surfaceColour(seconds) {
  const minutes = seconds / 60;
  return WALKING_BANDS.find(([limit]) => minutes <= limit)?.[1] || null;
}

export function canRenderSurface(metadata, grid) {
  return metadata?.status === "ready" && metadata?.grid?.grid_fingerprint === grid?.grid_fingerprint;
}

// WGS84 <-> UTM zone 17N (EPSG:26917). This is a geographic projection, not
// a degree-spacing approximation of the accepted metric grid.
const A = 6378137;
const ECC_SQUARED = 0.00669438;
const K0 = 0.9996;
const LONGITUDE_ORIGIN = -81;

export function latLngToUtm17(latitude, longitude) {
  const rad = Math.PI / 180;
  const eccPrime = ECC_SQUARED / (1 - ECC_SQUARED);
  const latRad = latitude * rad;
  const n = A / Math.sqrt(1 - ECC_SQUARED * Math.sin(latRad) ** 2);
  const t = Math.tan(latRad) ** 2;
  const c = eccPrime * Math.cos(latRad) ** 2;
  const aa = Math.cos(latRad) * (longitude - LONGITUDE_ORIGIN) * rad;
  const m = A * ((1 - ECC_SQUARED / 4 - 3 * ECC_SQUARED ** 2 / 64 - 5 * ECC_SQUARED ** 3 / 256) * latRad
    - (3 * ECC_SQUARED / 8 + 3 * ECC_SQUARED ** 2 / 32 + 45 * ECC_SQUARED ** 3 / 1024) * Math.sin(2 * latRad)
    + (15 * ECC_SQUARED ** 2 / 256 + 45 * ECC_SQUARED ** 3 / 1024) * Math.sin(4 * latRad)
    - (35 * ECC_SQUARED ** 3 / 3072) * Math.sin(6 * latRad));
  return {
    x: K0 * n * (aa + (1 - t + c) * aa ** 3 / 6 + (5 - 18 * t + t ** 2 + 72 * c - 58 * eccPrime) * aa ** 5 / 120) + 500000,
    y: K0 * (m + n * Math.tan(latRad) * (aa ** 2 / 2 + (5 - t + 9 * c + 4 * c ** 2) * aa ** 4 / 24 + (61 - 58 * t + t ** 2 + 600 * c - 330 * eccPrime) * aa ** 6 / 720)),
  };
}

export function utm17ToLatLng(x, y) {
  const rad = Math.PI / 180;
  const eccPrime = ECC_SQUARED / (1 - ECC_SQUARED);
  const e1 = (1 - Math.sqrt(1 - ECC_SQUARED)) / (1 + Math.sqrt(1 - ECC_SQUARED));
  const m = y / K0;
  const mu = m / (A * (1 - ECC_SQUARED / 4 - 3 * ECC_SQUARED ** 2 / 64 - 5 * ECC_SQUARED ** 3 / 256));
  const phi1 = mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * Math.sin(2 * mu) + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * Math.sin(4 * mu) + (151 * e1 ** 3 / 96) * Math.sin(6 * mu);
  const n1 = A / Math.sqrt(1 - ECC_SQUARED * Math.sin(phi1) ** 2);
  const t1 = Math.tan(phi1) ** 2;
  const c1 = eccPrime * Math.cos(phi1) ** 2;
  const r1 = A * (1 - ECC_SQUARED) / (1 - ECC_SQUARED * Math.sin(phi1) ** 2) ** 1.5;
  const d = (x - 500000) / (n1 * K0);
  const latitude = phi1 - (n1 * Math.tan(phi1) / r1) * (d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * eccPrime) * d ** 4 / 24 + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * eccPrime - 3 * c1 ** 2) * d ** 6 / 720);
  const longitude = (d - (1 + 2 * t1 + c1) * d ** 3 / 6 + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * eccPrime + 24 * t1 ** 2) * d ** 5 / 120) / Math.cos(phi1);
  return { latitude: latitude / rad, longitude: LONGITUDE_ORIGIN + longitude / rad };
}

export function cellFromLatLng(grid, latitude, longitude) {
  const { x, y } = latLngToUtm17(latitude, longitude);
  const column = Math.floor((x - grid.extent.min_x) / grid.cell_size_metres);
  const row = Math.floor((y - grid.extent.min_y) / grid.cell_size_metres);
  return surfaceCellIndex(grid, row, column);
}

export function nextSurfaceState(metadata) {
  if (!metadata) return "idle";
  if (metadata.status === "computing") return "computing";
  if (metadata.status === "ready") return "ready";
  if (metadata.status === "unavailable") return "unavailable";
  return "failed";
}
