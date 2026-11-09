// Change-workspace presentation logic against real API payloads (node:test, no extra dependency).
// Fixtures in __fixtures__ were captured from the actual FastAPI app + worker + persistence running on
// the deterministic synthetic worlds of apps/api/tests/change_fixtures.py (no hand-written payloads).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import type { AnalysisDetail, AnalysisSummary, ChangePreflight, Evidence, Layer } from "./api";
import {
  bandText,
  blockingLimitations,
  categoryColours,
  CONFIDENCE_DISCLAIMER,
  changeRequestBody,
  errorKind,
  eventList,
  eventSummary,
  groupLayers,
  layerKey,
  layerLabel,
  parseChangeFeatureCollection,
  parseChangePreflight,
  periodRange,
  PREFLIGHT_DISCLAIMER,
  preflightRows,
  provenanceSummary,
  runState,
  signRows,
  snapshotRequestBody,
  sourceGroups,
} from "./change.ts";
import { formatValue, label } from "./format.ts";

const fx = <T>(name: string): T => JSON.parse(readFileSync(new URL(`./__fixtures__/${name}.json`, import.meta.url), "utf8")) as T;

const detail = fx<AnalysisDetail>("detail_overlap");
const layers = fx<Layer[]>("layers_overlap");
const changes = parseChangeFeatureCollection(fx<unknown>("changes_overlap"));
const preflight = parseChangePreflight(fx<unknown>("preflight"));
const preflightSmall = parseChangePreflight(fx<unknown>("preflight_small_aoi"));

// 1. API type parsing ----------------------------------------------------------------------------------

test("real /changes and preflight payloads parse; malformed ones are rejected", () => {
  assert.equal(changes.type, "FeatureCollection");
  assert.equal(changes.feature_count, changes.features.length);
  assert.equal(preflight.type, "change");
  const broken = structuredClone(fx<{ features: { properties: { index_flags: Record<string, string[]> } }[] }>("changes_overlap"));
  broken.features[0].properties.index_flags.ndvi = [];
  assert.throws(() => parseChangeFeatureCollection(broken), /index_flags\.ndvi/);
  const wrongId = structuredClone(fx<{ features: { id: number }[] }>("changes_overlap"));
  wrongId.features[0].id = 99;
  assert.throws(() => parseChangeFeatureCollection(wrongId), /event_label/);
  assert.throws(() => parseChangePreflight({ type: "snapshot" }), /not a change preflight/);
});

// 2. Request construction -----------------------------------------------------------------------------

test("change requests carry exactly aoi_id, type, period_a and period_b (no parameters)", () => {
  const body = changeRequestBody("aoi-1", { start: "2023-06-01", end: "2023-07-15" }, { start: "2024-06-01", end: "2024-07-15" });
  assert.deepEqual(body, {
    aoi_id: "aoi-1",
    type: "change",
    period_a: { start: "2023-06-01", end: "2023-07-15" },
    period_b: { start: "2024-06-01", end: "2024-07-15" },
  });
  assert.deepEqual(Object.keys(body).sort(), ["aoi_id", "period_a", "period_b", "type"]);
  // Extra keys on the inputs are not forwarded.
  const extra = changeRequestBody("x", { start: "a", end: "b", k: 2 } as never, { start: "c", end: "d" });
  assert.deepEqual(extra.period_a, { start: "a", end: "b" });
});

test("the real API rejects an override field with a structured 422 (the UI never sends one)", () => {
  const e = fx<{ status: number; body: { error: { code: string; details: { unsupported_fields: string[] } } } }>("error_override");
  assert.equal(e.status, 422);
  assert.equal(e.body.error.code, "request_invalid");
  assert.deepEqual(e.body.error.details.unsupported_fields, ["k"]);
  assert.equal(errorKind(e.status), "validation");
});

// 3. Period A / Period B --------------------------------------------------------------------------------

