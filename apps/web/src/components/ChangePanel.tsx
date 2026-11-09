"use client";

import { useState } from "react";
import EvidencePanel from "./EvidencePanel";
import type { AnalysisDetail, ChangeFeature, ChangeFeatureCollection, Job, Layer } from "@/lib/api";
import {
  blockingLimitations,
  CONFIDENCE_DISCLAIMER,
  eventList,
  eventSummary,
  groupLayers,
  layerKey,
  layerLabel,
  LIMITATION_TEXT,
  periodRange,
  runState,
  signRows,
  sourceGroups,
} from "@/lib/change";
import { formatValue, label } from "@/lib/format";

type Props = {
  detail: AnalysisDetail;
  job: Job | null;
  layers: Layer[];
  layer: Layer | null;
  onLayer: (l: Layer | null) => void;
  opacity: number;
  onOpacity: (v: number) => void;
  events: ChangeFeatureCollection | null;
  showEvents: boolean;
  onShowEvents: (v: boolean) => void;
  selected: number[];
  onSelect: (ids: number[]) => void;
};

const TONE = { ok: "bg-green-50 text-green-900", warn: "bg-amber-50 text-amber-900" } as const;

export default function ChangePanel(p: Props) {
  const { detail } = p;
  const [deltaIndex, setDeltaIndex] = useState("ndvi");
  const state = runState(detail, p.job);
  const blocking = blockingLimitations(detail);
  const evidence = (kind: string, pred: (e: AnalysisDetail["evidence"][number]) => boolean = () => true) =>
    detail.evidence.find((e) => e.kind === kind && pred(e));
  const summary = evidence("change_summary");
  const coreg = evidence("coregistration_check");
  const coverage = evidence("coverage_statistic");
  const delta = evidence("index_statistic", (e) => e.period === "delta" && e.index === deltaIndex);
  const events = eventList(p.events);
  const selectedEvents = events.filter((f) => p.selected.includes(Number(f.id)));

  return (
    <>
      <section className="space-y-1">
        <h2 className="font-semibold">Change analysis</h2>
        <dl className="grid grid-cols-[auto_1fr] gap-x-2 text-xs">
          <dt className="font-semibold">Period A</dt>
          <dd>{periodRange({ start: String(detail.period_start), end: String(detail.period_end) })}</dd>
          <dt className="font-semibold">Period B</dt>
          <dd>{periodRange(detail.period_b_start ? { start: String(detail.period_b_start), end: String(detail.period_b_end) } : null)}</dd>
        </dl>
        <p className="text-xs text-neutral-500">Δ = B − A · positive = index increased. Periods are the submitted date ranges.</p>
        {state.kind === "scientific_outcome" && (
          <p className={`rounded px-2 py-1 ${TONE[state.tone]}`} role="status">
            {state.label}
            {state.outcome === "completed" && p.events && ` · ${p.events.feature_count} change events`}
          </p>
        )}
        {(state.kind === "queued" || state.kind === "running" || state.kind === "cancelled") && (
          <p className="rounded bg-neutral-100 px-2 py-1" role="status">{state.label}</p>
        )}
        {state.kind === "infrastructure_failure" && (
          <p className="rounded bg-red-50 px-2 py-1 text-red-900" role="alert">
            {state.label}
            {state.code && <span className="block font-mono text-xs">{state.code}{state.message ? `: ${state.message}` : ""}</span>}
          </p>
        )}
        {blocking.map((code) => (
          <p key={code} role="alert" className="rounded bg-amber-100 px-2 py-1 text-xs text-amber-900">
            ⚠ Blocking limitation <span className="font-mono">{code}</span>: {LIMITATION_TEXT[code] ?? code}
          </p>
        ))}
        {state.kind === "scientific_outcome" && state.outcome === "completed" && p.events?.feature_count === 0 && (
          <p className="text-xs text-neutral-600">No change events above the frozen, untuned thresholds.</p>
        )}
      </section>

      {p.layers.length > 0 && (
        <section className="space-y-1">
          <h3 className="font-semibold">Layers</h3>
          {groupLayers(p.layers).map((g) => (
            <fieldset key={g.period} className="space-y-0.5">
              <legend className="text-xs font-semibold text-neutral-600">{g.label}</legend>
              {g.layers.map((l) => (
                <label key={layerKey(l)} className="flex items-center gap-2 text-xs">
                  <input type="radio" name="change-layer" checked={!!p.layer && layerKey(p.layer) === layerKey(l)} onChange={() => p.onLayer(l)} />
                  {layerLabel(l)}
                </label>
              ))}
            </fieldset>
          ))}
          <label className="flex items-center gap-2 text-xs">
            <input type="radio" name="change-layer" checked={!p.layer} onChange={() => p.onLayer(null)} /> No raster layer
          </label>
          <label className="flex items-center gap-2 text-xs">opacity
            <input type="range" min={0} max={1} step={0.05} value={p.opacity} onChange={(e) => p.onOpacity(Number(e.target.value))} />
          </label>
          {p.events && p.events.feature_count > 0 && (
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={p.showEvents} onChange={(e) => p.onShowEvents(e.target.checked)} /> Change events ({p.events.feature_count})
            </label>
          )}
        </section>
      )}

      {events.length > 0 && (
        <section className="space-y-1">
          <h3 className="font-semibold">Change events</h3>
          <p className="text-[11px] text-neutral-500">
            One entry per event (global label). Events of different categories may overlap; they are never merged.
          </p>
          <ul className="max-h-40 overflow-auto text-xs">
            {events.map((f) => {
              const e = eventSummary(f);
              return (
                <li key={e.id}>
                  <button className={`w-full rounded px-1 text-left ${p.selected.includes(Number(e.id)) ? "bg-neutral-900 text-white" : "hover:bg-neutral-100"}`}
                    onClick={() => p.onSelect([Number(e.id)])}>
                    #{e.label} · {e.category.replaceAll("_", " ")} · band {e.band}{e.area ? ` · ${formatValue(e.area)}` : ""}
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {selectedEvents.map((f) => <EventInspector key={String(f.id)} feature={f} />)}

      {summary && <EvidencePanel key={summary.evidence_id} evidenceId={summary.evidence_id} title="Change summary (fractions of the compared area)" />}
      {coreg && <EvidencePanel key={coreg.evidence_id} evidenceId={coreg.evidence_id} title="Co-registration check (residual gates; error and σ are observational)" />}
      {coverage && <EvidencePanel key={coverage.evidence_id} evidenceId={coverage.evidence_id} title="Coverage, Period A and Period B (denominators named)" />}
      {detail.evidence.some((e) => e.period === "delta") && (
        <section className="space-y-1">
          <div className="flex gap-1">
            {["ndvi", "mndwi", "ndbi"].map((i) => (
              <button key={i} onClick={() => setDeltaIndex(i)} className={`rounded border px-2 py-0.5 text-xs ${deltaIndex === i ? "bg-neutral-900 text-white" : ""}`}>
                Δ {i.toUpperCase()}
              </button>
            ))}
          </div>
          {delta && <EvidencePanel key={delta.evidence_id} evidenceId={delta.evidence_id} title={`Δ ${deltaIndex.toUpperCase()} (B − A) over the compared area`} />}
        </section>
      )}

      <section className="space-y-1 text-xs">
        <h3 className="font-semibold">Parameters (untuned defaults)</h3>
        <ul className="max-h-40 overflow-auto">
          {detail.calibration.map((c) => (
            <li key={String(c.name)}>{String(c.name)} = {String(c.value)} · {String(c.calibration)}</li>
          ))}
        </ul>
        <p className="text-neutral-500">Change thresholds are untuned engineering defaults, not calibrated values.</p>
        <h3 className="font-semibold">Sources</h3>
        {sourceGroups(detail.inputs).map((g) => (
          <section key={g.period} aria-label={`${g.label} sources`}>
            <h4 className="text-neutral-600">{g.label} sources ({g.inputs.length})</h4>
            <ul className="max-h-40 overflow-auto">
              {g.inputs.map((i) => (
                <li key={i.item_id} title={i.item_id} className={i.status === "excluded" ? "text-amber-800" : ""}>
                  {String(i.acquired_at).slice(0, 10)} · {i.platform} · {i.status}{i.reason_code ? ` (${i.reason_code})` : ""}
                </li>
              ))}
            </ul>
          </section>
        ))}
        <p className="text-neutral-500">Versions: {Object.values(detail.algorithm_versions).join(", ")}</p>
      </section>
    </>
  );
}

function EventInspector({ feature }: { feature: ChangeFeature }) {
  const e = eventSummary(feature);
  const p = feature.properties;
  return (
    <section className="space-y-2 rounded border border-neutral-900 p-2 text-xs" aria-label={`Change event ${e.label}`}>
      <h3 className="font-semibold">Event #{e.label} · {e.category.replaceAll("_", " ")}</h3>
      <p className="text-neutral-500">Category is a typical-correlate label of an index change, not a land-cover or object claim.</p>
      <p>
        Confidence band: <span className="font-semibold">{e.band}</span>
        <span className="block text-neutral-500">{CONFIDENCE_DISCLAIMER}</span>
      </p>
      <p>Mode: {p.mode === "robust" ? "robust reference" : "absolute thresholds only"} · {e.touchesBoundary ? "touches the AOI boundary (may be truncated)" : "inside the AOI"}</p>
      <table className="w-full">
        <thead className="text-left text-neutral-500"><tr><th>Index</th><th>Signs present</th><th>+ pixels</th><th>− pixels</th></tr></thead>
        <tbody>
          {signRows(p).map((r) => (
            <tr key={r.index} className="border-t border-neutral-100">
              <td>{r.index.toUpperCase()}</td>
              <td className="font-mono">{r.signs.join(" ")}</td>
              <td className="tabular-nums">{r.positive ? formatValue(r.positive) : "—"}</td>
              <td className="tabular-nums">{r.negative ? formatValue(r.negative) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-neutral-500">Signs are per-pixel significance signs over the event (C-18); zero-sign pixels = event pixels − (+) − (−), not stored.</p>
      <table className="w-full">
        <tbody>
          {p.measurements.filter((m) => !m.quantity.startsWith("event_flag_pixels")).map((m) => (
            <tr key={m.measurement_id} className="border-t border-neutral-100">
              <td className="py-0.5 pr-2">{label(m.quantity, m.statistic)}</td>
              <td className={`text-right tabular-nums ${m.status === "unavailable" ? "italic text-neutral-500" : ""}`}>{formatValue(m)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {p.limitations.length > 0 && (
        <ul className="list-disc pl-4 text-neutral-600">
          {p.limitations.map((l) => <li key={l.code} className={l.blocking ? "text-amber-900" : ""}>{l.text}</li>)}
        </ul>
      )}
      <p className="text-neutral-600">
        Geometry shown: EPSG:4326 copy. Authoritative geometry: change_events.gpkg artifact <span className="font-mono">{e.geometryArtifactId}</span>, feature {e.label}.
      </p>
      <EvidencePanel key={e.evidenceId} evidenceId={e.evidenceId} title={`Evidence for event #${e.label}`} />
    </section>
  );
}
