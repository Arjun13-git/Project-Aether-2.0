"""Change-run orchestration (Stage 4): self-contained A/B execution through the frozen primitives.

Synthetic worlds only (tests/change_fixtures.py); no network, no database.
"""

from __future__ import annotations

import ast
import inspect
import math
from pathlib import Path

import numpy as np
import pytest
import shapely

from aether import science
from aether.evidence.provenance import validate_steps
from aether.geo import coreg as coreg_mod
from aether.geo.change import engine as engine_mod
from aether.geo.change.rules import CATEGORIES
from aether.geo.composite import QaComposite
from aether.geo.period import run_period
from aether.geo.snapshot import run_snapshot
from aether.services import change_run
from aether.services.change_run import ChangeRunRequest, execute_change
from tests.change_fixtures import CX, CY, PERIOD_A, PERIOD_B, ItemWorld, aoi_record, centred_box, world

EVENT = (centred_box(200),)  # 400 m × 400 m veg-loss box = 1,600 G10 pixels
SPECK = (CX + 600, CY + 600, CX + 640, CY + 640)  # 4 × 4 px: below the MMU (~50 px)


def run(side: float = 2300, **kw):
    searcher, reader = world(**kw)
    req = ChangeRunRequest(aoi_record(side), PERIOD_A, PERIOD_B)
    return execute_change(req, searcher=searcher, reader=reader), searcher, reader


@pytest.fixture(scope="module")
def ok():
    return run(b=ItemWorld(event_boxes=(*EVENT, SPECK)))


# --- 1, 11, 12, 14: successful orchestration end to end ------------------------------------------


def test_successful_run_detects_the_injected_change(ok):
    res, _, _ = ok
    a = res.analysis
    assert a.outcome == "completed" and a.limitations == ()
    assert a.coregistration_status == "ok" and a.coregistration.residual_px == pytest.approx(0.0, abs=0.05)
    assert a.joint is not None and a.joint.joint_coverage > 0.8
    assert a.change is not None and a.change.reference.mode == "robust"
    (ev,) = a.change.events.events
    assert (ev.category, ev.category_code, ev.label, ev.pixel_count) == ("veg_loss", 4, 1, 1600)
    assert ev.area_m2 == 1600 * a.pixel_area_true_m2
    assert ev.median_delta["ndvi"] < -science.CHANGE_ABS_THRESHOLDS["ndvi"].value
    assert ev.strength is not None and ev.strength >= 5 and (ev.support_a, ev.support_b) == (6.0, 6.0)
    assert ev.confidence_band == "higher" and not ev.touches_aoi_boundary
    assert ev.limitations == ("thresholds_untuned",)
    # C-17: global label and union area; C-18: sign counts + sign-set fact propagate end to end.
    assert ev.event_label == 1 and a.change.events.changed_px == 1600 and a.change.events.overlap_px == 0
    assert ev.flag_pixels["ndvi"]["negative"] == 1600 and ev.index_flags["ndvi"] == ["-"]
    for n in science.CHANGE_INDICES:
        c = ev.flag_pixels[n]
        assert c["positive"] + c["negative"] + ev.flag_zero_pixels(n) == ev.pixel_count


def test_delta_layers_are_exactly_the_frozen_set(ok):
    ch = ok[0].analysis.change
    assert sorted(ch.deltas) == sorted(ch.delta_stats) == sorted(ch.significance) == ["mndwi", "ndbi", "ndvi"]
    for name, d in ch.deltas.items():
        a, b = (getattr(ok[0].analysis, p).result.composites[name] for p in ("period_a", "period_b"))
        ok_px = a.array.valid & b.array.valid
        assert np.array_equal(
            d.native.data[ok_px], b.array.data[ok_px].astype(np.float64) - a.array.data[ok_px].astype(np.float64)
        )
        s = ch.delta_stats[name]
        assert s.available and s.n_pixels == ok[0].analysis.joint.joint_valid_px
        assert s.support_fraction == ok[0].analysis.joint.joint_coverage


