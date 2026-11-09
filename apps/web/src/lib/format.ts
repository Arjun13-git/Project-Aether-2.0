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
  "aoi_pixel_observations.a": "of Period A in-AOI pixel-observations",
  "aoi_pixel_observations.b": "of Period B in-AOI pixel-observations",
  aoi_grid_pixels_10m: "of the AOI's 10 m pixels",
  grid_pixels_10m: "of the 10 m analysis grid",
  joint_valid_area: "of the compared area",
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
    case "px":
      return `${v.toFixed(2)} px`;
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
  observed_area_b: "Period B: area observed at least once",
  valid_area_b: "Period B: area with ≥ 3 clear observations",
  coverage_b: "Period B coverage",
  joint_valid_area: "Compared area (valid in both periods)",
  joint_coverage: "Joint coverage",
  grid_pixels_10m: "Analysis grid pixels (10 m)",
  coreg_residual: "Co-registration residual",
  coreg_shift_x: "Co-registration shift x (columns)",
  coreg_shift_y: "Co-registration shift y (rows)",
  coreg_pcc_error: "Phase-correlation error (raw diagnostic; not a quality or confidence score)",
  coreg_window_width: "Registration window width",
  coreg_window_height: "Registration window height",
  coreg_grid_joint_valid_fraction: "Registration support (grid)",
  event_area: "Event area",
  event_pixel_count: "Event pixels",
  event_strength: "Event strength (median max |z|)",
  event_support_a: "Period A support (median n_valid NDVI)",
  event_support_b: "Period B support (median n_valid NDVI)",
  eligible_pixels: "Compared pixels",
  change_event_count: "Change events",
  changed_area: "Changed area (union of events)",
  changed_fraction: "Changed fraction",
};

const PERIOD_PREFIX: Record<string, string> = { a: "Period A", b: "Period B" };

export function label(q: string, statistic: string): string {
  // Per-period change quantities carry a trailing ".a" / ".b" (registry per-period variants).
  const period = /\.(a|b)$/.exec(q)?.[1];
  if (period && !(q in LABELS)) return `${PERIOD_PREFIX[period]}: ${label(q.slice(0, -2), statistic)}`;
  const [family, qual, sub] = q.split(".");
  if (family === "index_delta") return `Δ ${qual.toUpperCase()} (B − A) ${statistic}`;
  if (family === "index_delta_pixels") return `Δ ${qual.toUpperCase()} compared pixels`;
  if (family === "index_delta_support") return `Δ ${qual.toUpperCase()} support`;
  if (family === "event_median_delta") return `Median Δ ${qual.toUpperCase()} (B − A)`;
  if (family === "event_flag_pixels") return `${qual.toUpperCase()} pixels with sign ${sub === "positive" ? "+" : "−"}`;
  if (family === "coreg_qa_nir_std") return "NIR QA σ in window (observational)";
  if (family === "change_event_count" && qual) return `Change events: ${qual.replaceAll("_", " ")}`;
  if (family === "changed_area" && qual) return `Changed area: ${qual.replaceAll("_", " ")}`;
  if (family === "changed_fraction" && qual) return `Changed fraction: ${qual.replaceAll("_", " ")}`;
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
