"use client";

// MapLibre map with the OpenFreeMap basemap (ADR-021). Raster layers are server tiles addressed by
// artifact ID with fixed rendering. Display reprojection to Web Mercator is display only: nothing
// on this map is measured client-side.
import "maplibre-gl/dist/maplibre-gl.css";
import * as maplibregl from "maplibre-gl";
import type { Map as MlMap } from "maplibre-gl";
import { TerraDraw, TerraDrawPolygonMode, TerraDrawRectangleMode, TerraDrawSelectMode } from "terra-draw";
import { TerraDrawMapLibreGLAdapter } from "terra-draw-maplibre-gl-adapter";
import { useEffect, useRef } from "react";
import type { ChangeFeatureCollection, Layer } from "@/lib/api";

const STYLE = "https://tiles.openfreemap.org/styles/positron";
// The OpenFreeMap style already carries "OpenFreeMap © OpenMapTiles Data from OpenStreetMap";
// only the Copernicus line is added here so the basemap credit is not shown twice.
const attribution = (years: string) => `Contains modified Copernicus Sentinel data${years ? ` ${years}` : ""}`;

// See scripts/copy-maplibre-worker.mjs: the bundled default worker URL does not resolve.
maplibregl.setWorkerUrl("/vendor/maplibre-gl-worker.mjs");

export type DrawMode = "none" | "rectangle" | "polygon";

type Props = {
  aoiGeometry: GeoJSON.Geometry | null;
  rasterLayer: Layer | null;
  opacity: number;
  drawMode: DrawMode;
  onDrawn: (geometry: GeoJSON.Polygon) => void;
  attributionYears: string;
  // Change events (EPSG:4326 copy from /changes). One map feature per event; nothing is merged,
  // dissolved, clipped or simplified. Colours come from the API's category-layer legend.
  events?: ChangeFeatureCollection | null;
  eventColours?: Record<string, string>;
  selectedEvent?: number | null;
  onSelectEvents?: (ids: number[]) => void;
};

const EVENT_LAYERS = ["events-fill", "events-line", "events-selected"] as const;