def test_morphology_mmu_and_polygons_propagate(ok):
    a = ok[0].analysis
    layers = {lay.category: lay for lay in a.change.events.layers}
    assert list(layers) == list(CATEGORIES.values())
    assert layers["veg_loss"].kept == 1 and layers["veg_loss"].removed == 1  # the 16 px speck is below the MMU
    assert a.change.events.mmu_min_px == math.ceil(5000 / a.pixel_area_true_m2)  # C5 §7, true pixel area
    (ev,) = a.change.events.events
    g = ev.geometry
    assert g.label == ev.label and g.pixel_count == ev.pixel_count
    assert g.geom_utm.area == ev.pixel_count * 100.0  # pixel-exact, planar UTM
    assert g.geom_utm.equals(shapely.box(*centred_box(200)))  # no simplification, buffering or clipping
    assert g.geom_4326.is_valid


# --- 2, 3, 18: self-contained execution ----------------------------------------------------------


def test_period_engine_is_invoked_independently_for_a_and_b(monkeypatch):
    calls = []

    def spy(**kw):
        calls.append({k: kw[k] for k in ("period", "qa_composite")} | {"items": sorted(s.item_id for s in kw["scenes"])})
        return run_period(**kw)

    monkeypatch.setattr(engine_mod, "run_period", spy)
    res, searcher, _ = run(b=ItemWorld(event_boxes=EVENT))
    assert [c["period"] for c in calls] == ["a", "b"] and all(c["qa_composite"] for c in calls)
    assert set(calls[0]["items"]).isdisjoint(calls[1]["items"])
    assert all("2023" in i for i in calls[0]["items"]) and all("2024" in i for i in calls[1]["items"])
    assert searcher.calls == [(PERIOD_A.start, PERIOD_A.end), (PERIOD_B.start, PERIOD_B.end)]
    assert res.outcome == "completed"


