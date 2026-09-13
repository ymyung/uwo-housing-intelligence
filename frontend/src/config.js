const environment = import.meta.env || {};

export const PRODUCT_CONFIG = Object.freeze({
  apiBaseUrl: environment.VITE_API_BASE_URL || "",
  map: Object.freeze({
    provider: environment.VITE_MAP_PROVIDER || "leaflet",
    tileUrl:
      environment.VITE_MAP_TILE_URL ||
      "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution:
      environment.VITE_MAP_ATTRIBUTION || "&copy; OpenStreetMap contributors",
    initialCenter: Object.freeze([43.0096, -81.2737]),
    initialZoom: 13,
  }),
  routingProvider: environment.VITE_ROUTING_PROVIDER || "straight_line_estimate",
  liveTransitEnabled: environment.VITE_LIVE_TRANSIT_ENABLED === "true",
  walkingSpeedKmh: Number(environment.VITE_WALKING_SPEED_KMH || 4.8),
  cyclingSpeedKmh: Number(environment.VITE_CYCLING_SPEED_KMH || 15),
  compareLimit: 3,
});
