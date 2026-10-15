"""Change-run persistence through submit → worker → one transaction (synthetic worlds, PostGIS test DB).

Every scenario submits a self-contained change run (no snapshot run exists in the database) and the
worker persists it. Expected values are compared against the in-memory Stage 4 result of the same
request, so persistence is checked to be lossless rather than re-derived.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import shapely
from pyogrio import raw
from shapely.geometry import shape
from sqlalchemy import Engine, text

from aether import science
from aether.db import repo
from aether.geo.grid import build_analysis_grid
from aether.jobs import queue
from aether.jobs.worker import run_job
from aether.services import aois
from aether.services.change_persistence import submit_change
from aether.services.change_run import ChangeRunRequest, change_idempotency_key, execute_change
from aether.storage.store import LocalFilesystemStore
from tests.change_fixtures import CX, PERIOD_A, PERIOD_B, ItemWorld, aoi_record, centred_box, world
from tests.unit.test_change_overlap_flags import RECTS_40

pytestmark = pytest.mark.db

EVENT = (centred_box(200),)


def _overlap_world() -> ItemWorld:
    """The verified C-17 fixture painted into the world: veg_loss (red up) and veg_gain (red down)."""
    g = build_analysis_grid(shape(aoi_record(2300)["geometry"]), 32610).g10
    r0 = c0 = 100
    boxes = tuple(
        (
            (g.x0 + 10 * (c0 + c), g.y0 - 10 * (r0 + r + h), g.x0 + 10 * (c0 + c + w), g.y0 - 10 * (r0 + r)),
            2800.0 if v == 1 else 1200.0,
        )
        for r, c, h, w, v in RECTS_40
    )
    return ItemWorld(red_boxes=boxes, event_boxes=EVENT)


SCENARIOS: dict[str, dict[str, Any]] = {
    "overlap": {"side": 2300, "b": _overlap_world()},
    "overlap_repeat": {"side": 2300, "b": _overlap_world()},  # same inputs, distinct AOI name → distinct run key
    "zero_events": {"side": 2300},
    "insufficient_observations": {"side": 2300, "n_b": 2, "b": ItemWorld(event_boxes=EVENT)},
    "insufficient_coverage": {"side": 2300, "b": ItemWorld(cloud_west_of=CX + 300, event_boxes=EVENT)},
    "coregistration_failed": {"side": 2300, "b": ItemWorld(shift_px=2, event_boxes=EVENT)},
    "not_verified": {"side": 1500, "b": ItemWorld(event_boxes=EVENT)},
    "unequal_counts": {"side": 2300, "n_a": 6, "n_b": 4, "b": ItemWorld(event_boxes=EVENT)},
}


def _q(engine: Engine, sql: str, **kw: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), kw).mappings()]


def _execute(engine: Engine, store: LocalFilesystemStore, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    side = spec["side"]
    world_kw = {k: v for k, v in spec.items() if k != "side"}
    searcher, reader = world(**world_kw)
    with engine.begin() as conn:
        project = repo.insert_project(conn, f"change {name}", "")
        aoi = aois.create_aoi(conn, project_id=project["id"], name=name, geometry=aoi_record(side)["geometry"], source="geojson")
        body, created = submit_change(conn, aoi_id=aoi["id"], period_a=PERIOD_A, period_b=PERIOD_B)
    assert created
    job = queue.claim(engine, "test-worker")
    assert job is not None and str(job["analysis_run_id"]) == str(body["analysis_run_id"])
    run_job(engine, job, searcher=searcher, reader=reader, store=store)
    # Stage 4 reference: the same request executed in memory with a fresh, identical world.
    ref_searcher, ref_reader = world(**world_kw)
    with engine.connect() as conn:
        aoi_row = repo.get_aoi(conn, aoi["id"])
    reference = execute_change(ChangeRunRequest(aoi_row, PERIOD_A, PERIOD_B), searcher=ref_searcher, reader=ref_reader)
    return {"run_id": body["analysis_run_id"], "job": job, "aoi": aoi_row, "reference": reference, "searcher": searcher}


@pytest.fixture(scope="module")
def runs(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL aether.project_delete = 'on'"))
        conn.execute(text("TRUNCATE projects, scenes, assets CASCADE"))
    store = LocalFilesystemStore(tmp_path_factory.mktemp("change_store"))
    out = {name: _execute(engine, store, name, spec) for name, spec in SCENARIOS.items()}
    for v in out.values():
        v["store"] = store
    return out


def _run(engine, run_id):
    (row,) = _q(
        engine,
        """SELECT type, lower(period_a) AS a_s, upper(period_a) - 1 AS a_e, lower(period_b) AS b_s, upper(period_b) - 1 AS b_e,
                  collection, parameters, parameters_hash, algorithm_versions, grid_hash, idempotency_key, status, outcome
           FROM analysis_runs WHERE id = :id""",
        id=run_id,
    )
    return row


def _evidence(engine, run_id, kind=None):
    rows = _q(
        engine,
        """SELECT evidence_id, kind, facts, confidence_band, limitations, artifact_ids, geometry_artifact_id,
                  lower(observed_period_a) AS pa, lower(observed_period_b) AS pb, ST_AsGeoJSON(geom, 15)::jsonb AS geometry
           FROM evidence WHERE analysis_run_id = :r ORDER BY created_at, evidence_id""",
        r=run_id,
    )
    return [r for r in rows if kind is None or r["kind"] == kind]


def _measurements(engine, evidence_id):
    rows = _q(
        engine,
        "SELECT quantity, statistic, status, value, unavailable_reason, unit, denominator_quantity, region, "
        "geometry_evidence_id FROM measurements WHERE evidence_id = :e",
        e=evidence_id,
    )
    return {(r["quantity"], r["statistic"]): r for r in rows}


# --- run identity, versions, periods -------------------------------------------------------------


def test_persisted_identity_equals_the_stage4_change_identity(engine, runs):
    r = runs["overlap"]
    row = _run(engine, r["run_id"])
    assert row["idempotency_key"] == r["reference"].idempotency_key
    assert row["idempotency_key"] == change_idempotency_key(r["aoi"], PERIOD_A, PERIOD_B)
    assert row["type"] == "change" and row["collection"] == science.STAC_COLLECTION
    assert (row["a_s"], row["a_e"], row["b_s"], row["b_e"]) == (PERIOD_A.start, PERIOD_A.end, PERIOD_B.start, PERIOD_B.end)
    assert row["parameters"] == science.change_parameters() == r["reference"].parameters
    assert row["algorithm_versions"] == science.CHANGE_ALGORITHM_VERSIONS == r["reference"].algorithm_versions
    assert row["status"] == "succeeded" and row["outcome"] == "completed"
    (pr,) = _q(
        engine, "SELECT algorithm_versions, parameters, environment FROM processing_runs WHERE analysis_run_id=:r", r=r["run_id"]
    )
    assert pr["algorithm_versions"] == science.CHANGE_ALGORITHM_VERSIONS and pr["parameters"] == science.change_parameters()
    assert pr["environment"]["pyogrio"] == "0.13.0" and "pyogrio_gdal" in pr["environment"]


def test_duplicate_identity_returns_the_existing_run(engine, runs):
    r = runs["overlap"]
    with engine.begin() as conn:
        body, created = submit_change(conn, aoi_id=r["aoi"]["id"], period_a=PERIOD_A, period_b=PERIOD_B)
    assert not created and body["analysis_run_id"] == r["run_id"] and body["cached"] is True
    assert len(_q(engine, "SELECT id FROM analysis_runs WHERE idempotency_key=:k", k=r["reference"].idempotency_key)) == 1
    with pytest.raises(Exception, match="analysis_runs_idem_ux"), engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO analysis_runs (project_id, aoi_id, type, period_a, period_b, collection, parameters,
                 parameters_hash, algorithm_versions, grid_hash, idempotency_key, status)
               SELECT project_id, aoi_id, type, period_a, period_b, collection, parameters, parameters_hash,
                 algorithm_versions, grid_hash, idempotency_key, 'queued' FROM analysis_runs WHERE id = :id"""
            ),
            {"id": r["run_id"]},
        )


