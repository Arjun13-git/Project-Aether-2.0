"use client";

import { useEffect, useState } from "react";
import { api, type Evidence } from "@/lib/api";
import { formatValue, label } from "@/lib/format";

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
          provenance
        </a>
      </header>
      <p className="font-mono text-[11px] text-neutral-500">{ev.evidence_id}</p>
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
      <details className="text-xs text-neutral-600">
        <summary className="cursor-pointer">Facts (non-numeric)</summary>
        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-neutral-50 p-2">
          {JSON.stringify(ev.facts, null, 1)}
        </pre>
      </details>
    </section>
  );
}
