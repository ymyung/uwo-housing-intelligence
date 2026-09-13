import { useEffect, useMemo, useState } from "react";
import L from "leaflet";
import {
  MapContainer,
  Marker,
  Polyline,
  Popup,
  TileLayer,
  useMap,
  useMapEvents,
} from "react-leaflet";
import "leaflet/dist/leaflet.css";

import { PRODUCT_CONFIG } from "../config.js";
import { formatPrice, housingTypeLabel, numberOrNull } from "../domain/listings.js";
import { routePoints, routeSegments } from "../domain/transportation.js";
import {
  clusterInteraction,
  clusterListings,
  hasVisibleMapSize,
  selectClusterListing,
} from "./mapModel.js";
import WalkingSurfaceCanvas from "./WalkingSurfaceCanvas.jsx";

const listingIcon = (selected = false) =>
  L.divIcon({
    className: `listing-pin ${selected ? "selected" : ""}`,
    html: '<span aria-hidden="true"></span>',
    iconSize: selected ? [30, 30] : [24, 24],
    iconAnchor: selected ? [15, 15] : [12, 12],
  });

const clusterIcon = (count) =>
  L.divIcon({
    className: "cluster-pin",
    html: `<span>${Number(count)}</span>`,
    iconSize: [38, 38],
    iconAnchor: [19, 19],
  });

const hotspotIcon = (selected) =>
  L.divIcon({
    className: `hotspot-pin ${selected ? "selected" : ""}`,
    html: '<span aria-hidden="true">◆</span>',
    iconSize: [30, 30],
    iconAnchor: [15, 15],
  });

function MapState({ listings, selectedListing, route, fitRequest, onZoom }) {
  const map = useMap();
  useMapEvents({ zoomend: () => onZoom(map.getZoom()) });
  const routedPoints = useMemo(() => routePoints(route), [route]);

  useEffect(() => {
    if (!selectedListing || routedPoints.length) return;
    const latitude = numberOrNull(selectedListing.latitude);
    const longitude = numberOrNull(selectedListing.longitude);
    if (latitude === null || longitude === null) return;
    const container = map.getContainer();
    function focusSelectedListing() {
      if (!hasVisibleMapSize(container)) return;
      map.invalidateSize({ pan: false });
      map.flyTo([latitude, longitude], Math.max(map.getZoom(), 15), { duration: 0.45 });
    }
    focusSelectedListing();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(focusSelectedListing);
    observer.observe(container);
    return () => observer.disconnect();
  }, [map, routedPoints.length, selectedListing]);

  useEffect(() => {
    if (routedPoints.length < 2) return undefined;
    const container = map.getContainer();
    function fitRoute() {
      if (!hasVisibleMapSize(container)) return;
      map.invalidateSize({ pan: false });
      map.fitBounds(routedPoints, { padding: [42, 42], maxZoom: 16 });
    }
    fitRoute();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(fitRoute);
    observer.observe(container);
    return () => observer.disconnect();
  }, [map, routedPoints]);

  useEffect(() => {
    if (!fitRequest) return;
    const points = listings
      .map((listing) => [numberOrNull(listing.latitude), numberOrNull(listing.longitude)])
      .filter(([latitude, longitude]) => latitude !== null && longitude !== null);
    if (points.length === 1) map.setView(points[0], 15);
    if (points.length > 1) map.fitBounds(points, { padding: [36, 36], maxZoom: 15 });
  }, [fitRequest, listings, map]);
  return null;
}