def test_periods_a_and_b_persist_under_one_run_without_snapshot_runs(engine, runs):
    r = runs["unequal_counts"]
    rows = _q(
        engine,
        "SELECT period, status, count(*) AS n FROM analysis_run_inputs WHERE analysis_run_id=:r GROUP BY 1, 2",
        r=r["run_id"],
    )
    assert {(x["period"], x["status"]): x["n"] for x in rows} == {("a", "used"): 6, ("b", "used"): 4}
    assert _q(engine, "SELECT count(*) AS n FROM analysis_runs WHERE type='snapshot'") == [{"n": 0}]
    assert _run(engine, r["run_id"])["outcome"] == "completed"  # unequal observation counts accepted (C-11)
    cov = _evidence(engine, r["run_id"], "coverage_statistic")[0]
    ms = _measurements(engine, cov["evidence_id"])
    assert ms[("observations_usable.a", "count")]["value"] == 6 and ms[("observations_usable.b", "count")]["value"] == 4
    # Catalogue provenance: two searches, one per period.
    (pr,) = _q(engine, "SELECT steps FROM processing_runs WHERE analysis_run_id=:r", r=r["run_id"])
    searches = [s for s in pr["steps"] if s["kind"] == "catalog_search"]
    assert [s["period"] for s in searches] == ["a", "b"]


