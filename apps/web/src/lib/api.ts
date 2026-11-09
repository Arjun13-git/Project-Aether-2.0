// Typed client for the Aether API. Types are generated from FastAPI's OpenAPI (pnpm gen:api).
import type { components } from "./api-types";
import { changeRequestBody, parseChangeFeatureCollection, parseChangePreflight, snapshotRequestBody } from "./change.ts";

type S = components["schemas"];
export type Project = S["Project"];
export type Aoi = S["Aoi"];
export type CatalogSearchOut = S["CatalogSearchOut"];
export type SceneRow = S["SceneRow"];
export type AnalysisSubmitted = S["AnalysisSubmitted"];
export type AnalysisDetail = S["AnalysisDetail"];
export type AnalysisSummary = S["AnalysisSummary"];
export type Evidence = S["Evidence"];
export type Measurement = S["Measurement"];
export type Layer = S["Layer"];
export type Job = S["Job"];
export type EvidenceRef = S["EvidenceRef"];
export type ChangeFeatureCollection = S["ChangeFeatureCollection"];
export type ChangeFeature = S["ChangeFeature"];
export type ChangeEventProperties = S["ChangeEventProperties"];
export type ErrorResponse = S["ErrorResponse"];

export type PeriodIn = { start: string; end: string };

// POST /analyses/preflight (type: change) has no response_model, so it is not in OpenAPI. This
// mirrors aether.api.schemas.ChangePreflightOut field for field (validated server-side).
export type ChangePreflightPeriod = {
  start: string;
  end: string;
  days: number;
  items_considered: number;
  observations_eligible: number;
  sufficient_observations_possible: boolean;
};
export type ChangePreflight = {
  type: "change";
  pair_rules: "passed";
  periods: { a: ChangePreflightPeriod; b: ChangePreflightPeriod };
  grid: Record<string, Record<string, unknown>>;
  n_min: number;
  sufficient_observations_possible: boolean;
  coregistration_assessable_by_extent: boolean;
  coregistration_note: string;
  outcome_order: string[];
  note: string;
  scenes: { a: SceneRow[]; b: SceneRow[] };
};

// GET /evidence/{id}/provenance has no response_model; mirrors services.provenance_view.expand().
export type ProvenanceStep = { kind: string; period?: "a" | "b" | null; [k: string]: unknown };
export type ProvenanceScene = {
  item_id: string;
  collection: string;
  acquired_at: string | null;
  platform: string;
  processing_baseline: string | null;
  stac_sha256: string;
  status: "used" | "excluded";
  reason_code: string | null;
  datatake_id: string | null;
  period?: "a" | "b";
  assets: { asset_key: string; href: string; file_checksum: string | null }[];
};
export type ProvenanceArtifact = {
  id: string;
  sha256: string;
  kind: string;
  crs_epsg: number | null;
  resolution_m: number | null;
  band_names: string[] | null;
  [k: string]: unknown;
};
export type Provenance = {
  evidence_id: string;
  kind: string;
  processing_run: {
    id: string;
    kind: string;
    algorithm_versions: Record<string, string>;
    code_version: string;
    steps: ProvenanceStep[];
    [k: string]: unknown;
  };
  scenes: ProvenanceScene[];
  artifacts: ProvenanceArtifact[];
  step_kinds: string[];
};

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api/v1${path}`, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return undefined as T;
  const json = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = json?.error ?? {};
    throw new ApiError(res.status, e.code ?? "http_error", e.message ?? res.statusText, e.details ?? {});
  }
  return json as T;
}

export const api = {
  projects: () => call<Project[]>("GET", "/projects"),
  createProject: (name: string) => call<Project>("POST", "/projects", { name }),
  aois: (projectId: string) => call<Aoi[]>("GET", `/projects/${projectId}/aois`),
  createAoi: (projectId: string, name: string, geometry: unknown, source: "drawn" | "geojson") =>
    call<Aoi>("POST", `/projects/${projectId}/aois`, { name, geometry, source }),
  search: (aoiId: string, start: string, end: string) =>
    call<CatalogSearchOut>("POST", "/catalog/search", { aoi_id: aoiId, period: { start, end } }),
  submit: (aoiId: string, start: string, end: string) =>
    call<AnalysisSubmitted>("POST", "/analyses", snapshotRequestBody(aoiId, start, end)),
  // Change requests carry exactly these four fields (C-09: no parameters, thresholds or collection).
  changePreflight: (aoiId: string, a: PeriodIn, b: PeriodIn) =>
    call<unknown>("POST", "/analyses/preflight", changeRequestBody(aoiId, a, b)).then(parseChangePreflight),
  submitChange: (aoiId: string, a: PeriodIn, b: PeriodIn) =>
    call<AnalysisSubmitted>("POST", "/analyses", changeRequestBody(aoiId, a, b)),
  changes: (runId: string, opts: { category?: string; bbox?: string } = {}) => {
    const q = new URLSearchParams({ analysis_run_id: runId, ...opts });
    return call<unknown>("GET", `/changes?${q.toString()}`).then(parseChangeFeatureCollection);
  },
  analyses: (projectId: string) => call<AnalysisSummary[]>("GET", `/projects/${projectId}/analyses`),
  analysis: (id: string) => call<AnalysisDetail>("GET", `/analyses/${id}`),
  layers: (id: string) => call<Layer[]>("GET", `/analyses/${id}/layers`),
  job: (id: string) => call<Job>("GET", `/jobs/${id}`),
  cancel: (id: string) => call<Job>("POST", `/jobs/${id}/cancel`),
  evidence: (id: string) => call<Evidence>("GET", `/evidence/${id}`),
  provenance: (id: string) => call<Provenance>("GET", `/evidence/${id}/provenance`),
};