export default function ListingMap({
  listings,
  hotspots,
  selectedListingId,
  selectedHotspotId,
  onSelectListing,
  onSelectHotspot,
  route,
  fitRequest,
  walkingSurface,
  onSurfaceHover,
  onSurfaceDestination,
  surfaceDestination,
}) {
  const [zoom, setZoom] = useState(PRODUCT_CONFIG.map.initialZoom);
  const selectedListing = listings.find(
    (listing) => String(listing.listing_id) === String(selectedListingId),
  );
  const clusterCandidates = useMemo(
    () => listings.filter(
      (listing) => String(listing.listing_id) !== String(selectedListingId),
    ),
    [listings, selectedListingId],
  );
  const clusters = useMemo(
    () => clusterListings(clusterCandidates, zoom),
    [clusterCandidates, zoom],
  );
  const segments = useMemo(() => routeSegments(route), [route]);

  return (
    <MapContainer
      center={PRODUCT_CONFIG.map.initialCenter}
      zoom={PRODUCT_CONFIG.map.initialZoom}
      className="listing-map"
      aria-label="Housing listings map"
    >
      <TileLayer
        attribution={PRODUCT_CONFIG.map.attribution}
        url={PRODUCT_CONFIG.map.tileUrl}
      />
      <MapState
        listings={listings}
        selectedListing={selectedListing}
        route={route}
        fitRequest={fitRequest}
        onZoom={setZoom}
      />

      {walkingSurface?.values && walkingSurface?.validity && (
        <WalkingSurfaceCanvas
          grid={walkingSurface.grid}
          values={walkingSurface.values}
          validity={walkingSurface.validity}
          onHover={onSurfaceHover}
          onDestination={onSurfaceDestination}
        />
      )}

      {hotspots
        .filter((hotspot) => hotspot.latitude != null && hotspot.longitude != null)
        .map((hotspot) => (
          <Marker
            key={hotspot.id}
            position={[hotspot.latitude, hotspot.longitude]}
            icon={hotspotIcon(hotspot.id === selectedHotspotId)}
            eventHandlers={{ click: () => onSelectHotspot(hotspot.id) }}
          >
            <Popup>
              <strong>{hotspot.name}</strong>
              <br />
              {hotspot.category} destination
            </Popup>
          </Marker>
        ))}

      {clusters.map((cluster) => {
        if (cluster.listings.length > 1) {
          const interaction = clusterInteraction(cluster, zoom);
          const commonAddress = interaction.listings.every(
            (listing) => listing.address === interaction.listings[0].address,
          ) ? interaction.listings[0].address : null;
          const markerTitle = commonAddress
            ? `${interaction.listings.length} listings at ${commonAddress}`
            : `${interaction.listings.length} listings in this area`;
          return (
            <Marker
              key={`cluster-${cluster.latitude}-${cluster.longitude}`}
              position={[cluster.latitude, cluster.longitude]}
              icon={clusterIcon(cluster.listings.length)}
              title={markerTitle}
              eventHandlers={{
                click: (event) => {
                  if (interaction.kind !== "zoom") return;
                  event.target.closePopup();
                  event.target._map.setView(
                    [cluster.latitude, cluster.longitude],
                    interaction.zoom,
                  );
                },
              }}
            >
              <Popup
                minWidth={250}
                maxWidth={340}
                autoPanPaddingTopLeft={[10, 60]}
              >
                {interaction.kind === "choose" ? (
                  <div className="cluster-chooser">
                    <strong>{interaction.listings.length} listings at this location</strong>
                    <div className="cluster-chooser-list">
                      {interaction.listings.map((listing) => (
                        <button
                          key={listing.listing_id}
                          type="button"
                          data-listing-id={listing.listing_id}
                          onClick={(event) => {
                            event.stopPropagation();
                            selectClusterListing(onSelectListing, listing.listing_id);
                          }}
                        >
                          <span>{formatPrice(listing)} · {housingTypeLabel(listing.housing_type)}</span>
                          <small>{listing.address || listing.title || "Housing listing"}</small>
                          <b>View listing</b>
                        </button>
                      ))}
                    </div>
                  </div>
                ) : `${cluster.listings.length} listings in this area`}
              </Popup>
            </Marker>
          );
        }
        const listing = cluster.listings[0];
        const selected = String(listing.listing_id) === String(selectedListingId);
        return (
          <Marker
            key={listing.listing_id}
            position={[cluster.latitude, cluster.longitude]}
            icon={listingIcon(selected)}
            title={`Listing ${listing.listing_id}`}
            eventHandlers={{ click: () => onSelectListing(listing.listing_id, "map") }}
          >
            <Popup>
              <strong>{listing.address || listing.title || "Housing listing"}</strong>
              <br />
              {formatPrice(listing)}
            </Popup>
          </Marker>
        );
      })}

      {selectedListing
        && numberOrNull(selectedListing.latitude) !== null
        && numberOrNull(selectedListing.longitude) !== null && (
        <Marker
          key={`selected-${selectedListing.listing_id}`}
          position={[
            Number(selectedListing.latitude),
            Number(selectedListing.longitude),
          ]}
          icon={listingIcon(true)}
          title={`Listing ${selectedListing.listing_id}`}
          zIndexOffset={1000}
          eventHandlers={{ click: () => onSelectListing(selectedListing.listing_id, "map") }}
        >
          <Popup>
            <strong>{selectedListing.address || selectedListing.title || "Housing listing"}</strong>
            <br />
            {formatPrice(selectedListing)}
          </Popup>
        </Marker>
      )}

      {surfaceDestination && (
        <Marker position={[surfaceDestination.latitude, surfaceDestination.longitude]} zIndexOffset={1100}>
          <Popup><strong>Approx. walk from this home</strong><br />{surfaceDestination.text}</Popup>
        </Marker>
      )}

      {segments.map((segment) => (
        <Polyline
          key={segment.key}
          positions={segment.positions}
          pathOptions={segment.style}
        />
      ))}
    </MapContainer>
  );
}