# --- measurements: coverage, per-period, delta (C-06) ---------------------------------------------


def test_delta_statistics_are_persisted_for_the_frozen_index_set_only(engine, runs):
    r = runs["overlap"]
    deltas = [e for e in _evidence(engine, r["run_id"], "index_statistic") if e["facts"]["period"] == "delta"]
    assert sorted(e["facts"]["index"] for e in deltas) == ["mndwi", "ndbi", "ndvi"]
    joint = r["reference"].analysis.joint
    for e in deltas:
        name = e["facts"]["index"]
        s = r["reference"].analysis.change.delta_stats[name]
        ms = _measurements(engine, e["evidence_id"])
        for stat in ("mean", "median", "p10", "p90"):
            assert ms[(f"index_delta.{name}", stat)]["value"] == getattr(s, stat)
        sup = ms[(f"index_delta_support.{name}", "fraction")]
        assert sup["value"] == joint.joint_valid_px / joint.aoi_grid_px and sup["denominator_quantity"] == "aoi_grid_pixels_10m"
        assert ms[(f"index_delta_pixels.{name}", "count")]["value"] == joint.joint_valid_px
        assert e["facts"]["sample_set"] == "joint_valid" and e["facts"]["delta"] == "B - A"
        assert e["pa"] == PERIOD_A.start and e["pb"] == PERIOD_B.start
    all_q = _q(engine, "SELECT DISTINCT quantity FROM measurements WHERE quantity LIKE 'index_delta%'")
    assert all(q["quantity"].split(".")[1] in ("ndvi", "mndwi", "ndbi") for q in all_q)  # never NDWI (C-06)
    per_period = [e for e in _evidence(engine, r["run_id"], "index_statistic") if e["facts"]["period"] in ("a", "b")]
    assert sorted((e["facts"]["period"], e["facts"]["index"]) for e in per_period) == sorted(
        (p, n) for p in "ab" for n in science.INDICES
    )
    for e in per_period:
        assert (e["pa"] is None) == (e["facts"]["period"] == "b") and (e["pb"] is None) == (e["facts"]["period"] == "a")


def test_coverage_evidence_carries_joint_and_per_period_quantities(engine, runs):
    r = runs["overlap"]
    (cov,) = _evidence(engine, r["run_id"], "coverage_statistic")
    ms = _measurements(engine, cov["evidence_id"])
    a = r["reference"].analysis
    assert ms[("joint_valid_area", "value")]["value"] == a.joint.joint_valid_px * a.pixel_area_true_m2
    assert ms[("joint_coverage", "fraction")]["value"] == a.joint.joint_coverage
    assert ms[("joint_coverage", "fraction")]["denominator_quantity"] == "aoi_grid_area"
    assert ms[("masked_fraction.clear.b", "fraction")]["denominator_quantity"] == "aoi_pixel_observations.b"
    assert cov["facts"]["run_type"] == "change" and cov["pb"] == PERIOD_B.start


# --- co-registration (C3 §9, C-01–C-03) ------------------------------------------------------------


