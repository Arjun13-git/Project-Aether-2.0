// Rendering of Measurement objects. The UI never computes measurements; it only converts units
// for display (m² → ha / km², fraction → %) and always names the denominator of a percentage.
import type { Measurement } from "./api";

const REASONS: Record<string, string> = {
  insufficient_observations: "fewer than 3 usable observations",
  insufficient_coverage: "less than 50 % of the AOI had enough clear observations",
  insufficient_support: "less than 50 % of the region had a defined value",
  not_applicable: "not applicable",
  coregistration_failed: "co-registration failed",
  robust_reference_unavailable: "robust reference unavailable",
};

const DENOMINATORS: Record<string, string> = {
  aoi_grid_area: "of the AOI (pixel area)",
  aoi_area: "of the AOI's geodesic area",
  aoi_pixel_observations: "of in-AOI pixel-observations",
};

export function denominatorLabel(q: string | null | undefined): string {
  if (!q) return "";
  if (q.startsWith("index_region_pixels.")) return "of the AOI's pixels on this index grid";
  return DENOMINATORS[q] ?? `of ${q}`;
}

export function reasonText(code: string | null | undefined): string {
  return code ? (REASONS[code] ?? code.replaceAll("_", " ")) : "";
}

export function formatValue(m: Measurement): string {
  if (m.status === "unavailable" || m.value === null || m.value === undefined) {
    return `unavailable — ${reasonText(m.unavailable_reason)}`;
  }
  const v = m.value;
  switch (m.unit) {
    case "m2":
      return v >= 1e6 ? `${(v / 1e6).toFixed(3)} km²` : `${(v / 1e4).toFixed(2)} ha`;
    case "count":
      return Math.round(v).toLocaleString();
    case "deg":
      return `${v.toFixed(1)}°`;
    case "1":
      // STAC eo:cloud_cover is stored as a fraction of the whole MGRS tile (not of the AOI).
      if (m.quantity === "tile_cloud_cover") return `${(v * 100).toFixed(1)} % of tile`;
      if (m.statistic === "fraction") return `${(v * 100).toFixed(1)} % ${denominatorLabel(m.denominator_quantity)}`;
      return v.toFixed(3);
    default:
      return `${v} ${m.unit}`;
  }
}

export const LABELS: Record<string, string> = {
  aoi_area: "AOI area (geodesic)",
  aoi_grid_area: "AOI area on the 10 m grid",
  aoi_grid_pixels_10m: "AOI pixels (10 m)",
  aoi_grid_pixels_20m: "AOI pixels (20 m)",
  grid_vs_vector_area_ratio: "Grid / geodesic area (QA)",
  observed_area_a: "Area observed at least once",
  valid_area_a: "Area with ≥ 3 clear observations",
  coverage_a: "Coverage",
  observations_total: "Observations (datatakes)",
  observations_usable: "Usable observations",
  items_considered: "STAC items considered",
  items_used: "STAC items used",
  aoi_pixel_observations: "In-AOI pixel-observations",
};

export function label(q: string, statistic: string): string {
  const [family, qual] = q.split(".");
  if (family === "index") return `${qual.toUpperCase()} ${statistic}`;
  if (family === "index_pixels") return `${qual.toUpperCase()} defined pixels`;
  if (family === "index_region_pixels") return `${qual.toUpperCase()} AOI pixels`;
  if (family === "index_support") return `${qual.toUpperCase()} support`;
  if (family === "masked_fraction") return `Pixel-observations: ${qual.replaceAll("_", " ")}`;
  if (family === "items_excluded") return `Items excluded: ${qual}`;
  if (family === "tile_cloud_cover") return "Tile cloud cover (tile-level, not AOI)";
  if (family === "sun_elevation") return "Sun elevation";
  return LABELS[q] ?? q;
}
