"use client";

import { useState } from "react";
import { api, type Provenance } from "@/lib/api";
import { provenanceSummary } from "@/lib/change";

// Expands GET /evidence/{id}/provenance on demand: processing run and versions, steps by period,
// source scenes by the period the API recorded, and the artifacts the evidence cites.
export default function ProvenancePanel({ evidenceId }: { evidenceId: string }) {
  const [prov, setProv] = useState<Provenance | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    if (prov || error) return;
    api.provenance(evidenceId).then(setProv).catch((e) => setError(String(e.message ?? e)));
  };

  const s = prov ? provenanceSummary(prov) : null;
  return (
    <details className="text-xs text-neutral-700" onToggle={(e) => (e.currentTarget.open ? load() : undefined)}>
      <summary className="cursor-pointer">Provenance</summary>
      {error && <p className="text-red-700">Provenance unavailable: {error}</p>}
      {!prov && !error && <p className="text-neutral-500">Loading provenance…</p>}
      {prov && s && (
        <div className="mt-1 space-y-2">
          <p>
            Processing run <span className="font-mono">{prov.processing_run.id.slice(0, 8)}</span> · code{" "}
            <span className="font-mono">{prov.processing_run.code_version.slice(0, 12)}</span>
          </p>
          <p className="text-neutral-600">Versions: {Object.entries(prov.processing_run.algorithm_versions).map(([k, v]) => `${k}=${v}`).join(", ")}</p>
          <table className="w-full">
            <thead className="text-left text-neutral-500"><tr><th>Step</th><th>Period A</th><th>Period B</th><th>Run-level</th></tr></thead>
            <tbody>
              {s.steps.map((r) => (
                <tr key={r.kind} className="border-t border-neutral-100"><td>{r.kind}</td><td>{r.a || ""}</td><td>{r.b || ""}</td><td>{r.none || ""}</td></tr>
              ))}
            </tbody>
          </table>
          {s.scenes.map((g) => (
            <div key={g.period}>
              <p className="font-semibold">{g.label} sources ({g.scenes.length})</p>
              <ul className="max-h-28 overflow-auto">
                {g.scenes.map((sc) => (
                  <li key={sc.item_id} title={sc.stac_sha256} className={sc.status === "excluded" ? "text-amber-800" : ""}>
                    {String(sc.acquired_at).slice(0, 10)} · {sc.item_id} · {sc.status}{sc.reason_code ? ` (${sc.reason_code})` : ""} · {sc.assets.length} assets
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {prov.artifacts.length > 0 && (
            <div>
              <p className="font-semibold">Artifacts cited by this evidence</p>
              <ul>
                {prov.artifacts.map((a) => (
                  <li key={a.id}>
                    {a.kind} · {(a.band_names ?? []).join(", ") || "vector"} · <span className="font-mono">{a.sha256.slice(0, 12)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <a className="text-blue-700 underline" href={`/api/v1/evidence/${evidenceId}/provenance`} target="_blank">raw provenance JSON</a>
        </div>
      )}
    </details>
  );
}