def test_coregistration_evidence_and_unfilled_qa_artifacts(engine, runs):
    r = runs["overlap"]
    (ev,) = _evidence(engine, r["run_id"], "coregistration_check")
    ms = _measurements(engine, ev["evidence_id"])
    c = r["reference"].analysis.coregistration
    assert ms[("coreg_residual", "value")]["value"] == c.residual_px and ms[("coreg_residual", "value")]["unit"] == "px"
    assert ms[("coreg_shift_x", "value")]["value"] == c.shift_dx_px and ms[("coreg_shift_y", "value")]["value"] == c.shift_dy_px
    assert ms[("coreg_pcc_error", "value")]["value"] == c.pcc_error
    assert ms[("coreg_qa_nir_std.a", "value")]["value"] == c.nir_qa_std_a
    assert ms[("coreg_qa_nir_std.b", "value")]["value"] == c.nir_qa_std_b
    assert ms[("coreg_grid_joint_valid_fraction", "fraction")]["value"] == c.support.grid_fraction
    assert (ms[("coreg_window_height", "value")]["value"], ms[("coreg_window_width", "value")]["value"]) == c.support.window_shape
    assert ev["facts"]["status"] == "ok" and ev["facts"]["method"] == science.COREG_VERSION
    arts = _q(engine, "SELECT kind, band_names FROM artifacts WHERE id = ANY(:ids)", ids=ev["artifact_ids"])
    assert sorted(a["band_names"][0] for a in arts) == ["nir_qa", "nir_qa"] and {a["kind"] for a in arts} == {"composite"}
    (pr,) = _q(engine, "SELECT steps FROM processing_runs WHERE analysis_run_id=:r", r=r["run_id"])
    assert all(s["filled"] is False for s in pr["steps"] if s["kind"] == "qa_composite")


def test_persisted_qa_composite_is_the_unfilled_composite(engine, runs):
    """C-01: the stored QA artifact equals the in-memory unfilled composite, NaN gaps included."""
    import numpy as np
    import rasterio

    r = runs["insufficient_coverage"]  # period B is cloudy west of the AOI centre → QA gaps
    (ev,) = _evidence(engine, r["run_id"], "coregistration_check")
    qa_b = r["reference"].analysis.period_b.result.qa
    assert qa_b is not None and (~qa_b.array.valid).any()
    stored = []
    for a in _q(engine, "SELECT uri FROM artifacts WHERE id = ANY(:ids)", ids=ev["artifact_ids"]):
        with rasterio.open(a["uri"].removeprefix("file://")) as src:
            stored.append(src.read(1))
    match = [x for x in stored if np.array_equal(np.isnan(x), ~qa_b.array.valid)]
    assert len(match) == 1
    assert np.array_equal(match[0][qa_b.array.valid], qa_b.array.data[qa_b.array.valid])


def test_early_stops_keep_coregistration_evidence_with_the_outcome_as_reason(engine, runs):
    for name in ("insufficient_observations", "insufficient_coverage"):
        r = runs[name]
        assert _run(engine, r["run_id"])["outcome"] == name
        (ev,) = _evidence(engine, r["run_id"], "coregistration_check")
        assert ev["facts"]["status"] == "not_run"
        ms = _measurements(engine, ev["evidence_id"])
        for q in ("coreg_residual", "coreg_shift_x", "coreg_shift_y", "coreg_pcc_error"):
            assert ms[(q, "value")]["status"] == "unavailable" and ms[(q, "value")]["unavailable_reason"] == name
        kinds = sorted(e["kind"] for e in _evidence(engine, r["run_id"]))
        assert kinds == ["coregistration_check", "coverage_statistic", *["index_statistic"] * 8]
        assert _q(engine, "SELECT count(*) AS n FROM change_events WHERE analysis_run_id=:r", r=r["run_id"]) == [{"n": 0}]


def test_coregistration_failed_persists_the_residual_and_no_change_products(engine, runs):
    r = runs["coregistration_failed"]
    assert _run(engine, r["run_id"])["outcome"] == "coregistration_failed"
    (ev,) = _evidence(engine, r["run_id"], "coregistration_check")
    ms = _measurements(engine, ev["evidence_id"])
    assert ev["facts"]["status"] == "failed" and ms[("coreg_residual", "value")]["value"] > 0.5
    assert {e["kind"] for e in _evidence(engine, r["run_id"])} == {
        "coregistration_check",
        "coverage_statistic",
        "index_statistic",
    }
    kinds = {
        a["kind"]
        for a in _q(
            engine,
            "SELECT a.kind FROM artifacts a JOIN processing_runs p ON p.id = a.produced_by_run_id WHERE p.analysis_run_id=:r",
            r=r["run_id"],
        )
    }
    assert not kinds & {"delta", "z", "category", "event_label", "joint_valid", "vector_gpkg"}


