// Typed client for the Aether API. Types are generated from FastAPI's OpenAPI (pnpm gen:api).
import type { components } from "./api-types";

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
    call<AnalysisSubmitted>("POST", "/analyses", { aoi_id: aoiId, type: "snapshot", period_a: { start, end } }),
  analyses: (projectId: string) => call<AnalysisSummary[]>("GET", `/projects/${projectId}/analyses`),
  analysis: (id: string) => call<AnalysisDetail>("GET", `/analyses/${id}`),
  layers: (id: string) => call<Layer[]>("GET", `/analyses/${id}/layers`),
  job: (id: string) => call<Job>("GET", `/jobs/${id}`),
  cancel: (id: string) => call<Job>("POST", `/jobs/${id}/cancel`),
  evidence: (id: string) => call<Evidence>("GET", `/evidence/${id}`),
  provenance: (id: string) => call<Record<string, unknown>>("GET", `/evidence/${id}/provenance`),
};
