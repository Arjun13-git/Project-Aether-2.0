// Change-analysis presentation logic (Phase 2). Pure functions over API payloads: they choose
// labels, group and order what the API returned, and classify run state. Nothing here computes a
// measurement, an index, a delta, a threshold, a band or an event property; every number shown
// comes from an API Measurement or field, unchanged (apps/web/README.md, C7).
import type {
  AnalysisDetail,
  ChangeEventProperties,
  ChangeFeature,
  ChangeFeatureCollection,
  ChangePreflight,
  Job,
  Layer,
  Measurement,
  PeriodIn,
  Provenance,
  ProvenanceScene,
} from "./api";

// --- requests --------------------------------------------------------------------------------------

/** The snapshot request body, exactly as in Phase 1. */
export function snapshotRequestBody(aoiId: string, start: string, end: string) {
  return { aoi_id: aoiId, type: "snapshot" as const, period_a: { start, end } };
}

/** The exact change request body: four fields, no parameters (C-09; never relies on extra-field tolerance). */
export function changeRequestBody(aoiId: string, a: PeriodIn, b: PeriodIn) {
  return { aoi_id: aoiId, type: "change" as const, period_a: { start: a.start, end: a.end }, period_b: { start: b.start, end: b.end } };
}

export function periodRange(p: { start: string; end: string } | null | undefined): string {
  return p ? `${p.start} → ${p.end}` : "—";
}

// --- layers ----------------------------------------------------------------------------------------

/** Stable, collision-free layer identity: the same layer name exists once per period in change runs. */
export function layerKey(l: Pick<Layer, "name" | "period" | "render" | "artifact_id">): string {
  return `${l.period ?? "none"}|${l.name}|${l.render}|${l.artifact_id}`;
}

export const PERIOD_LABEL: Record<string, string> = {
  a: "Period A",
  b: "Period B",
  delta: "Change (Δ = B − A)",
  // The API returns no period when it cannot attribute an artifact to one period, e.g. identical
  // bytes produced by both periods (content-addressed artifacts are stored once).
  none: "Other layers (period not distinguished by the API)",
};

export function layerLabel(l: Pick<Layer, "name">): string {
  const n = l.name;
  if (n === "true_colour") return "True colour (display composite)";
  if (n.startsWith("insufficient")) return `No reliable data (${n.split(":")[1]})`;
  if (n.startsWith("index:")) return `${n.slice(6).toUpperCase()} index`;
  if (n.startsWith("n_valid:")) return `Clear observations n_valid (${n.slice(8).toUpperCase()})`;
  if (n.startsWith("delta:")) return `Δ ${n.slice(6).toUpperCase()} (B − A)`;
  if (n === "category") return "Change category";
  if (n === "not_compared") return "Not compared";
  return n;
}

/** Groups layers by the API's period field, in a fixed order; order within a group is the API's. */
export function groupLayers(layers: Layer[]): { period: string; label: string; layers: Layer[] }[] {
  const order = ["delta", "a", "b", "none"];
  const groups = new Map<string, Layer[]>();
  for (const l of layers) {
    const p = l.period ?? "none";
    groups.set(p, [...(groups.get(p) ?? []), l]);
  }
  return order.filter((p) => groups.has(p)).map((p) => ({ period: p, label: PERIOD_LABEL[p], layers: groups.get(p)! }));
}

// --- run state -------------------------------------------------------------------------------------

export type RunState =
  | { kind: "queued" | "running"; label: string }
  | { kind: "infrastructure_failure"; label: string; code: string | null; message: string | null }
  | { kind: "cancelled"; label: string }
  | { kind: "scientific_outcome"; outcome: string; label: string; tone: "ok" | "warn" };

export const CHANGE_OUTCOME_TEXT: Record<string, string> = {
  completed: "Completed: change products available",
  insufficient_observations: "No change result: fewer than 3 usable observations in Period A or Period B",
  insufficient_coverage: "No change result: less than 50 % of the AOI could be compared in both periods",
  coregistration_failed: "No change result: co-registration residual above 0.5 px; change detection was not run",
};

