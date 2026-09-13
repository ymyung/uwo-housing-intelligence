const CHANGE_LABELS = {
  AVAILABILITY_CHANGED: "Availability changed",
  LEASE_CHANGED: "Lease details changed",
  HOUSING_TYPE_CHANGED: "Housing type changed",
  UTILITIES_CHANGED: "Utilities changed",
  FURNISHING_CHANGED: "Furnishing changed",
  SUBLET_CHANGED: "Sublet status changed",
  GENDER_PREFERENCE_CHANGED: "Gender preference changed",
  ADDRESS_CHANGED: "Address changed",
};

function priceText(value) {
  if (value === null || value === undefined || value === "") return "Unknown";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return `$${numeric.toLocaleString("en-CA", { maximumFractionDigits: 2 })}`;
}

function simpleValue(value) {
  if (value === null || value === undefined || value === "") return "Unknown";
  if (typeof value === "object") {
    const informative = Object.values(value).find(
      (candidate) => candidate !== null && candidate !== undefined && candidate !== "",
    );
    return informative === undefined ? "Unknown" : String(informative).replaceAll("_", " ");
  }
  return String(value).replaceAll("_", " ");
}

export function historyEventText(event) {
  if (event?.type === "PRICE_CHANGED") {
    return `Price changed · ${priceText(event.previous_value)} → ${priceText(event.current_value)}`;
  }
  if (event?.type === "LISTING_REACTIVATED") return "Listing became available again";
  const label = CHANGE_LABELS[event?.type] || "Listing details changed";
  return `${label} · ${simpleValue(event?.previous_value)} → ${simpleValue(event?.current_value)}`;
}

export function observedDate(value) {
  if (!value) return "Unknown";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return "Unknown";
  return new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(parsed);
}