test("Period A and Period B keep their submitted ranges", () => {
  assert.equal(periodRange({ start: String(detail.period_start), end: String(detail.period_end) }), "2023-06-01 → 2023-07-15");
  assert.equal(periodRange({ start: String(detail.period_b_start), end: String(detail.period_b_end) }), "2024-06-01 → 2024-07-15");
  const runs = fx<AnalysisSummary[]>("analyses");
  for (const r of runs) assert.equal(r.period_b_start === null, r.type === "snapshot");
});

// 4. Layer identity -------------------------------------------------------------------------------------

test("layer identity is collision-free across periods where names repeat", () => {
  const names = layers.map((l) => l.name);
  assert.ok(names.filter((n) => n === "true_colour").length === 2, "fixture has one true-colour layer per period");
  assert.ok(new Set(names).size < names.length, "names alone collide");
  const keys = layers.map(layerKey);
  assert.equal(new Set(keys).size, keys.length);
  const a = layers.find((l) => l.name === "true_colour" && l.period === "a")!;
  const b = layers.find((l) => l.name === "true_colour" && l.period === "b")!;
  assert.notEqual(layerKey(a), layerKey(b));
});

test("layers group by API period with Δ first and keep the API legend verbatim", () => {
  const groups = groupLayers(layers);
  // n_valid rasters are byte-identical in A and B in this synthetic world, so the API reports one
  // content-addressed artifact with period null; it is grouped separately, never guessed.
  assert.deepEqual(groups.map((g) => g.period), ["delta", "a", "b", "none"]);
  assert.ok(groups[3].layers.every((l) => l.kind === "n_valid"));
  assert.equal(groups[0].label, "Change (Δ = B − A)");
  const delta = layers.find((l) => l.name === "delta:ndvi")!;
  assert.equal(layerLabel(delta), "Δ NDVI (B − A)");
  assert.deepEqual(delta.legend.range, [-0.5, 0.5]);
  assert.equal(delta.legend.note, "Δ = B − A · positive = index increased");
  assert.ok(!layers.some((l) => l.kind === "z"), "no z layers (C-08)");
  assert.equal(layerLabel(layers.find((l) => l.name === "not_compared")!), "Not compared");
});

// 5. Preflight ------------------------------------------------------------------------------------------

test("preflight rows report per-period counts and disclose, without claiming, coverage or registration", () => {
  const rows = preflightRows(preflight);
  assert.deepEqual(rows.map((r) => [r.label, r.observations, r.sufficient, r.needed]), [
    ["Period A", 6, true, 3],
    ["Period B", 6, true, 3],
  ]);
  assert.match(PREFLIGHT_DISCLAIMER, /metadata-level/);
  assert.match(PREFLIGHT_DISCLAIMER, /Joint coverage and co-registration are evaluated during processing/);
  assert.equal(preflight.coregistration_assessable_by_extent, true);
  assert.equal(preflightSmall.coregistration_assessable_by_extent, false);
  assert.match(preflightSmall.coregistration_note, /coregistration_not_verified/);
  assert.ok(!("joint_coverage" in (preflight as unknown as Record<string, unknown>)));
});

// 6–7. Scientific outcome vs infrastructure failure ----------------------------------------------------

test("a scientific outcome of a succeeded run is never shown as a failure", () => {
  for (const outcome of ["insufficient_observations", "insufficient_coverage", "coregistration_failed"]) {
    const s = runState({ status: "succeeded", outcome });
    assert.equal(s.kind, "scientific_outcome");
    if (s.kind === "scientific_outcome") assert.equal(s.tone, "warn");
  }
  const ok = runState({ status: "succeeded", outcome: "completed" });
  assert.equal(ok.kind === "scientific_outcome" && ok.tone, "ok");
});

test("an infrastructure failure is reported with the job's error code and no outcome", () => {
  const s = runState({ status: "failed", outcome: null }, { error: { code: "asset_read_failed", message: "boom" } });
  assert.equal(s.kind, "infrastructure_failure");
  if (s.kind === "infrastructure_failure") {
    assert.equal(s.code, "asset_read_failed");
    assert.match(s.label, /no scientific result/);
  }
  assert.equal(runState({ status: "running", outcome: null }).kind, "running");
});

