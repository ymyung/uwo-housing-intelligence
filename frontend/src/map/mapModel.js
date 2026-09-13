import { mapReadyListings, numberOrNull } from "../domain/listings.js";

export const MAX_CLUSTER_ZOOM = 18;

export function hasVisibleMapSize(container) {
  return Number(container?.offsetWidth) > 0 && Number(container?.offsetHeight) > 0;
}

export function clusterListings(listings, zoom) {
  const precision = zoom >= 16 ? 5 : zoom >= 14 ? 3 : 2;
  const buckets = new Map();
  for (const listing of mapReadyListings(listings)) {
    const latitude = numberOrNull(listing.latitude);
    const longitude = numberOrNull(listing.longitude);
    const key = `${latitude.toFixed(precision)}:${longitude.toFixed(precision)}`;
    const bucket = buckets.get(key) || { latitude, longitude, listings: [] };
    bucket.listings.push(listing);
    buckets.set(key, bucket);
  }
  return [...buckets.values()];
}

export function orderedClusterListings(listings) {
  return [...listings].sort((left, right) => {
    const leftPrice = numberOrNull(left.price_monthly);
    const rightPrice = numberOrNull(right.price_monthly);
    if (leftPrice !== rightPrice) {
      if (leftPrice === null) return 1;
      if (rightPrice === null) return -1;
      return leftPrice - rightPrice;
    }
    const leftLabel = left.address || left.title || "";
    const rightLabel = right.address || right.title || "";
    return leftLabel.localeCompare(rightLabel)
      || String(left.listing_id).localeCompare(String(right.listing_id), undefined, {
        numeric: true,
      });
  });
}

export function clusterInteraction(cluster, zoom) {
  const listings = orderedClusterListings(cluster.listings);
  if (listings.length <= 1) return { kind: "listing", listings };
  if (zoom < MAX_CLUSTER_ZOOM) {
    return {
      kind: "zoom",
      listings,
      zoom: Math.min(MAX_CLUSTER_ZOOM, zoom + 2),
    };
  }
  return { kind: "choose", listings };
}

export function selectClusterListing(onSelectListing, listingId) {
  onSelectListing(listingId, "map");
}