export default function MapView({
  aoiGeometry,
  rasterLayer,
  opacity,
  drawMode,
  onDrawn,
  attributionYears,
  events = null,
  eventColours = {},
  selectedEvent = null,
  onSelectEvents,
}: Props) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MlMap | null>(null);
  const drawRef = useRef<TerraDraw | null>(null);
  const readyRef = useRef(false);
  const attributionRef = useRef<maplibregl.AttributionControl | null>(null);
  const onDrawnRef = useRef(onDrawn);
  const onSelectRef = useRef(onSelectEvents);
  const pendingRef = useRef<Array<() => void>>([]);
  useEffect(() => {
    onDrawnRef.current = onDrawn;
  }, [onDrawn]);
  useEffect(() => {
    onSelectRef.current = onSelectEvents;
  }, [onSelectEvents]);

  // Run fn once the map style and drawing are ready.
  const whenReady = (fn: () => void) => {
    if (readyRef.current) fn();
    else pendingRef.current.push(fn);
  };

  useEffect(() => {
    if (!container.current) return;
    const map = new maplibregl.Map({
      container: container.current,
      style: STYLE,
      center: [-121.78, 38.53],
      zoom: 11,
      attributionControl: false,
    });
    attributionRef.current = new maplibregl.AttributionControl({ compact: false, customAttribution: attribution("") });
    map.addControl(attributionRef.current);
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    map.on("load", () => {
      const draw = new TerraDraw({
        adapter: new TerraDrawMapLibreGLAdapter({ map }),
        modes: [new TerraDrawRectangleMode(), new TerraDrawPolygonMode(), new TerraDrawSelectMode()],
      });
      draw.start();
      draw.on("finish", (id) => {
        const f = draw.getSnapshotFeature(id);
        if (f && f.geometry.type === "Polygon") onDrawnRef.current(f.geometry as GeoJSON.Polygon);
        draw.clear();
      });
      drawRef.current = draw;
      map.on("click", "events-fill", (e) => {
        // Overlapping events (C-17) are all reported, in feature order; the panel lists them.
        const ids = [...new Set((e.features ?? []).map((f) => Number(f.id)))].sort((x, y) => x - y);
        if (ids.length) onSelectRef.current?.(ids);
      });
      map.on("mouseenter", "events-fill", () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", "events-fill", () => (map.getCanvas().style.cursor = ""));
      readyRef.current = true;
      pendingRef.current.splice(0).forEach((fn) => fn());
    });
    mapRef.current = map;
    return () => {
      drawRef.current?.stop();
      map.remove();
      mapRef.current = null;
      readyRef.current = false;
    };
  }, []);

  // Attribution is always visible (ADR-020 §6); the Copernicus line carries the data years.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !attributionRef.current) return;
    map.removeControl(attributionRef.current);
    attributionRef.current = new maplibregl.AttributionControl({ compact: false, customAttribution: attribution(attributionYears) });
    map.addControl(attributionRef.current);
  }, [attributionYears]);

  useEffect(() => {
    const draw = drawRef.current;
    if (!draw) return;
    draw.setMode(drawMode === "none" ? "select" : drawMode);
  }, [drawMode]);

  // AOI outline
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const apply = () => {
      if (map.getLayer("aoi-line")) map.removeLayer("aoi-line");
      if (map.getSource("aoi")) map.removeSource("aoi");
      if (!aoiGeometry) return;
      map.addSource("aoi", { type: "geojson", data: { type: "Feature", properties: {}, geometry: aoiGeometry } });
      map.addLayer({ id: "aoi-line", type: "line", source: "aoi", paint: { "line-color": "#d6336c", "line-width": 2 } });
      const coords = (aoiGeometry.type === "MultiPolygon" ? aoiGeometry.coordinates.flat(2) : aoiGeometry.type === "Polygon" ? aoiGeometry.coordinates.flat() : []) as number[][];
      if (coords.length) {
        const xs = coords.map((c) => c[0]);
        const ys = coords.map((c) => c[1]);
        map.fitBounds([[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]], { padding: 60, duration: 0 });
      }
    };
    whenReady(apply);
  }, [aoiGeometry]);

  // Raster layer (server tiles by artifact ID)
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const apply = () => {
      if (map.getLayer("raster")) map.removeLayer("raster");
      if (map.getSource("raster")) map.removeSource("raster");
      if (!rasterLayer) return;
      map.addSource("raster", { type: "raster", tiles: [rasterLayer.tile_url], tileSize: 256, maxzoom: 17 });
      map.addLayer({ id: "raster", type: "raster", source: "raster", paint: { "raster-opacity": opacity, "raster-resampling": "nearest" } },
        map.getLayer("aoi-line") ? "aoi-line" : undefined);
    };
    whenReady(apply);
  }, [rasterLayer]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const map = mapRef.current;
    if (map?.getLayer("raster")) map.setPaintProperty("raster", "raster-opacity", opacity);
  }, [opacity]);

  // Change-event vectors above the raster, below the AOI outline.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const apply = () => {
      for (const id of EVENT_LAYERS) if (map.getLayer(id)) map.removeLayer(id);
      if (map.getSource("events")) map.removeSource("events");
      if (!events || events.features.length === 0) return;
      map.addSource("events", { type: "geojson", data: events as unknown as GeoJSON.FeatureCollection });
      const pairs = Object.entries(eventColours).flat();
      const colour = (pairs.length ? ["match", ["get", "category"], ...pairs, "#444444"] : "#444444") as unknown as maplibregl.ExpressionSpecification;
      const before = map.getLayer("aoi-line") ? "aoi-line" : undefined;
      map.addLayer({ id: "events-fill", type: "fill", source: "events", paint: { "fill-color": colour, "fill-opacity": 0.25 } }, before);
      map.addLayer({ id: "events-line", type: "line", source: "events", paint: { "line-color": colour, "line-width": 1.5 } }, before);
      map.addLayer(
        { id: "events-selected", type: "line", source: "events", paint: { "line-color": "#111111", "line-width": 3 }, filter: ["==", ["id"], -1] },
        before,
      );
    };
    whenReady(apply);
  }, [events, eventColours]);

  useEffect(() => {
    const map = mapRef.current;
    if (map?.getLayer("events-selected")) map.setFilter("events-selected", ["==", ["id"], selectedEvent ?? -1]);
  }, [selectedEvent, events]);

  return <div ref={container} className="h-full w-full" role="region" aria-label="Map" />;
}