/** Infrastructure state (job/run status) is kept apart from the scientific outcome of a succeeded run. */
export function runState(detail: Pick<AnalysisDetail, "status" | "outcome">, job?: Pick<Job, "error"> | null): RunState {
  if (detail.status === "queued") return { kind: "queued", label: "Queued" };
  if (detail.status === "running") return { kind: "running", label: "Processing" };
  if (detail.status === "cancelled") return { kind: "cancelled", label: "Cancelled" };
  if (detail.status === "failed") {
    const err = (job?.error ?? {}) as Record<string, unknown>;
    return {
      kind: "infrastructure_failure",
      label: "Processing failed (infrastructure or data access); no scientific result was produced",
      code: typeof err.code === "string" ? err.code : null,
      message: typeof err.message === "string" ? err.message : null,
    };
  }
  const outcome = detail.outcome ?? "unknown";
  return {
    kind: "scientific_outcome",
    outcome,
    label: CHANGE_OUTCOME_TEXT[outcome] ?? outcome,
    tone: outcome === "completed" ? "ok" : "warn",
  };
}

/** Blocking limitations recorded on the run's change evidence (deduplicated, API order kept). */
export function blockingLimitations(detail: Pick<AnalysisDetail, "evidence">): string[] {
  const out: string[] = [];
  for (const e of detail.evidence) {
    if (!["change_summary", "change_event", "coverage_statistic"].includes(e.kind)) continue;
    for (const l of e.limitations as { code?: unknown; blocking?: unknown }[]) {
      if (l.blocking === true && typeof l.code === "string" && !out.includes(l.code)) out.push(l.code);
    }
  }
  return out;
}

export const LIMITATION_TEXT: Record<string, string> = {
  coregistration_not_verified: "Co-registration could not be verified; confidence bands are capped at lower.",
  partial_coverage: "Only part of the AOI could be compared in both periods.",
  widespread_change: "Change is widespread; the robust reference was not used (absolute thresholds only).",
  robust_reference_unavailable: "Robust reference statistics unavailable (absolute thresholds only).",
};

// --- preflight -------------------------------------------------------------------------------------

export const PREFLIGHT_DISCLAIMER =
  "Preflight checks metadata-level eligibility. Joint coverage and co-registration are evaluated during processing.";

export function preflightRows(pf: ChangePreflight) {
  return (["a", "b"] as const).map((p) => {
    const v = pf.periods[p];
    return {
      period: p,
      label: PERIOD_LABEL[p],
      range: periodRange(v),
      days: v.days,
      items: v.items_considered,
      observations: v.observations_eligible,
      sufficient: v.sufficient_observations_possible,
      needed: pf.n_min,
    };
  });
}

// --- events ----------------------------------------------------------------------------------------

export const CONFIDENCE_DISCLAIMER = "Confidence bands are rule-based data-quality indicators, not calibrated probabilities.";
export const CHANGE_INDICES = ["ndvi", "mndwi", "ndbi"] as const;

/** The persisted band, verbatim. Unknown or absent values are reported as unavailable, never guessed. */
export function bandText(band: string | null | undefined): string {
  return band === "higher" || band === "moderate" || band === "lower" ? band : "unavailable";
}

export function measurement(p: Pick<ChangeEventProperties, "measurements">, quantity: string, statistic?: string): Measurement | undefined {
  return p.measurements.find((m) => m.quantity === quantity && (statistic === undefined || m.statistic === statistic));
}

/** C-18 per index: the API's sign list in its order, and the stored positive/negative counts. */
export function signRows(p: Pick<ChangeEventProperties, "index_flags" | "measurements">) {
  return CHANGE_INDICES.map((i) => ({
    index: i,
    signs: [...(p.index_flags[i] ?? [])],
    positive: measurement(p, `event_flag_pixels.${i}.positive`, "count") ?? null,
    negative: measurement(p, `event_flag_pixels.${i}.negative`, "count") ?? null,
  }));
}

export function eventSummary(f: ChangeFeature) {
  const p = f.properties;
  return {
    id: f.id,
    label: p.event_label,
    category: p.category,
    band: bandText(p.confidence_band),
    area: measurement(p, "event_area", "value") ?? null,
    pixels: measurement(p, "event_pixel_count", "count") ?? null,
    touchesBoundary: p.touches_aoi_boundary,
    evidenceId: p.evidence_id,
    geometryArtifactId: p.geometry_artifact_id,
  };
}

/** Events in the API's order (global event_label 1..N). Overlapping events stay separate entries. */
export function eventList(fc: ChangeFeatureCollection | null): ChangeFeature[] {
  return fc ? [...fc.features] : [];
}

/** Map style colours for categories, taken from the API's category-layer legend (no palette here). */
export function categoryColours(layers: Layer[]): Record<string, string> {
  const cat = layers.find((l) => l.name === "category");
  const classes = (cat?.legend as { classes?: { category?: unknown; colour?: unknown }[] } | undefined)?.classes ?? [];
  const out: Record<string, string> = {};
  for (const c of classes) if (typeof c.category === "string" && typeof c.colour === "string") out[c.category] = c.colour;
  return out;
}

