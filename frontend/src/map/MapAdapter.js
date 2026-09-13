/**
 * Provider-neutral map contract used by product UI and tests.
 * A future GoogleMapAdapter or MapboxMapAdapter should implement these methods.
 */
export class MapAdapter {
  setListings(listings) {
    void listings;
    throw new Error("setListings is not implemented");
  }
  setHotspots(hotspots) {
    void hotspots;
    throw new Error("setHotspots is not implemented");
  }
  selectListing(listingId) {
    void listingId;
    throw new Error("selectListing is not implemented");
  }
  fitToListings() {
    throw new Error("fitToListings is not implemented");
  }
  showRoute(route) {
    void route;
    throw new Error("showRoute is not implemented");
  }
  destroy() {}
}

export class MockMapAdapter extends MapAdapter {
  constructor() {
    super();
    this.state = { listings: [], hotspots: [], selectedListingId: null, route: null, fitted: false };
  }
  setListings(listings) {
    this.state.listings = listings;
  }
  setHotspots(hotspots) {
    this.state.hotspots = hotspots;
  }
  selectListing(listingId) {
    this.state.selectedListingId = listingId;
  }
  fitToListings() {
    this.state.fitted = true;
  }
  showRoute(route) {
    this.state.route = route;
  }
  destroy() {
    this.state.destroyed = true;
  }
}