def _imports(module) -> set[str]:
    tree = ast.parse(Path(inspect.getfile(module)).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    return names


def test_change_run_has_no_dependency_on_snapshot_runs_or_the_database():
    for module in (change_run, engine_mod):
        imports = _imports(module)
        assert not any(m.startswith(("aether.db", "aether.jobs", "sqlalchemy")) for m in imports), imports
        assert "aether.services.snapshot_run" not in imports and "aether.geo.snapshot" not in imports
        src = Path(inspect.getfile(module)).read_text()
        assert "analysis_runs" not in src and "snapshot_run_id" not in src
    params = inspect.signature(execute_change).parameters
    assert list(params) == ["request", "searcher", "reader", "progress", "cancelled"]  # no engine/connection


def test_period_reuse_is_by_content_and_window_cache_only():
    """A snapshot of period A and the change run's period A read the same cache keys and produce
    byte-identical composites (so content addressing dedupes them); no record links the two."""
    from aether.geo.grid import build_analysis_grid
    from aether.services.catalog import discover

    res, searcher, reader = run(b=ItemWorld(event_boxes=EVENT))
    change_keys = {k for k in reader.calls if "2023" in k[1]}
    reader.calls.clear()
    aoi = res.request.aoi
    grid = build_analysis_grid(shapely.geometry.shape(aoi["geometry"]), aoi["utm_epsg"])
    _, scenes = discover(searcher, aoi, PERIOD_A)
    snap = run_snapshot(grid=grid, aoi_area_m2=1.0, scenes=scenes, reader=reader)
    assert set(reader.calls) == change_keys  # identical (grid_hash, item_id, asset_key) window-cache keys
    pa = res.analysis.period_a.result
    for name, c in snap.composites.items():
        assert c.array.data.tobytes() == pa.composites[name].array.data.tobytes()
        assert c.n_valid.tobytes() == pa.composites[name].n_valid.tobytes()
    # Snapshot provenance is untouched by the change path: no period tags, no QA composite.
    assert all("period" not in s for s in snap.steps) and all(s["kind"] != "qa_composite" for s in snap.steps)


# --- 7–10: outcome order (C-03) ------------------------------------------------------------------


def test_insufficient_observations_stops_before_coverage_and_registration():
    res, _, _ = run(n_b=2, b=ItemWorld(event_boxes=EVENT))
    a = res.analysis
    assert a.outcome == "insufficient_observations"
    assert (a.period_a.observations_usable, a.period_b.observations_usable) == (6, 2)
    assert a.coregistration is None and a.coregistration_status == "not_run"
    assert a.coregistration_unavailable_reason == "insufficient_observations"
    assert a.change is None and a.limitations == ()
    assert a.period_a.stats["ndvi"].available  # per-period statistics keep their own support rule
    assert all(s["kind"] not in ("coregistration", "change_rules", "morphology", "polygonization") for s in res.steps)


def test_insufficient_aoi_joint_coverage_stops_before_registration():
    res, _, _ = run(b=ItemWorld(cloud_west_of=CX + 300, event_boxes=EVENT))
    a = res.analysis
    assert a.joint.joint_coverage < 0.5
    assert a.outcome == "insufficient_coverage" and a.coregistration is None
    assert a.coregistration_unavailable_reason == "insufficient_coverage" and a.change is None


def test_coregistration_failure_aborts_change_detection():
    res, _, _ = run(b=ItemWorld(shift_px=2, event_boxes=EVENT))
    a = res.analysis
    assert a.joint.joint_coverage >= 0.5
    assert a.outcome == "coregistration_failed" and a.coregistration.status == "failed"
    assert a.coregistration.residual_px > science.COREG_MAX_RESIDUAL_PX.value
    assert a.change is None and a.limitations == ()
    assert [s["status"] for s in res.steps if s["kind"] == "coregistration"] == ["failed"]


def test_coregistration_not_verified_continues_with_blocking_limitation_and_lower_bands():
    res, _, _ = run(side=1500, b=ItemWorld(event_boxes=EVENT))  # grid < 256 px per axis (C-15)
    a = res.analysis
    assert a.outcome == "completed"
    assert a.coregistration.status == "not_assessable" and a.coregistration_unavailable_reason == "insufficient_support"
    assert "coregistration_not_verified" in a.limitations
    (ev,) = a.change.events.events
    assert ev.strength is not None and ev.strength >= 5 and ev.confidence_band == "lower"  # capped (C5 §8)


def test_partial_aoi_coverage_is_a_blocking_limitation_not_an_outcome():
    res, _, _ = run(b=ItemWorld(cloud_west_of=CX - 500, event_boxes=EVENT))
    a = res.analysis
    assert 0.5 <= a.joint.joint_coverage < 0.8
    assert a.outcome == "completed" and "partial_coverage" in a.limitations


# --- 13: robust vs absolute mode -----------------------------------------------------------------


def test_widespread_change_switches_to_absolute_only():
    wide = ((CX - 1150, CY - 1150, CX - 150, CY + 1150),)  # ~43 % of the AOI
    res, _, _ = run(b=ItemWorld(event_boxes=wide))
    a = res.analysis
    assert a.change.reference.mode == "absolute_only" and a.limitations == ("widespread_change",)
    assert a.change.reference.z is None
    (ev,) = a.change.events.events
    assert ev.strength is None and ev.strength_unavailable_reason == "robust_reference_unavailable"
    assert ev.confidence_band == "lower"


def test_identical_periods_without_noise_are_absolute_only_with_no_events():
    searcher, reader = world()
    reader.noise_dn = 0.0
    res = execute_change(ChangeRunRequest(aoi_record(2300), PERIOD_A, PERIOD_B), searcher=searcher, reader=reader)
    a = res.analysis
    assert a.change.reference.mode == "absolute_only" and "robust_reference_unavailable" in a.limitations
    assert a.change.events.events == ()


# --- 15: registration fill never reaches measurement ---------------------------------------------


def test_filled_registration_pixels_never_enter_measurement(monkeypatch):
    cloud = (centred_box(100, CX + 700, CY - 700),)  # QA-invalid pixels inside the registration window
    kw = {"b": ItemWorld(event_boxes=EVENT, cloud_boxes=cloud)}
    base, _, _ = run(**kw)
    seen: list[type] = []
    real = coreg_mod.assess_coregistration

    def spy(qa_a, qa_b):
        seen.extend([type(qa_a), type(qa_b)])
        return real(qa_a, qa_b)

    monkeypatch.setattr(engine_mod, "assess_coregistration", spy)
    monkeypatch.setattr(coreg_mod, "_filled_copy", lambda w, j: np.where(j, w, 1e6).astype(np.float64))
    patched, _, _ = run(**kw)
    assert seen == [QaComposite, QaComposite]
    b0, b1 = base.analysis, patched.analysis
    assert b1.coregistration.status == b0.coregistration.status == "ok"
    for n in science.CHANGE_INDICES:
        assert np.array_equal(b0.change.deltas[n].g10.data, b1.change.deltas[n].g10.data, equal_nan=True)
        assert b0.change.delta_stats[n] == b1.change.delta_stats[n]
    assert [(e.category, e.pixel_count, e.geometry.geom_utm.wkb) for e in b0.change.events.events] == [
        (e.category, e.pixel_count, e.geometry.geom_utm.wkb) for e in b1.change.events.events
    ]
    qa_b = b1.period_b.result.qa
    assert qa_b is not None and np.isnan(qa_b.array.data[~qa_b.array.valid]).all() and (~qa_b.array.valid).any()


# --- 16: provenance ------------------------------------------------------------------------------


def test_provenance_carries_periods_and_every_change_step(ok):
    steps = ok[0].steps
    validate_steps(steps)
    kinds = [s["kind"] for s in steps]
    assert kinds[:2] == ["catalog_search", "catalog_search"] and [s["period"] for s in steps[:2]] == ["a", "b"]
    assert [s["period"] for s in steps if s["kind"] == "qa_composite"] == ["a", "b"]
    assert all(s["filled"] is False for s in steps if s["kind"] == "qa_composite")
    for kind in ("coregistration", "change_rules", "morphology"):
        assert kinds.count(kind) == 1
    assert kinds.count("polygonization") == len(CATEGORIES)
    assert {(s["rule"], s["data_class"]) for s in steps if s["kind"] == "resampling" and s["rule"] == "R3"} == {
        ("R3", "boolean"),
        ("R3", "continuous"),
    }
    per_obs = [s for s in steps if s["kind"] in ("source_read", "masking", "index", "compositing", "radiometric_normalization")]
    assert per_obs and all(s.get("period") in ("a", "b") for s in per_obs)
    delta_stats = [s for s in steps if s["kind"] == "statistics" and s.get("region") == "joint_valid"]
    assert sorted(s["index"] for s in delta_stats) == ["mndwi", "ndbi", "ndvi"]
    rules = next(s for s in steps if s["kind"] == "change_rules")
    assert rules["mode"] == "robust" and rules["parameters"]["k"] == 3.0 and rules["parameters"]["calibration"] == "untuned"


def test_result_carries_change_versions_and_frozen_parameters(ok):
    res = ok[0]
    assert res.algorithm_versions == science.CHANGE_ALGORITHM_VERSIONS
    assert res.algorithm_versions is not science.SNAPSHOT_ALGORITHM_VERSIONS
    assert res.parameters == science.change_parameters()


# --- 17: determinism -----------------------------------------------------------------------------


def _fingerprint(res):
    a = res.analysis
    return (
        res.idempotency_key,
        a.outcome,
        a.limitations,
        a.joint.joint_valid_px,
        a.coregistration.residual_px,
        {n: s for n, s in a.change.delta_stats.items()},
        a.change.categories.data.tobytes(),
        [
            (e.category_code, e.label, e.pixel_count, e.median_delta, e.strength, e.geometry.geom_utm.wkb)
            for e in a.change.events.events
        ],
    )


def test_identical_requests_are_deterministic():
    kw = {"b": ItemWorld(event_boxes=(*EVENT, (CX - 900, CY + 500, CX - 700, CY + 700)))}
    r1, _, _ = run(**kw)
    r2, _, _ = run(**kw)
    assert _fingerprint(r1) == _fingerprint(r2)
    events = r1.analysis.change.events.events
    assert len(events) == 2
    assert [(e.category_code, e.label) for e in events] == sorted((e.category_code, e.label) for e in events)
    layers = {lay.code: lay for lay in r1.analysis.change.events.layers}
    first_px = [int(np.flatnonzero(layers[e.category_code].labels.data.ravel() == e.label)[0]) for e in events]
    assert first_px == sorted(first_px)  # raster-scan order within the category


def test_pair_rules_are_enforced():
    from datetime import date

    from aether.domain.errors import AetherError
    from aether.domain.periods import Period

    searcher, reader = world()
    bad = Period(date(2024, 1, 1), date(2024, 2, 14))  # not season-matched with June A
    with pytest.raises(AetherError, match="periods_not_season_matched"):
        execute_change(ChangeRunRequest(aoi_record(2300), PERIOD_A, bad), searcher=searcher, reader=reader)