def test_coregistration_not_verified_is_a_blocking_limitation_with_lower_bands(engine, runs):
    r = runs["not_verified"]
    assert _run(engine, r["run_id"])["outcome"] == "completed"
    (ev,) = _evidence(engine, r["run_id"], "coregistration_check")
    ms = _measurements(engine, ev["evidence_id"])
    assert ev["facts"]["status"] == "not_assessable"
    assert ms[("coreg_residual", "value")]["unavailable_reason"] == "insufficient_support"
    events = _evidence(engine, r["run_id"], "change_event")
    assert events and all(e["confidence_band"] == "lower" for e in events)
    for e in [*events, *_evidence(engine, r["run_id"], "change_summary")]:
        lims = {x["code"]: x["blocking"] for x in e["limitations"]}
        assert lims.get("coregistration_not_verified") is True


# --- events, C-17 overlap, C-18 flags, GeoPackage ---------------------------------------------------


def test_events_persist_with_global_labels_overlap_and_flags(engine, runs):
    r = runs["overlap"]
    ref = r["reference"].analysis.change.events
    events = _evidence(engine, r["run_id"], "change_event")
    by_label = {e["facts"]["event_label"]: e for e in events}
    assert sorted(by_label) == list(range(1, len(ref.events) + 1))
    rows = _q(
        engine, "SELECT evidence_id, category, touches_aoi_boundary FROM change_events WHERE analysis_run_id=:r", r=r["run_id"]
    )
    assert {x["evidence_id"] for x in rows} == {e["evidence_id"] for e in events}
    cats = sorted(e.category for e in ref.events)
    assert sorted(x["category"] for x in rows) == cats and {"veg_loss", "veg_gain"} <= set(cats)
    for e in ref.events:
        ev = by_label[e.event_label]
        ms = _measurements(engine, ev["evidence_id"])
        assert ev["facts"]["category"] == e.category and ev["facts"]["touches_aoi_boundary"] == e.touches_aoi_boundary
        assert ev["confidence_band"] == e.confidence_band  # the Stage 4 band, not recomputed
        assert ms[("event_area", "value")]["value"] == e.area_m2 and ms[("event_pixel_count", "count")]["value"] == e.pixel_count
        assert ms[("event_strength", "median")]["value"] == e.strength
        assert (
            ms[("event_support_a", "median")]["value"] == e.support_a
            and ms[("event_support_b", "median")]["value"] == e.support_b
        )
        for i in science.CHANGE_INDICES:
            assert ms[(f"event_median_delta.{i}", "median")]["value"] == e.median_delta[i]
            pos = ms[(f"event_flag_pixels.{i}.positive", "count")]["value"]
            neg = ms[(f"event_flag_pixels.{i}.negative", "count")]["value"]
            assert (pos, neg) == (e.flag_pixels[i]["positive"], e.flag_pixels[i]["negative"])
            zero = e.pixel_count - pos - neg  # C-18: derived, not stored
            assert zero == e.flag_zero_pixels(i) and ev["facts"]["index_flags"][i] == e.index_flags[i]
        assert all(m["region"] == "event" and m["geometry_evidence_id"] == ev["evidence_id"] for m in ms.values())
        got = shape(ev["geometry"])
        assert got.is_valid and got.equals(e.geometry.geom_4326)
    # The overlap survives: two events of different categories share exactly one pixel (UTM, authoritative).
    overlaps = [
        (x, y)
        for x in ref.events
        for y in ref.events
        if x.event_label < y.event_label
        and x.category != y.category
        and x.geometry.geom_utm.intersection(y.geometry.geom_utm).area > 0
    ]
    assert len(overlaps) == 1
    assert any(
        set(by_label[x.event_label]["facts"]["index_flags"]["ndvi"]) == {"-", "+"} for x in ref.events if x.category == "veg_loss"
    )


