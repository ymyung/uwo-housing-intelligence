export const SHORTLIST_STORAGE_KEY = "uwo-housing-shortlist-v1";

export function loadShortlist(storage) {
  try {
    const value = JSON.parse(storage.getItem(SHORTLIST_STORAGE_KEY) || "[]");
    return Array.isArray(value) ? [...new Set(value.map(String))] : [];
  } catch {
    return [];
  }
}

export function saveShortlist(storage, listingIds) {
  storage.setItem(SHORTLIST_STORAGE_KEY, JSON.stringify([...new Set(listingIds.map(String))]));
}

export function toggleShortlist(listingIds, listingId) {
  const id = String(listingId);
  return listingIds.includes(id)
    ? listingIds.filter((candidate) => candidate !== id)
    : [...listingIds, id];
}

export function toggleComparison(listingIds, listingId, limit = 3) {
  const id = String(listingId);
  if (listingIds.includes(id)) return listingIds.filter((candidate) => candidate !== id);
  return listingIds.length >= limit ? listingIds : [...listingIds, id];
}
