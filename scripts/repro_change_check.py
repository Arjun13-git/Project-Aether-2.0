"""Reproducibility check (invariant I-12) for a persisted change run (Phase 2, C-07/C-10).

Usage (from the repo root, API database configured in apps/api/.env):
  uv run --project apps/api python scripts/repro_change_check.py [ANALYSIS_RUN_ID ...]   # default: all completed change runs

Snapshot runs are checked by scripts/repro_check.py; a snapshot run named here is reported as FAIL (not checked).

The run is re-executed from its *recorded* inputs, with no new catalogue search:
- the recorded catalogue response of each period (request, item order, verbatim STAC JSON);
- the same AOI, periods, grid, frozen parameters and change algorithm-version map.
The recorded identity (parameters, versions, change key, grid) must equal what the current code derives,
otherwise the run is not re-executed. Window reads come from the window cache (or the network if
evicted). Fresh artifacts go to a throw-away store and the database is read in a READ ONLY transaction.

Compared (exactly): processing steps and parameter hash; inputs per period; outcome; every artifact
(kind, bands, sha256, GeoPackage included) and the on-disk bytes of the persisted ones; every evidence
record (facts, confidence band, limitations, periods, geometry, cited artifacts, measurements) and the
change_events rows. Not compared, because they are random or environment-specific by design: evidence,
measurement, processing-run and artifact ids (and content_sha256, which hashes those ids), artifact
URIs, created_at/completed_at, timings, code_version and environment (reported for information), and the
`from_cache` flag of source_read steps (whether a window came from the window cache or the network; the
window bytes themselves are covered by every downstream artifact hash).
Exit code 1 on any difference.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry import shape
from sqlalchemy import text

os.chdir(Path(__file__).resolve().parents[1] / "apps" / "api")  # so apps/api/.env is used

from aether import science  # noqa: E402
from aether.db import repo  # noqa: E402
from aether.db.engine import get_engine  # noqa: E402
from aether.domain.periods import Period  # noqa: E402
from aether.evidence.builders import canonical_hash, unit_of  # noqa: E402
from aether.geo.io.windows import AssetReader, CachedAssetReader, RasterioAssetReader  # noqa: E402
from aether.geo.stac.client import SearchResult  # noqa: E402
from aether.services.change_persistence import _write_artifacts, build_evidence  # noqa: E402
from aether.services.change_run import ChangeRunRequest, change_idempotency_key, execute_change  # noqa: E402
from aether.services.snapshot_run import _aoi_area  # noqa: E402
from aether.settings import get_settings  # noqa: E402
from aether.storage.store import LocalFilesystemStore  # noqa: E402

CHANGE_RUNS_SQL = "SELECT id FROM analysis_runs WHERE type='change' AND outcome='completed' ORDER BY created_at"


def change_run_ids(conn) -> list:
    """Default selection: completed change runs only (snapshot runs belong to repro_check.py)."""
    return [r.id for r in conn.execute(text(CHANGE_RUNS_SQL))]


class RecordedCatalogue:
    """Replays the catalogue response recorded for each period; never contacts a STAC API."""

    def __init__(self, searches: dict[date, dict[str, Any]], items: dict[str, dict[str, Any]]) -> None:
        self._searches = searches
        self._items = items
        self.api_url = next(iter(searches.values()))["api_url"]

    def search(self, geometry: dict[str, Any], start: date, end: date, max_tile_cloud: float) -> SearchResult:
        step = self._searches[start]
        items = [self._items[i] for i in step["item_ids"]]
        return SearchResult(step["api_url"], step["collection"], step["request"], items, None)


def _period(rng: Any) -> Period:  # daterange [start, end + 1 day)
    return Period(rng.lower, rng.upper - timedelta(days=1))


def _sha256_file(uri: str) -> str:
    h = hashlib.sha256()
    with open(uri.removeprefix("file://"), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _step_view(step: dict[str, Any]) -> dict[str, Any]:
    """A processing step without ``from_cache`` (an execution fact: cache hit vs network read)."""
    return {k: v for k, v in step.items() if not (step.get("kind") == "source_read" and k == "from_cache")}


def _evidence_key(kind: str, facts: dict[str, Any]) -> tuple:
    return (kind, facts.get("period"), facts.get("index"), facts.get("event_label"))


def _measurement_view(quantity, statistic, value, status, reason, unit, region, n_samples, method, denominator) -> tuple:
    return (
        quantity,
        statistic,
        None if value is None else float(value),
        status,
        reason,
        unit,
        region,
        n_samples,
        method,
        denominator,
    )


def check(conn, run_id, *, reader: AssetReader | None = None, aoi_area: Callable[[dict], float] | None = None) -> bool:
    """Re-execute one change run and compare it with what was persisted. Prints one line per comparison."""
    conn.execute(text("SET TRANSACTION READ ONLY"))
    run = (
        conn.execute(
            text(
                """SELECT id, aoi_id, type, status, outcome, period_a, period_b, collection, parameters, parameters_hash,
                      algorithm_versions, grid_hash, idempotency_key FROM analysis_runs WHERE id=:r"""
            ),
            {"r": run_id},
        )
        .mappings()
        .one()
    )
    if run["type"] != "change":
        print(f"{run_id}  {run['type']} run: not a change run; use scripts/repro_check.py  FAIL")
        return False
    if run["status"] != "succeeded":
        print(f"{run_id}  change run with status {run['status']}: nothing persisted to reproduce  FAIL")
        return False

    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    aoi = repo.get_aoi(conn, run["aoi_id"])
    pa, pb = _period(run["period_a"]), _period(run["period_b"])

    # 1. Identity: what was recorded must be what the current code derives (C-10). Otherwise stop.
    record("collection", run["collection"] == science.STAC_COLLECTION, run["collection"])
    record("parameters (frozen defaults)", run["parameters"] == science.change_parameters())
    record("algorithm versions", run["algorithm_versions"] == science.CHANGE_ALGORITHM_VERSIONS)
    record("change idempotency key", change_idempotency_key(aoi, pa, pb) == run["idempotency_key"])
    if not all(ok for _, ok, _ in results):
        return _report(run_id, aoi, results, executed=False)

    pr = conn.execute(
        text("SELECT parameters_hash, steps, code_version FROM processing_runs WHERE analysis_run_id=:r"), {"r": run_id}
    ).one()
    searches = {date.fromisoformat(s["request"]["datetime"][:10]): s for s in pr.steps if s["kind"] == "catalog_search"}
    items = {
        r.item_id: r.stac_json
        for r in conn.execute(
            text(
                """SELECT s.item_id, s.stac_json FROM analysis_run_inputs i JOIN scenes s ON s.id=i.scene_id
                   WHERE i.analysis_run_id=:r"""
            ),
            {"r": run_id},
        )
    }

    # 2. Re-execute from the recorded catalogue response into a throw-away store.
    if reader is None:
        reader = CachedAssetReader(RasterioAssetReader(), Path(get_settings().storage_root).resolve())
    result = execute_change(ChangeRunRequest(aoi, pa, pb), searcher=RecordedCatalogue(searches, items), reader=reader)
    area = (aoi_area or (lambda a: _aoi_area(conn.engine, a)))(aoi)
    with tempfile.TemporaryDirectory() as tmp:
        fresh_art, _vector = _write_artifacts(result, LocalFilesystemStore(Path(tmp)))
    drafts = build_evidence(result, aoi, area, fresh_art)

    record("grid hash", result.analysis.grid.grid_hash == run["grid_hash"])
    record("outcome", result.outcome == run["outcome"], f"{run['outcome']} → {result.outcome}")
    record("parameter hash", canonical_hash(result.parameters) == pr.parameters_hash)
    display = [
        {"kind": "display_composite", "purpose": "display_rgb", "bands": ["red", "green", "blue"], "statistic": "median",
         "artifact_sha256": fresh_art[f"rgb.{p}"]["sha256"], "period": p}
        for p in ("a", "b") if f"rgb.{p}" in fresh_art
    ]  # fmt: skip
    fresh_steps = [*result.steps, *display]
    record(
        "processing steps",
        [_step_view(x) for x in fresh_steps] == [_step_view(x) for x in pr.steps],
        f"{len(pr.steps)} recorded; window cache hits {sum(bool(x.get('from_cache')) for x in fresh_steps)}",
    )

    stored_inputs = {
        tuple(r)
        for r in conn.execute(
            text(
                """SELECT i.period, s.item_id, i.datatake_id, i.status, i.reason_code
                   FROM analysis_run_inputs i JOIN scenes s ON s.id=i.scene_id WHERE i.analysis_run_id=:r"""
            ),
            {"r": run_id},
        )
    }
    fresh_inputs = {
        (p, s.item_id, s.datatake_id, s.status, s.reason_code)
        for p, summary in (("a", result.analysis.period_a), ("b", result.analysis.period_b))
        for s in summary.scenes
    }
    record("inputs per period", stored_inputs == fresh_inputs, f"{len(stored_inputs)} recorded")

    # 3. Artifacts: identical (kind, bands, sha256) and intact persisted bytes.
    stored = repo.run_artifacts(conn, run_id)
    uris = {
        r.id: r.uri
        for r in conn.execute(text("SELECT id, uri FROM artifacts WHERE id = ANY(:ids)"), {"ids": [a["id"] for a in stored]})
    }
    sha_by_id = {a["id"]: a["sha256"] for a in stored}
    stored_set = {(a["kind"], tuple(a["band_names"] or ()), a["sha256"]) for a in stored}
    fresh_set = {(m["kind"], tuple(m["raster"].get("band_names") or ()), m["sha256"]) for m in fresh_art.values()}
    record(
        "artifacts (kind, bands, sha256)", stored_set == fresh_set, f"{len(stored_set & fresh_set)}/{len(stored_set)} identical"
    )
    gpkg = [m["sha256"] for m in fresh_art.values() if m["kind"] == "vector_gpkg"]
    if gpkg:
        record("GeoPackage bytes", any(k == "vector_gpkg" and s == gpkg[0] for k, _, s in stored_set), gpkg[0][:12])
    bad_bytes = [i for i, u in uris.items() if _sha256_file(u) != sha_by_id[i]]
    record("persisted artifact bytes intact", not bad_bytes, f"{len(uris) - len(bad_bytes)}/{len(uris)}")

    # 4. Evidence and measurements, keyed by kind + period + index + event label (ids are random by design).
    stored_ev = {}
    for e in conn.execute(
        text(
            """SELECT evidence_id, kind, facts, confidence_band, limitations, artifact_ids, geometry_artifact_id,
                  ST_AsBinary(geom) AS wkb, lower(observed_period_a) AS a0, upper(observed_period_a) - 1 AS a1,
                  lower(observed_period_b) AS b0, upper(observed_period_b) - 1 AS b1
               FROM evidence WHERE analysis_run_id=:r"""
        ),
        {"r": run_id},
    ).mappings():
        ms = conn.execute(
            text(
                """SELECT quantity, statistic, value, status, unavailable_reason, unit, region, n_samples, method,
                      denominator_quantity FROM measurements WHERE evidence_id=:e"""
            ),
            {"e": e["evidence_id"]},
        )
        stored_ev[_evidence_key(e["kind"], e["facts"])] = {
            "facts": e["facts"],
            "band": e["confidence_band"],
            "limitations": [x["code"] for x in e["limitations"]],
            "periods": tuple(str(x) if x is not None else None for x in (e["a0"], e["a1"], e["b0"], e["b1"])),
            "geometry": shapely.from_wkb(bytes(e["wkb"])) if e["wkb"] is not None else None,
            "artifacts": sorted(sha_by_id[i] for i in e["artifact_ids"]),
            "geometry_artifact": sha_by_id.get(e["geometry_artifact_id"]),
            "measurements": sorted(_measurement_view(*m) for m in ms),
        }
    fresh_ev = {}
    for d in drafts:
        oa, ob = d.observed_period_a or (None, None), d.observed_period_b or (None, None)
        fresh_ev[_evidence_key(d.kind, d.facts)] = {
            "facts": d.facts,
            "band": d.confidence_band,
            "limitations": list(d.limitations),
            "periods": (*oa, *ob),
            "geometry": shape(d.geometry) if d.geometry else None,
            "artifacts": sorted(fresh_art[k]["sha256"] for k in d.artifact_keys),
            "geometry_artifact": fresh_art[d.geometry_artifact_key]["sha256"] if d.geometry_artifact_key else None,
            "measurements": sorted(
                _measurement_view(
                    m.quantity, m.statistic, m.value, m.status, m.unavailable_reason, unit_of(m.quantity), m.region,
                    m.n_samples, m.method, m.denominator,
                )
                for m in d.measurements
            ),
        }  # fmt: skip
    n_stored_ev = conn.execute(text("SELECT count(*) FROM evidence WHERE analysis_run_id=:r"), {"r": run_id}).scalar_one()
    record(
        "evidence records",  # equal keys, and every key unique on both sides
        fresh_ev.keys() == stored_ev.keys() and n_stored_ev == len(stored_ev) and len(drafts) == len(fresh_ev),
        f"{n_stored_ev} recorded, {len(drafts)} reproduced",
    )
    fields = ("facts", "band", "limitations", "periods", "artifacts", "geometry_artifact", "measurements")
    diffs: dict[str, int] = {}
    for key in stored_ev.keys() & fresh_ev.keys():
        s, f = stored_ev[key], fresh_ev[key]
        for name in fields:
            if s[name] != f[name]:
                diffs[name] = diffs.get(name, 0) + 1
        sg, fg = s["geometry"], f["geometry"]
        if (sg is None) != (fg is None) or (sg is not None and not (sg.geom_type == fg.geom_type and sg.equals_exact(fg, 0.0))):
            diffs["geometry"] = diffs.get("geometry", 0) + 1
    n_meas = sum(len(v["measurements"]) for v in stored_ev.values())
    record("evidence content (facts, bands, limitations, periods, geometry, artifacts)", not diffs, str(diffs or ""))
    record("measurements", "measurements" not in diffs, f"{n_meas} recorded")

    # 5. change_events rows (C-17 identity: global event_label, category, boundary flag).
    stored_rows = {
        (r.label, r.category, r.touches_aoi_boundary)
        for r in conn.execute(
            text(
                """SELECT (e.facts->>'event_label')::int AS label, c.category, c.touches_aoi_boundary
                   FROM change_events c JOIN evidence e ON e.evidence_id = c.evidence_id WHERE c.analysis_run_id=:r"""
            ),
            {"r": run_id},
        )
    }
    fresh_rows = {
        (d.facts["event_label"], d.facts["category"], d.facts["touches_aoi_boundary"]) for d in drafts if d.kind == "change_event"
    }
    record("change events (label, category, boundary)", stored_rows == fresh_rows, f"{len(stored_rows)} recorded")
    record("code version (information only)", True, str(pr.code_version)[:12])
    return _report(run_id, aoi, results, executed=True)


def _report(run_id, aoi: dict[str, Any], results: list[tuple[str, bool, str]], *, executed: bool) -> bool:
    ok = all(r for _, r, _ in results)
    print(f"{run_id}  {aoi['name'][:34]}")
    for name, r, detail in results:
        print(f"    {'ok  ' if r else 'DIFF'}  {name}{f'  ({detail})' if detail else ''}")
    if not executed:
        print("    recorded identity differs from the current code: not re-executed")
    print(f"    {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> None:
    with get_engine().connect() as conn:
        ids = sys.argv[1:] or change_run_ids(conn)
        results = []
        for i in ids:
            results.append(check(conn, i))
            conn.rollback()  # end each read-only transaction
    print(f"change reproducibility: {sum(results)}/{len(results)} runs identical")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