test("blocking limitations come from the run's change evidence", () => {
  assert.deepEqual(blockingLimitations(detail), []);
  const withLim = {
    evidence: [
      { evidence_id: "ev_1", kind: "change_summary", index: null, limitations: [{ code: "coregistration_not_verified", blocking: true }, { code: "thresholds_untuned", blocking: false }] },
      { evidence_id: "ev_2", kind: "change_event", index: null, limitations: [{ code: "coregistration_not_verified", blocking: true }] },
    ],
  } as unknown as AnalysisDetail;
  assert.deepEqual(blockingLimitations(withLim), ["coregistration_not_verified"]);
});

// 8. Category layer ------------------------------------------------------------------------------------

test("event colours come from the API's category legend, not a frontend palette", () => {
  const cat = layers.find((l) => l.name === "category")!;
  const classes = cat.legend.classes as { category: string; colour: string }[];
  assert.deepEqual(categoryColours(layers), Object.fromEntries(classes.map((c) => [c.category, c.colour])));
  assert.match(String(cat.legend.note), /not land-cover truth/);
});

// 9. Events ---------------------------------------------------------------------------------------------

test("events keep the API order (event_label 1..N); overlapping events stay separate", () => {
  const evs = eventList(changes);
  assert.deepEqual(evs.map((f) => f.id), evs.map((_, i) => i + 1));
  const cats = evs.map((f) => f.properties.category);
  assert.ok(cats.includes("veg_loss") && cats.includes("veg_gain"));
  assert.equal(evs.length, changes.feature_count);
  const s = eventSummary(evs[0]);
  assert.equal(s.label, 1);
  assert.equal(s.evidenceId, evs[0].properties.evidence_id);
  assert.equal(s.geometryArtifactId, evs[0].properties.geometry_artifact_id);
  assert.equal(s.area, evs[0].properties.measurements.find((m) => m.quantity === "event_area")!); // the API object itself
});

// 10. C-18 -------------------------------------------------------------------------------------------------

test("sign lists keep the API order and counts are the stored measurements (zero is not derived)", () => {
  for (const f of changes.features) {
    for (const r of signRows(f.properties)) {
      assert.deepEqual(r.signs, f.properties.index_flags[r.index]);
      assert.equal(r.positive, f.properties.measurements.find((m) => m.quantity === `event_flag_pixels.${r.index}.positive`));
      assert.equal(r.negative, f.properties.measurements.find((m) => m.quantity === `event_flag_pixels.${r.index}.negative`));
      assert.ok(!("zero" in r));
    }
  }
  const all = changes.features.flatMap((f) => signRows(f.properties));
  assert.ok(all.some((r) => r.index === "mndwi" && r.signs.length === 1 && r.signs[0] === "0"), "all-zero index is ['0']");
  assert.ok(all.some((r) => r.index === "ndvi" && r.signs.join("") === "-+"), "mixed signs preserved in order");
});

// 11. Confidence bands --------------------------------------------------------------------------------

test("confidence band is shown verbatim; anything else is 'unavailable'", () => {
  for (const f of changes.features) assert.equal(bandText(f.properties.confidence_band), f.properties.confidence_band);
  assert.equal(bandText("high"), "unavailable");
  assert.equal(bandText(null), "unavailable");
  assert.match(CONFIDENCE_DISCLAIMER, /not calibrated probabilities/);
  const ev = fx<Evidence>("evidence_event");
  assert.equal(ev.confidence_band, changes.features[0].properties.confidence_band);
  assert.equal(ev.period_b_start, "2024-06-01");
  assert.ok(ev.geometry_artifact_id);
});

// 12. Errors and snapshot regression ------------------------------------------------------------------

test("error kinds follow the API's status codes", () => {
  assert.equal(errorKind(422), "validation");
  assert.equal(errorKind(404), "not_found");
  assert.equal(errorKind(409), "conflict");
  assert.equal(errorKind(500), "server");
  assert.equal(errorKind(0), "network");
  const e = fx<{ status: number; body: { error: { code: string } } }>("error_snapshot_changes");
  assert.deepEqual([e.status, e.body.error.code], [422, "analysis_type_invalid"]);
});

