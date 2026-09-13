const STATUS_COPY = {
  walking_better_than_transit: "Walking is faster or more practical than transit for this period.",
  no_route: "No useful transit route was found for this period.",
  technical_failure: "Routing is temporarily unavailable. This is not a transit-service finding.",
  unavailable: "Transportation estimate unavailable",
  stale: "This transportation estimate is stale and needs refresh.",
};

export function durationText(value) {
  return Number.isFinite(Number(value)) ? `${Math.round(Number(value))} min` : "Unavailable";
}

export function distanceText(meters) {
  const value = Number(meters);
  if (!Number.isFinite(value)) return "Distance unavailable";
  return value >= 1000 ? `${(value / 1000).toFixed(1)} km` : `${Math.round(value)} m`;
}

export function transportStatusText(value, mode = "transit") {
  if (!value) return STATUS_COPY.unavailable;
  if (STATUS_COPY[value.status]) return STATUS_COPY[value.status];
  if (value.review_required) return "Transit estimate has a review flag.";
  if (value.status === "partial") return "A partial scheduled estimate is available.";
  if (value.duration_minutes != null) {
    if (value.result_type === "same_stop_reuse") {
      return `Same-stop estimate · ${durationText(value.duration_minutes)}`;
    }
    return `${mode === "transit" ? "Scheduled estimate · " : ""}${durationText(value.duration_minutes)}`;
  }
  return STATUS_COPY.unavailable;
}

export function transportCardItems(transportation) {
  if (!transportation || transportation.availability === "unavailable") return [];
  return [
    ["Walk", transportation.walking],
    ["Bike", transportation.cycling],
    ["Transit", transportation.transit],
  ].map(([label, value]) => ({
    label,
    value: value?.duration_minutes == null ? "—" : `${Math.round(value.duration_minutes)} min`,
    status: value?.status || "unavailable",
  }));
}

export function decodePolyline(value, precision = 5) {
  if (typeof value !== "string" || !value.length) return [];
  const points = [];
  let index = 0;
  let latitude = 0;
  let longitude = 0;
  const factor = 10 ** precision;
  while (index < value.length) {
    const deltas = [];
    for (let coordinate = 0; coordinate < 2; coordinate += 1) {
      let result = 0;
      let shift = 0;
      let byte;
      do {
        if (index >= value.length) return [];
        byte = value.charCodeAt(index) - 63;
        index += 1;
        if (byte < 0 || byte > 63 || shift > 60) return [];
        result |= (byte & 0x1f) << shift;
        shift += 5;
      } while (byte >= 0x20);
      deltas.push(result & 1 ? ~(result >> 1) : result >> 1);
    }
    latitude += deltas[0];
    longitude += deltas[1];
    const point = [latitude / factor, longitude / factor];
    if (!point.every(Number.isFinite)) return [];
    points.push(point);
  }
  return points.length >= 2 ? points : [];
}

const LEG_STYLE = {
  WALK: { color: "#6f2da8", weight: 5, dashArray: "3 8", opacity: 0.9 },
  BICYCLE: { color: "#147d92", weight: 5, dashArray: "12 6", opacity: 0.9 },
  BUS: { color: "#6f2da8", weight: 6, dashArray: null, opacity: 0.95 },
};

export function routeSegments(routeResponse) {
  const route = routeResponse?.route || routeResponse;
  const legs = route?.itinerary?.legs;
  if (!Array.isArray(legs) || route?.itinerary?.geometry_available !== true) return [];
  return legs.flatMap((leg, index) => {
    const positions = decodePolyline(leg.encoded_polyline);
    if (!positions.length) return [];
    return [{
      key: `${index}-${leg.mode}-${leg.route_id || "direct"}`,
      mode: leg.mode,
      positions,
      style: LEG_STYLE[leg.mode] || LEG_STYLE.BUS,
      label: leg.mode === "BUS"
        ? `Bus ${leg.route_short_name || leg.route_long_name || "route"}`
        : leg.mode === "BICYCLE" ? "Bike" : "Walk",
    }];
  });
}

export function routePoints(routeResponse) {
  return routeSegments(routeResponse).flatMap((segment) => segment.positions);
}

export function transitLegRows(routeResponse) {
  const route = routeResponse?.route || routeResponse;
  const legs = route?.itinerary?.legs;
  if (!Array.isArray(legs)) return [];
  return legs.map((leg, index) => ({
    key: `${index}-${leg.mode}-${leg.route_id || "direct"}`,
    mode: leg.mode,
    label: leg.mode === "BUS"
      ? `Bus ${leg.route_short_name || leg.route_long_name || "route"}`
      : leg.mode === "BICYCLE" ? "Bike" : "Walk",
    duration: leg.duration_seconds == null ? null : `${Math.round(leg.duration_seconds / 60)} min`,
    distance: distanceText(leg.distance_meters),
    fromStop: leg.from_stop?.name || null,
    toStop: leg.to_stop?.name || null,
    departure: leg.scheduled_departure_at || null,
    arrival: leg.scheduled_arrival_at || null,
  }));
}

export function selectedModeSummary(overview, mode, timePeriod) {
  const transportation = overview?.transportation || overview;
  if (!transportation) return null;
  if (mode === "walking") return transportation.walking;
  if (mode === "cycling") return transportation.cycling;
  return transportation.transit?.periods?.find((period) => period.time_period === timePeriod) || null;
}
