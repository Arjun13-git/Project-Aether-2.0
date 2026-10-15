"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useState } from "react";
import EvidencePanel from "./EvidencePanel";
import type { DrawMode } from "./MapView";
import {
  api,
  ApiError,
  type Aoi,
  type AnalysisDetail,
  type AnalysisSummary,
  type CatalogSearchOut,
  type Job,
  type Layer,
  type Project,
} from "@/lib/api";
import { formatValue, reasonText } from "@/lib/format";

const MapView = dynamic(() => import("./MapView"), { ssr: false });

const OUTCOME_TEXT: Record<string, string> = {
  completed: "Completed",
  insufficient_observations: "No result: fewer than 3 usable observations in the period",
  insufficient_coverage: "No result: less than 50 % of the AOI had ≥ 3 clear observations",
};

function errText(e: unknown): string {
  if (e instanceof ApiError) return `${e.code}: ${e.message}`;
  return e instanceof Error ? e.message : String(e);
}

export default function Workspace() {
  const [project, setProject] = useState<Project | null>(null);
  const [aois, setAois] = useState<Aoi[]>([]);
  const [aoi, setAoi] = useState<Aoi | null>(null);
  const [drawMode, setDrawMode] = useState<DrawMode>("none");
  const [pending, setPending] = useState<GeoJSON.Polygon | null>(null);
  const [aoiName, setAoiName] = useState("");
  const [geojsonText, setGeojsonText] = useState("");
  const [start, setStart] = useState("2024-06-01");
  const [end, setEnd] = useState("2024-08-15");
  const [search, setSearch] = useState<CatalogSearchOut | null>(null);
  const [analyses, setAnalyses] = useState<AnalysisSummary[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [detail, setDetail] = useState<AnalysisDetail | null>(null);
  const [layers, setLayers] = useState<Layer[]>([]);
  const [layer, setLayer] = useState<Layer | null>(null);
  const [opacity, setOpacity] = useState(0.85);
  const [index, setIndex] = useState("ndvi");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async (what: string, fn: () => Promise<void>) => {
    setBusy(what);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(null);
    }
  }, []);

  // Project bootstrap: single-user localhost (ADR-012) — use the first project or create one.
  useEffect(() => {
    (async () => {
      const ps = await api.projects();
      const p = ps[0] ?? (await api.createProject("My workspace"));
      setProject(p);
      setAois(await api.aois(p.id));
    })().catch((e) => setError(errText(e)));
  }, []);

  const refreshAnalyses = useCallback(async () => {
    if (project) setAnalyses(await api.analyses(project.id));
  }, [project]);
  useEffect(() => {
    if (project) api.analyses(project.id).then(setAnalyses).catch((e) => setError(errText(e)));
  }, [project]);

  const openAnalysis = useCallback(async (id: string) => {
    const d = await api.analysis(id);
    setDetail(d);
    if (d.status === "succeeded") {
      const ls = await api.layers(id);
      setLayers(ls);
      setLayer(ls.find((l) => l.name === "true_colour") ?? ls[0] ?? null);
    } else {
      setLayers([]);
      setLayer(null);
    }
  }, []);

  // Job polling (SSE is deferred; JOB-SYSTEM.md allows the 5 s polling fallback, we poll at 2 s).
  useEffect(() => {
    if (!job || ["succeeded", "failed", "cancelled"].includes(job.status)) return;
    const t = setTimeout(async () => {
      try {
        const j = await api.job(job.id);
        setJob(j);
        if (["succeeded", "failed", "cancelled"].includes(j.status) && j.analysis_run_id) {
          await openAnalysis(j.analysis_run_id);
          await refreshAnalyses();
        }
      } catch (e) {
        setError(errText(e));
      }
    }, 2000);
    return () => clearTimeout(t);
  }, [job, openAnalysis, refreshAnalyses]);

  const createAoi = (geometry: unknown, source: "drawn" | "geojson") =>
    run("Validating AOI", async () => {
      if (!project) return;
      const a = await api.createAoi(project.id, aoiName.trim() || `AOI ${aois.length + 1}`, geometry, source);
      setAois(await api.aois(project.id));
      setAoi(a);
      setPending(null);
      setSearch(null);
      setDetail(null);
    });

  const aoiAnalyses = useMemo(() => analyses.filter((a) => a.aoi_id === aoi?.id), [analyses, aoi]);
  const coverageEv = detail?.evidence.find((e) => e.kind === "coverage_statistic");
  const indexEv = detail?.evidence.find((e) => e.kind === "index_statistic" && e.index === index);
  const years = detail ? `${String(detail.period_start).slice(0, 4)}` : "";

  return (
    <div className="flex h-screen flex-col text-neutral-900">
      <header className="flex items-center justify-between border-b border-neutral-200 px-4 py-2">
        <div>
          <span className="font-semibold tracking-tight">Aether</span>
          <span className="ml-2 text-sm text-neutral-500">Evidence-first Earth-observation snapshot analysis · Phase 1</span>
        </div>
        <span className="text-xs text-neutral-500">Sentinel-2 L2A (Earth Search, sentinel-2-c1-l2a) · single period · no change detection</span>
      </header>
      {error && (
        <div role="alert" className="border-b border-red-200 bg-red-50 px-4 py-2 text-sm text-red-800">
          {error}
        </div>
      )}
      <div className="flex min-h-0 flex-1">
        {/* Left: workflow */}
        <aside className="w-80 shrink-0 space-y-4 overflow-y-auto border-r border-neutral-200 p-3 text-sm">
          <section className="space-y-2">
            <h2 className="font-semibold">1 · Area of interest</h2>
            <select className="w-full rounded border px-2 py-1" value={aoi?.id ?? ""} aria-label="Select AOI"
              onChange={(e) => { setAoi(aois.find((a) => a.id === e.target.value) ?? null); setSearch(null); setDetail(null); }}>
              <option value="">— select an AOI —</option>
              {aois.map((a) => (<option key={a.id} value={a.id}>{a.name}</option>))}
            </select>
            <input className="w-full rounded border px-2 py-1" placeholder="New AOI name" value={aoiName}
              onChange={(e) => setAoiName(e.target.value)} aria-label="New AOI name" />
            <div className="flex gap-2">
              {(["rectangle", "polygon"] as const).map((m) => (
                <button key={m} className={`rounded border px-2 py-1 ${drawMode === m ? "bg-neutral-900 text-white" : ""}`}
                  onClick={() => setDrawMode(drawMode === m ? "none" : m)}>Draw {m}</button>
              ))}
            </div>
            {pending && (
              <button className="w-full rounded bg-neutral-900 px-2 py-1 text-white" onClick={() => createAoi(pending, "drawn")}>
                Validate &amp; save drawn AOI
              </button>
            )}
            <details>
              <summary className="cursor-pointer text-neutral-600">Paste GeoJSON geometry</summary>
              <textarea className="mt-1 h-20 w-full rounded border p-1 font-mono text-xs" value={geojsonText}
                onChange={(e) => setGeojsonText(e.target.value)} aria-label="GeoJSON geometry" />
              <button className="rounded border px-2 py-1" onClick={() => {
                try { createAoi(JSON.parse(geojsonText), "geojson"); } catch { setError("invalid JSON"); }
              }}>Validate &amp; save</button>
            </details>
            {aoi && (
              <p className="text-xs text-neutral-600">
                EPSG:{aoi.utm_epsg} analysis grid · hash <span className="font-mono">{aoi.geometry_hash.slice(0, 10)}</span>
                {aoi.normalization?.some((o) => o.startsWith("make_valid")) && " · geometry was repaired (recorded)"}
              </p>
            )}
            {aoi?.evidence_id && <EvidencePanel key={aoi.evidence_id} evidenceId={aoi.evidence_id} title="AOI measurement" />}
          </section>

          <section className="space-y-2">
            <h2 className="font-semibold">2 · Period (30–120 days)</h2>
            <div className="flex gap-2">
              <input type="date" className="w-1/2 rounded border px-1 py-1" value={start} onChange={(e) => setStart(e.target.value)} aria-label="Period start" />
              <input type="date" className="w-1/2 rounded border px-1 py-1" value={end} onChange={(e) => setEnd(e.target.value)} aria-label="Period end" />
            </div>
            <button disabled={!aoi || !!busy} className="w-full rounded border px-2 py-1 disabled:opacity-40"
              onClick={() => run("Searching Earth Search", async () => setSearch(await api.search(aoi!.id, start, end)))}>
              3 · Discover Sentinel-2 scenes
            </button>
            {search && (
              <div className="space-y-1">
                <p className="text-xs text-neutral-600">
                  {search.scenes.length} items · {search.observations_eligible} eligible observations (datatakes) · ≥ {search.n_min} needed
                </p>
                <div className="max-h-56 overflow-auto rounded border">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-neutral-50 text-left"><tr><th className="px-1">Date</th><th>Tile cloud*</th><th>Status</th></tr></thead>
                    <tbody>
                      {search.scenes.map((s) => {
                        const cloud = s.metadata_evidence?.measurements.find((m) => m.quantity === "tile_cloud_cover");
                        return (
                          <tr key={s.item_id} className="border-t" title={`${s.item_id} · ${s.platform} · baseline ${s.processing_baseline}`}>
                            <td className="px-1">{s.acquired_at?.slice(0, 10)}</td>
                            <td>{cloud ? formatValue(cloud) : "—"}</td>
                            <td className={s.status === "excluded" ? "text-amber-800" : "text-green-800"}>
                              {s.status}{s.reason_code ? `: ${s.reason_code}` : ""}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <p className="text-[11px] text-neutral-500">* tile-level STAC cloud cover, not AOI cloud cover.</p>
              </div>
            )}
            <button disabled={!aoi || !!busy} className="w-full rounded bg-neutral-900 px-2 py-1 text-white disabled:opacity-40"
              onClick={() => run("Submitting analysis", async () => {
                const s = await api.submit(aoi!.id, start, end);
                if (s.job_id) setJob(await api.job(s.job_id));
                await refreshAnalyses();
                if (s.cached) await openAnalysis(s.analysis_run_id);
              })}>
              4 · Run snapshot analysis
            </button>
            {job && (
              <div className="rounded bg-neutral-50 p-2 text-xs" aria-live="polite">
                Job {job.status} · stage {job.stage ?? "—"} · {(job.progress * 100).toFixed(0)} %
                {job.error && <div className="text-red-700">{String((job.error as Record<string, unknown>).code)}: {String((job.error as Record<string, unknown>).message)}</div>}
                {job.status === "running" && (
                  <button className="ml-2 underline" onClick={() => run("Cancelling", async () => setJob(await api.cancel(job.id)))}>cancel</button>
                )}
              </div>
            )}
          </section>

          <section className="space-y-1">
            <h2 className="font-semibold">Snapshots of this AOI</h2>
            {aoiAnalyses.length === 0 && <p className="text-xs text-neutral-500">none yet</p>}
            {aoiAnalyses.map((a) => (
              <button key={a.id} onClick={() => run("Loading analysis", () => openAnalysis(a.id))}
                className={`block w-full rounded border px-2 py-1 text-left text-xs ${detail?.id === a.id ? "border-neutral-900" : ""}`}>
                {a.period_start} → {a.period_end} · {a.status}{a.outcome ? ` · ${a.outcome}` : ""}
              </button>
            ))}
          </section>
          {busy && <p className="text-xs text-neutral-500" aria-live="polite">{busy}…</p>}
        </aside>

        {/* Centre: map */}
        <main className="relative min-w-0 flex-1">
          <MapView aoiGeometry={(pending ?? aoi?.geometry ?? null) as GeoJSON.Geometry | null} rasterLayer={layer}
            opacity={opacity} drawMode={drawMode} onDrawn={(g) => { setPending(g); setDrawMode("none"); }} attributionYears={years} />
          {layer && (
            <div className="absolute left-2 top-2 max-w-xs rounded bg-white/95 p-2 text-xs shadow">
              <div className="font-semibold">{layer.name.replace("index:", "").toUpperCase()}</div>
              <div>{String(layer.legend.unit ?? layer.legend.meaning ?? "")}</div>
              {Array.isArray(layer.legend.range) && <div>range {String(layer.legend.range[0])} … {String(layer.legend.range[1])} ({String(layer.legend.colormap ?? layer.legend.stretch)})</div>}
              {typeof layer.legend.note === "string" && <div className="text-neutral-500">{layer.legend.note}</div>}
              <div className="text-neutral-500">{layer.grid} · {layer.resolution_m} m · {detail?.period_start} → {detail?.period_end}</div>
            </div>
          )}
        </main>

        {/* Right: results and evidence */}
        <aside className="w-96 shrink-0 space-y-3 overflow-y-auto border-l border-neutral-200 p-3 text-sm">
          {!detail && <p className="text-neutral-500">Run or select a snapshot to see evidence.</p>}
          {detail && (
            <>
              <section className="space-y-1">
                <h2 className="font-semibold">Snapshot {detail.period_start} → {detail.period_end}</h2>
                <p className={`rounded px-2 py-1 ${detail.outcome === "completed" ? "bg-green-50 text-green-900" : "bg-amber-50 text-amber-900"}`}>
                  {detail.status !== "succeeded" ? `Status: ${detail.status}` : OUTCOME_TEXT[detail.outcome ?? ""] ?? detail.outcome}
                </p>
                <p className="text-xs text-neutral-500">Single-period snapshot. This is not a change analysis.</p>
              </section>
              {layers.length > 0 && (
                <section className="space-y-1">
                  <h3 className="font-semibold">Layers</h3>
                  {layers.map((l) => (
                    <label key={`${l.artifact_id}-${l.render}`} className="flex items-center gap-2 text-xs">
                      <input type="radio" name="layer" checked={layer?.name === l.name} onChange={() => setLayer(l)} />
                      {l.name === "true_colour" ? "True colour (display composite)" : l.name.startsWith("insufficient") ? `No reliable data (${l.name.split(":")[1]})` : l.name}
                    </label>
                  ))}
                  <label className="flex items-center gap-2 text-xs">opacity
                    <input type="range" min={0} max={1} step={0.05} value={opacity} onChange={(e) => setOpacity(Number(e.target.value))} />
                  </label>
                </section>
              )}
              {coverageEv && <EvidencePanel key={coverageEv.evidence_id} evidenceId={coverageEv.evidence_id} title="Coverage (denominators named)" />}
              <section className="space-y-1">
                <div className="flex gap-1">
                  {["ndvi", "ndwi", "mndwi", "ndbi"].map((i) => (
                    <button key={i} onClick={() => setIndex(i)} className={`rounded border px-2 py-0.5 text-xs ${index === i ? "bg-neutral-900 text-white" : ""}`}>{i.toUpperCase()}</button>
                  ))}
                </div>
                {indexEv && <EvidencePanel key={indexEv.evidence_id} evidenceId={indexEv.evidence_id} title={`${index.toUpperCase()} statistics over the AOI`} />}
              </section>
              <section className="space-y-1 text-xs">
                <h3 className="font-semibold">Sources</h3>
                <ul className="max-h-40 overflow-auto">
                  {detail.inputs.map((i) => (
                    <li key={i.item_id} title={i.item_id} className={i.status === "excluded" ? "text-amber-800" : ""}>
                      {String(i.acquired_at).slice(0, 10)} · {i.platform} · {i.status}{i.reason_code ? ` (${i.reason_code})` : ""}
                    </li>
                  ))}
                </ul>
                <h3 className="font-semibold">Parameters (untuned defaults)</h3>
                <ul>
                  {detail.calibration.map((c) => (
                    <li key={String(c.name)}>{String(c.name)} = {String(c.value)} · {String(c.calibration)}</li>
                  ))}
                </ul>
                <p className="text-neutral-500">Thresholds are untuned engineering defaults, not calibrated values. Versions: {Object.values(detail.algorithm_versions).join(", ")}</p>
                {detail.outcome && detail.outcome !== "completed" && <p className="text-amber-800">{reasonText(detail.outcome)}</p>}
              </section>
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
