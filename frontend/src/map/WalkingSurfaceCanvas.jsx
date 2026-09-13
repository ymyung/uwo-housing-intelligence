import { useEffect, useMemo, useRef } from "react";
import { useMap, useMapEvents } from "react-leaflet";

import { cellFromLatLng, formatApproximateMinutes, isValidCell, surfaceColour, utm17ToLatLng, WALKING_SURFACE_SENTINEL } from "../domain/walkingSurface.js";

function geometryFor(grid) {
  const geometry = new Array(grid.cell_count);
  for (let row = 0; row < grid.rows; row += 1) for (let column = 0; column < grid.columns; column += 1) {
    const x = grid.extent.min_x + column * grid.cell_size_metres;
    const y = grid.extent.min_y + row * grid.cell_size_metres;
    geometry[row * grid.columns + column] = [utm17ToLatLng(x, y), utm17ToLatLng(x + grid.cell_size_metres, y + grid.cell_size_metres)];
  }
  return geometry;
}

function SurfaceEvents({ grid, values, validity, onHover, onDestination }) {
  const pending = useRef(null);
  useMapEvents({
    mousemove(event) {
      if (pending.current) cancelAnimationFrame(pending.current);
      pending.current = requestAnimationFrame(() => {
        const index = cellFromLatLng(grid, event.latlng.lat, event.latlng.lng);
        const value = index === null || !isValidCell(validity, index) ? null : values[index];
        onHover({ latitude: event.latlng.lat, longitude: event.latlng.lng, index, text: formatApproximateMinutes(value, grid.display_rounding_seconds) });
      });
    },
    click(event) {
      const index = cellFromLatLng(grid, event.latlng.lat, event.latlng.lng);
      if (index !== null && isValidCell(validity, index) && values[index] !== WALKING_SURFACE_SENTINEL) onDestination({ latitude: event.latlng.lat, longitude: event.latlng.lng, index, seconds: values[index] });
    },
  });
  return null;
}

export default function WalkingSurfaceCanvas({ grid, values, validity, onHover, onDestination }) {
  const map = useMap();
  const canvas = useRef(null);
  const geometry = useMemo(() => geometryFor(grid), [grid]);

  useEffect(() => {
    const element = document.createElement("canvas");
    element.className = "walking-surface-canvas";
    map.getPanes().overlayPane.appendChild(element);
    canvas.current = element;
    function draw() {
      const size = map.getSize(); const origin = map.containerPointToLayerPoint([0, 0]);
      const scale = window.devicePixelRatio || 1;
      element.width = size.x * scale; element.height = size.y * scale;
      element.style.width = `${size.x}px`; element.style.height = `${size.y}px`;
      element.style.transform = `translate(${origin.x}px, ${origin.y}px)`;
      const context = element.getContext("2d"); context.scale(scale, scale); context.clearRect(0, 0, size.x, size.y);
      const bounds = map.getBounds(); const zoom = map.getZoom();
      for (let index = 0; index < geometry.length; index += 1) {
        if (!isValidCell(validity, index) || values[index] === WALKING_SURFACE_SENTINEL) continue;
        const [southWest, northEast] = geometry[index];
        if (northEast.latitude < bounds.getSouth() || southWest.latitude > bounds.getNorth() || northEast.longitude < bounds.getWest() || southWest.longitude > bounds.getEast()) continue;
        const topLeft = map.latLngToLayerPoint([northEast.latitude, southWest.longitude]).subtract(origin);
        const bottomRight = map.latLngToLayerPoint([southWest.latitude, northEast.longitude]).subtract(origin);
        context.fillStyle = surfaceColour(values[index]) || "transparent";
        context.globalAlpha = 0.54;
        context.fillRect(topLeft.x, topLeft.y, bottomRight.x - topLeft.x + 1, bottomRight.y - topLeft.y + 1);
        if (zoom >= 16 && index % 9 === 0 && bottomRight.x - topLeft.x > 25) {
          context.globalAlpha = 0.85; context.fillStyle = "#281a33"; context.font = "600 11px system-ui";
          context.fillText(String(Math.round(values[index] / 60)), topLeft.x + 3, topLeft.y + 13);
        }
      }
      context.globalAlpha = 1;
    }
    draw(); map.on("moveend zoomend resize", draw);
    return () => { map.off("moveend zoomend resize", draw); element.remove(); };
  }, [map, geometry, values, validity]);
  return <SurfaceEvents grid={grid} values={values} validity={validity} onHover={onHover} onDestination={onDestination} />;
}
