"use client";

import { useEffect, useState } from "react";
import { api, type Evidence } from "@/lib/api";
import { bandText, CONFIDENCE_DISCLAIMER, periodRange } from "@/lib/change";
import { formatValue, label } from "@/lib/format";
import ProvenancePanel from "./ProvenancePanel";

// Shows one Evidence. Every number comes from a Measurement (value, unit, denominator, method).
// Render with key={evidenceId} so a new ID starts from a clean state.
export default function EvidencePanel({ evidenceId, title }: { evidenceId: string; title: string }) {
  const [ev, setEv] = useState<Evidence | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api.evidence(evidenceId).then((e) => alive && setEv(e)).catch((e) => alive && setError(String(e.message ?? e)));
    return () => {
      alive = false;
    };
  }, [evidenceId]);

  if (error) return <p className="text-sm text-red-700">Evidence unavailable: {error}</p>;
  if (!ev) return <p className="text-sm text-neutral-500">Loading evidence…</p>;

  const blocking = ev.limitations.filter((l) => l.blocking);
  const other = ev.limitations.filter((l) => !l.blocking);
  return (
    <section className="space-y-2 rounded border border-neutral-200 p-3" aria-label={title}>
      <header className="flex items-baseline justify-between gap-2">
        <h3 className="font-semibold">{title}</h3>
        <a className="text-xs text-blue-700 underline" href={`/api/v1/evidence/${ev.evidence_id}/provenance`} target="_blank">
          provenance JSON
        </a>
      </header>
      <p className="font-mono text-[11px] text-neutral-500">{ev.evidence_id}</p>
      {(ev.period_start || ev.period_b_start) && (
        <p className="text-xs text-neutral-600">
          {ev.period_start && <span>Period A {periodRange({ start: ev.period_start, end: ev.period_end ?? "" })}</span>}
          {ev.period_start && ev.period_b_start && " · "}
          {ev.period_b_start && <span>Period B {periodRange({ start: ev.period_b_start, end: ev.period_b_end ?? "" })}</span>}
        </p>
      )}
      {ev.confidence_band !== "n/a" && (
        <p className="text-xs">
          Confidence band: <span className="font-semibold">{bandText(ev.confidence_band)}</span>
          <span className="block text-neutral-500">{CONFIDENCE_DISCLAIMER}</span>
        </p>
      )}
      {ev.geometry_artifact_id && (
        <p className="text-xs text-neutral-600">
          Authoritative geometry: change_events.gpkg artifact <span className="font-mono">{ev.geometry_artifact_id}</span> (UTM; feature id = event label)
        </p>
      )}
      {blocking.map((l) => (
        <p key={l.code} role="alert" className="rounded bg-amber-100 px-2 py-1 text-sm text-amber-900">
          ⚠ {l.text}
        </p>
      ))}
      <table className="w-full text-sm">
        <tbody>
          {ev.measurements.map((m) => (
            <tr key={m.measurement_id} className="border-t border-neutral-100 align-top">
              <td className="py-1 pr-2 text-neutral-700">{label(m.quantity, m.statistic)}</td>
              <td className={`py-1 text-right tabular-nums ${m.status === "unavailable" ? "italic text-neutral-500" : ""}`}>
                {formatValue(m)}
              </td>
              <td className="py-1 pl-2 text-right font-mono text-[10px] text-neutral-400" title={m.measurement_id}>
                {m.method}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {other.length > 0 && (
        <ul className="list-disc pl-5 text-xs text-neutral-600">
          {other.map((l) => (
            <li key={l.code}>{l.text}</li>
          ))}
        </ul>
      )}
      <ProvenancePanel evidenceId={ev.evidence_id} />
      <details className="text-xs text-neutral-600">
        <summary className="cursor-pointer">Facts (non-numeric)</summary>
        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-neutral-50 p-2">
          {JSON.stringify(ev.facts, null, 1)}
        </pre>
      </details>
    </section>
  );
}