def test_change_summary_uses_union_changed_area(engine, runs):
    r = runs["overlap"]
    ref = r["reference"].analysis
    ev = ref.change.events
    (summary,) = _evidence(engine, r["run_id"], "change_summary")
    ms = _measurements(engine, summary["evidence_id"])
    assert ms[("changed_area", "value")]["value"] == ev.changed_px * ref.pixel_area_true_m2
    by_cat = sum(ms[(f"changed_area.{c}", "value")]["value"] for c in ev.changed_px_by_category)
    assert by_cat - ms[("changed_area", "value")]["value"] == pytest.approx(ev.overlap_px * ref.pixel_area_true_m2, abs=1e-6)
    assert ev.overlap_px == 1
    assert ms[("changed_fraction", "fraction")]["denominator_quantity"] == "joint_valid_area"
    assert ms[("change_event_count", "count")]["value"] == len(ev.events)
    assert {x["code"] for x in summary["limitations"]} >= {"thresholds_untuned"}


def test_geopackage_artifact_and_event_geometry_references(engine, runs):
    r = runs["overlap"]
    events = _evidence(engine, r["run_id"], "change_event")
    (gid,) = {e["geometry_artifact_id"] for e in events}
    (art,) = _q(
        engine, "SELECT sha256, uri, media_type, bytes, kind, crs_epsg, width, band_names FROM artifacts WHERE id=:i", i=gid
    )
    assert art["kind"] == "vector_gpkg" and art["media_type"] == "application/geopackage+sqlite3"
    assert art["crs_epsg"] == 32610 and art["width"] is None and art["band_names"] is None
    path = Path(art["uri"].removeprefix("file://"))
    data = path.read_bytes()
    import hashlib

    assert hashlib.sha256(data).hexdigest() == art["sha256"] and len(data) == art["bytes"]
    meta, fids, wkb, fields = raw.read(str(path), layer="change_events", return_fids=True)[:4]
    assert meta["crs"] == "EPSG:32610" and list(meta["fields"]) == ["event_label", "category", "touches_aoi_boundary"]
    ref = r["reference"].analysis.change.events.events
    assert [int(f) for f in fids] == [e.event_label for e in ref] == list(fields[0])
    for e, w in zip(ref, wkb, strict=True):
        assert shapely.from_wkb(w).equals(e.geometry.geom_utm)  # authoritative geometry, UTM, unaltered
    # The event_label artifact has 6 int32 bands, one per category (C-17).
    (lab,) = _q(
        engine,
        "SELECT a.dtype, a.band_names FROM evidence e JOIN artifacts a ON a.id = ANY(e.artifact_ids) "
        "WHERE e.evidence_id = :e AND a.kind = 'event_label'",
        e=events[0]["evidence_id"],
    )
    assert lab["dtype"] == "int32" and len(lab["band_names"]) == 6
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT last_change FROM gpkg_contents").fetchone()[0] == "2000-01-01T00:00:00.000Z"


def test_zero_event_completed_run_registers_an_empty_geopackage(engine, runs):
    r = runs["zero_events"]
    assert _run(engine, r["run_id"])["outcome"] == "completed"
    assert _evidence(engine, r["run_id"], "change_event") == []
    (summary,) = _evidence(engine, r["run_id"], "change_summary")
    ms = _measurements(engine, summary["evidence_id"])
    assert ms[("change_event_count", "count")]["value"] == 0 and ms[("changed_area", "value")]["value"] == 0
    arts = _q(
        engine, "SELECT kind, uri FROM artifacts WHERE id = ANY(:ids) AND kind = 'vector_gpkg'", ids=summary["artifact_ids"]
    )
    (gpkg,) = arts
    meta, fids = raw.read(gpkg["uri"].removeprefix("file://"), layer="change_events", return_fids=True)[:2]
    assert len(fids) == 0 and meta["geometry_type"] == "MultiPolygon"


# --- provenance (C-16) ------------------------------------------------------------------------------