test("snapshot request body is the Phase 1 body, and snapshot layers keep a stable identity", () => {
  assert.deepEqual(snapshotRequestBody("aoi-1", "2024-06-01", "2024-08-15"), {
    aoi_id: "aoi-1",
    type: "snapshot",
    period_a: { start: "2024-06-01", end: "2024-08-15" },
  });
  const snap = { name: "index:ndvi", period: null, render: "index:ndvi", artifact_id: "x" };
  assert.equal(layerKey(snap), "none|index:ndvi|index:ndvi|x");
  assert.equal(runState({ status: "succeeded", outcome: "completed" }).kind, "scientific_outcome");
});

test("per-period quantities are labelled with their period; values are formatted, never recomputed", () => {
  assert.equal(label("observations_usable.b", "count"), "Period B: Usable observations");
  assert.equal(label("masked_fraction.clear.a", "fraction"), "Period A: Pixel-observations: clear");
  assert.equal(label("index_delta.ndvi", "median"), "Δ NDVI (B − A) median");
  assert.equal(label("event_flag_pixels.ndvi.negative", "count"), "NDVI pixels with sign −");
  const m = changes.features[0].properties.measurements.find((x) => x.quantity === "event_pixel_count")!;
  assert.equal(formatValue(m), Math.round(m.value as number).toLocaleString());
});

test("provenance summary groups by the period the API recorded", () => {
  const prov = {
    evidence_id: "ev",
    kind: "change_event",
    processing_run: { id: "p", kind: "analysis", algorithm_versions: {}, code_version: "c", steps: [
      { kind: "source_read", period: "a" }, { kind: "source_read", period: "b" }, { kind: "coregistration" }, { kind: "qa_composite", period: "b" },
    ] },
    scenes: [
      { item_id: "1", period: "b", status: "used", assets: [] },
      { item_id: "2", period: "a", status: "used", assets: [] },
    ],
    artifacts: [],
    step_kinds: [],
  } as unknown as Parameters<typeof provenanceSummary>[0];
  const s = provenanceSummary(prov);
  assert.deepEqual(s.steps.find((r) => r.kind === "source_read"), { kind: "source_read", a: 1, b: 1, none: 0 });
  assert.deepEqual(s.steps.find((r) => r.kind === "coregistration"), { kind: "coregistration", a: 0, b: 0, none: 1 });
  assert.deepEqual(s.scenes.map((g) => [g.label, g.scenes.map((x) => x.item_id)]), [["Period A", ["2"]], ["Period B", ["1"]]]);
});

test("sources are grouped by the period the API recorded, A then B, nothing dropped or invented", () => {
  const groups = sourceGroups(detail.inputs);
  assert.deepEqual(groups.map((g) => [g.period, g.label]), [["a", "Period A"], ["b", "Period B"]]);
  assert.equal(groups.reduce((n, g) => n + g.inputs.length, 0), detail.inputs.length);
  for (const g of groups) assert.ok(g.inputs.every((i) => i.period === g.period));
  // In the captured synthetic world Period A is 2023 and Period B 2024; the grouping follows the field.
  assert.ok(groups[0].inputs.every((i) => String(i.acquired_at).startsWith("2023")));
  assert.ok(groups[1].inputs.every((i) => String(i.acquired_at).startsWith("2024")));
  assert.deepEqual(sourceGroups([]), []);
});

test("the raw phase-correlation error is labelled as a diagnostic, never a quality or confidence score (C-02)", () => {
  const text = label("coreg_pcc_error", "value");
  assert.match(text, /raw diagnostic/);
  assert.match(text, /not a quality or confidence score/);
  assert.doesNotMatch(text, /%|probabilit/i);
});

test("a fixture preflight is typed as ChangePreflight", () => {
  const typed: ChangePreflight = preflight;
  assert.equal(typed.periods.a.days, 45);
});