// --- errors ----------------------------------------------------------------------------------------

export type ErrorKind = "validation" | "not_found" | "conflict" | "server" | "network";

export function errorKind(status: number | null | undefined): ErrorKind {
  if (status === undefined || status === null || status === 0) return "network";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status >= 400 && status < 500) return "validation";
  return "server";
}

export const ERROR_PREFIX: Record<ErrorKind, string> = {
  validation: "Request rejected",
  not_found: "Not found",
  conflict: "Not available yet",
  server: "Server error",
  network: "Network error",
};

// --- provenance ------------------------------------------------------------------------------------

/** Step counts per kind and per recorded period, and scenes grouped by the period the API recorded. */
export function provenanceSummary(p: Provenance) {
  const steps = new Map<string, { kind: string; a: number; b: number; none: number }>();
  for (const st of p.processing_run.steps) {
    const row = steps.get(st.kind) ?? { kind: st.kind, a: 0, b: 0, none: 0 };
    if (st.period === "a") row.a += 1;
    else if (st.period === "b") row.b += 1;
    else row.none += 1;
    steps.set(st.kind, row);
  }
  const groups = new Map<string, ProvenanceScene[]>();
  for (const sc of p.scenes) {
    const key = sc.period ?? "none";
    groups.set(key, [...(groups.get(key) ?? []), sc]);
  }
  const scenes = ["a", "b", "none"]
    .filter((k) => groups.has(k))
    .map((k) => ({ period: k, label: k === "none" ? "Source" : PERIOD_LABEL[k], scenes: groups.get(k)! }));
  return { steps: [...steps.values()], scenes };
}

/** Run inputs grouped by the period the API recorded for each (`inputs[].period`), A then B; API order within. */
export function sourceGroups(inputs: AnalysisDetail["inputs"]) {
  return (["a", "b"] as const)
    .map((p) => ({ period: p, label: PERIOD_LABEL[p], inputs: inputs.filter((i) => i.period === p) }))
    .filter((g) => g.inputs.length > 0);
}

// --- runtime parsing of the payloads the change UI depends on ------------------------------------------

function fail(what: string): never {
  throw new Error(`unexpected API payload: ${what}`);
}

/** Checks the /changes shape the UI relies on; throws instead of rendering a malformed collection. */
export function parseChangeFeatureCollection(json: unknown): ChangeFeatureCollection {
  const fc = json as ChangeFeatureCollection;
  if (!fc || fc.type !== "FeatureCollection" || !Array.isArray(fc.features)) fail("not a FeatureCollection");
  if (fc.feature_count !== fc.features.length) fail("feature_count does not match features");
  fc.features.forEach((f, i) => {
    const p = f?.properties;
    if (f?.type !== "Feature" || typeof f.id !== "number" || !f.geometry) fail(`feature ${i}`);
    if (p.event_label !== f.id) fail(`feature ${i}: id differs from event_label`);
    if (typeof p.evidence_id !== "string" || typeof p.geometry_artifact_id !== "string") fail(`feature ${i}: identifiers`);
    if (!["higher", "moderate", "lower"].includes(p.confidence_band)) fail(`feature ${i}: confidence_band`);
    for (const ix of CHANGE_INDICES) {
      const signs = p.index_flags?.[ix];
      if (!Array.isArray(signs) || signs.length === 0 || signs.some((s) => !["-", "0", "+"].includes(s))) fail(`feature ${i}: index_flags.${ix}`);
    }
    if (!Array.isArray(p.measurements)) fail(`feature ${i}: measurements`);
  });
  return fc;
}

/** Checks the change-preflight shape (the route is not described by OpenAPI; see api.ts). */
export function parseChangePreflight(json: unknown): ChangePreflight {
  const pf = json as ChangePreflight;
  if (!pf || pf.type !== "change" || pf.pair_rules !== "passed") fail("not a change preflight");
  for (const k of ["a", "b"] as const) {
    const v = pf.periods?.[k];
    if (!v || typeof v.observations_eligible !== "number" || typeof v.sufficient_observations_possible !== "boolean") fail(`periods.${k}`);
  }
  if (typeof pf.coregistration_assessable_by_extent !== "boolean" || typeof pf.coregistration_note !== "string") fail("co-registration disclosure");
  return pf;
}