def test_provenance_round_trips_with_period_tags_and_change_steps(engine, runs):
    r = runs["overlap"]
    (pr,) = _q(engine, "SELECT steps FROM processing_runs WHERE analysis_run_id=:r", r=r["run_id"])
    steps = pr["steps"]
    ref_steps = json.loads(json.dumps(r["reference"].steps, sort_keys=True))
    assert steps[: len(ref_steps)] == ref_steps  # the Stage 4 steps, unchanged
    kinds = [s["kind"] for s in steps]
    for kind in ("qa_composite", "coregistration", "change_rules", "morphology", "polygonization", "resampling"):
        assert kind in kinds
    assert [s["period"] for s in steps if s["kind"] == "qa_composite"] == ["a", "b"]
    assert [s["period"] for s in steps if s["kind"] == "display_composite"] == ["a", "b"]
    assert {(s["rule"], s["data_class"]) for s in steps if s["kind"] == "resampling" and s["rule"] == "R3"} == {
        ("R3", "boolean"),
        ("R3", "continuous"),
    }
    assert next(s for s in steps if s["kind"] == "change_rules")["parameters"]["calibration"] == "untuned"
    events = _evidence(engine, r["run_id"], "change_event")
    assert {e["confidence_band"] for e in events} <= {"higher", "moderate", "lower"}
    assert all(e["pb"] == PERIOD_B.start for e in events)


# --- determinism ------------------------------------------------------------------------------------


def _logical(engine, run_id):
    """Persisted content of a run without per-insert identifiers (evidence/measurement ids, timestamps)."""
    arts = sorted(
        (a["kind"], a["sha256"], a["bytes"])
        for a in _q(
            engine,
            "SELECT DISTINCT a.kind, a.sha256, a.bytes FROM evidence e JOIN artifacts a "
            "ON a.id = ANY(e.artifact_ids) OR a.id = e.geometry_artifact_id WHERE e.analysis_run_id = :r",
            r=run_id,
        )
    )
    evidence = []
    for e in _evidence(engine, run_id):
        ms = sorted(
            (k[0], k[1], v["status"], v["value"], v["unavailable_reason"])
            for k, v in _measurements(engine, e["evidence_id"]).items()
        )
        evidence.append(
            (e["kind"], json.dumps(e["facts"], sort_keys=True), e["confidence_band"], json.dumps(e["limitations"]), ms)
        )
    (pr,) = _q(engine, "SELECT steps FROM processing_runs WHERE analysis_run_id=:r", r=run_id)
    return arts, sorted(evidence, key=lambda x: (x[0], x[1])), json.dumps(pr["steps"], sort_keys=True)


def test_repeated_persistence_produces_equivalent_logical_records(engine, runs):
    a, b = runs["overlap"], runs["overlap_repeat"]
    assert _run(engine, a["run_id"])["idempotency_key"] != _run(engine, b["run_id"])["idempotency_key"]  # AOI differs
    la, lb = _logical(engine, a["run_id"]), _logical(engine, b["run_id"])
    assert la[0] == lb[0]  # identical artifact bytes (content-addressed), incl. the GeoPackage
    assert la[2] == lb[2]  # identical provenance steps
    strip = [(k, json.loads(f), band, lims, ms) for k, f, band, lims, ms in la[1]]
    strip_b = [(k, json.loads(f), band, lims, ms) for k, f, band, lims, ms in lb[1]]
    assert strip == strip_b  # identical facts, bands, limitations and measurement values


# --- Phase 1 regression -----------------------------------------------------------------------------


def test_phase1_snapshot_identity_is_unchanged():
    import uuid
    from datetime import date

    from aether.domain.periods import Period
    from aether.services.analyses import idempotency_key

    key = idempotency_key(
        {"id": uuid.UUID("00000000-0000-0000-0000-000000000001"), "geometry_hash": "0" * 64},
        "snapshot",
        Period(date(2024, 6, 1), date(2024, 8, 15)),
        science.snapshot_parameters(),
    )
    assert key == "3b2f1c793cda807ab850f3d4e7633f19f44c0b7e2aad6a0309e2ebebc71f6f0a"
    assert json.dumps(science.SNAPSHOT_ALGORITHM_VERSIONS, separators=(",", ":")) == (
        '{"ingest":"ingest.earth_search.v1","radiometry":"radiometry.s2c1.v1","grid":"grid.utm_s2.v1",'
        '"mask":"mask.scl.v1","composite":"composite.median_index.v1","indices":"indices.s2.v1",'
        '"coverage":"coverage.v1","zonal_stats":"zonal_stats@1"}'
    )
    assert json.dumps(science.snapshot_parameters(), separators=(",", ":")) == (
        '{"n_min":3,"max_tile_cloud":80.0,"mask_profile":"mask.scl.v1","indices":["mndwi","ndbi","ndvi","ndwi"],'
        '"stats_min_support_fraction":0.5,"coverage_thresholds":[0.5,0.8]}'
    )
