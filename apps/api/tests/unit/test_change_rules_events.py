"""C5 §6 categories (P8) and C5 §7–§8 event extraction, measurements and confidence bands (P12)."""

from __future__ import annotations

import numpy as np
import pytest

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.change.delta import IndexDelta
from aether.geo.change.events import confidence_band, extract_events
from aether.geo.change.joint import JointValid
from aether.geo.change.robust import RobustReference
from aether.geo.change.rules import CATEGORIES, categorize
from aether.geo.composite import IndexComposite
from aether.geo.coreg import CoregResult, CoregSupport
from aether.geo.grid import GridArray
from tests.factories import make_grid


def setup(w=8, h=2):
    g10, g20 = make_grid("G10", w, h), make_grid("G20", w // 2, h // 2)
    joint = JointValid(g10, np.ones(g10.shape, bool), w * h, w * h, ())
    return g10, g20, joint


def mndwi(g20, values):
    v = np.asarray(values, np.float32)
    return IndexComposite("mndwi", GridArray(g20, v, np.ones(g20.shape, bool)), np.full(g20.shape, 5, np.uint8), 3, ("o",))


def delta(g10, name, values):
    v = np.asarray(values, np.float64)
    ga = GridArray(g10, v, np.ones(g10.shape, bool))
    return IndexDelta(name, ga, ga, ())


def test_category_rules_first_match_wins():
    g10, g20, joint = setup(w=8, h=2)
    # Columns (G10 pairs share one G20 parent): each pixel exercises one rule.
    s = {
        "mndwi": np.array([[1, -1, 0, 0, 0, 0, 0, 1]] * 2, np.int8),
        "ndbi": np.array([[1, 1, 1, 0, 0, 0, 1, 0]] * 2, np.int8),
        "ndvi": np.array([[-1, -1, -1, -1, 1, 0, 0, 0]] * 2, np.int8),
    }
    d_ndvi = delta(g10, "ndvi", [[-0.3, -0.3, -0.3, -0.3, 0.3, 0.0, 0.1, 0.0]] * 2)
    mndwi_a = mndwi(g20, [[0.2, 0.2, 0.2, -0.2]])  # parent of columns 0–1, 2–3, 4–5, 6–7
    mndwi_b = mndwi(g20, [[0.3, 0.3, 0.3, -0.1]])
    codes, steps = categorize(s, {"ndvi": d_ndvi}, mndwi_a, mndwi_b, joint)
    # col 0: water_increase beats built/veg; col 1: water_decrease (A > 0); col 2: built (ΔNDVI ≤ 0) beats veg_loss;
    # col 3: veg_loss; col 4: veg_gain; col 5: none; col 6: NDBI+ but ΔNDVI > 0 → other; col 7: MNDWI+ but B ≤ 0 → other.
    assert codes.data[0].tolist() == [1, 2, 3, 4, 5, 0, 6, 6]
    assert codes.data.dtype == np.uint8 and [s["rule"] for s in steps] == ["R3", "R3"]
    assert set(CATEGORIES) == {1, 2, 3, 4, 5, 6}


def test_categories_are_zero_outside_joint_valid():
    g10, g20, _ = setup(w=4, h=2)
    joint_mask = np.ones(g10.shape, bool)
    joint_mask[:, 0] = False
    joint = JointValid(g10, joint_mask, int(joint_mask.sum()), 8, ())
    s = {n: np.full(g10.shape, -1, np.int8) for n in science.CHANGE_INDICES}
    codes, _ = categorize(
        s, {"ndvi": delta(g10, "ndvi", np.full(g10.shape, -0.3))}, mndwi(g20, [[0.5, 0.5]]), mndwi(g20, [[0.5, 0.5]]), joint
    )
    assert (codes.data[:, 0] == 0).all() and (codes.data[:, 1:] == 2).all()


def _coreg(status, residual):
    support = CoregSupport(1, 1, 1.0, (0, 1, 0, 1), status != "not_assessable", None)
    return CoregResult(status, support, residual_px=residual)


def _ref(mode):
    return RobustReference(mode, (), 2000, {}, {}, 0.0, {} if mode == "robust" else None)


@pytest.mark.parametrize(
    ("mode", "strength", "sa", "sb", "status", "residual", "band"),
    [
        ("robust", 5.0, 6, 6, "ok", 0.3, "higher"),
        ("robust", 5.0, 6, 6, "ok", 0.31, "moderate"),
        ("robust", 4.99, 6, 6, "ok", 0.0, "moderate"),
        ("robust", 5.0, 6, 5, "ok", 0.0, "moderate"),
        ("robust", 3.5, 4, 4, "ok", 0.0, "moderate"),
        ("robust", 3.49, 6, 6, "ok", 0.0, "lower"),
        ("robust", 9.0, 3, 9, "ok", 0.0, "lower"),
        ("robust", 9.0, 9, 9, "not_assessable", None, "lower"),
        ("absolute_only", None, 9, 9, "ok", 0.0, "lower"),
    ],
)
def test_confidence_band_rules(mode, strength, sa, sb, status, residual, band):
    assert confidence_band(_ref(mode), strength, sa, sb, _coreg(status, residual)) == band


def _events_inputs(cat: np.ndarray, in_aoi: np.ndarray):
    h, w = cat.shape
    g10 = make_grid("G10", w, h)
    joint = JointValid(g10, in_aoi.copy(), int(in_aoi.sum()), int(in_aoi.sum()), ())
    d = {n: delta(g10, n, np.where(cat > 0, -0.4, 0.0)) for n in science.CHANGE_INDICES}
    z = {n: np.where(cat > 0, -8.0, 0.0) for n in science.CHANGE_INDICES}
    nv = np.full(g10.shape, 7, np.uint8)
    comp = IndexComposite("ndvi", GridArray(g10, np.zeros(g10.shape, np.float32), np.ones(g10.shape, bool)), nv, 3, ("o",))
    s_ndvi = np.where(cat == 4, -1, np.where(cat == 5, 1, 0)).astype(np.int8)
    sig = {"ndvi": s_ndvi, "mndwi": np.zeros(g10.shape, np.int8), "ndbi": np.zeros(g10.shape, np.int8)}
    return dict(
        categories=GridArray(g10, cat.astype(np.uint8), in_aoi.copy()),
        joint=joint,
        deltas=d,
        sig=sig,
        ref=RobustReference("robust", (), 2000, {}, {}, 0.0, z),
        ndvi_a=comp,
        ndvi_b=comp,
        in_aoi10=GridArray(g10, in_aoi.copy(), np.ones(g10.shape, bool)),
        coreg=_coreg("ok", 0.1),
        pixel_area_true_m2=100.0,
    )


def test_events_per_category_with_measurements_and_boundary_flag():
    cat = np.zeros((30, 30), np.uint8)
    cat[2:12, 2:12] = 4  # 100 px veg_loss inside the AOI
    cat[15:25, 20:30] = 5  # 100 px veg_gain touching the AOI edge (column 29 adjoins the outside)
    cat[20:26, 2:8] = 4  # 36 px veg_loss → below the MMU (50 px)
    in_aoi = np.ones((30, 30), bool)
    in_aoi[:, 29:] = False
    in_aoi[:, 28] = True
    kw = _events_inputs(cat, in_aoi)
    cat_joint = cat.copy()
    cat_joint[:, 29] = 0
    kw["categories"] = GridArray(kw["joint"].grid, cat_joint, in_aoi.copy())
    res = extract_events(**kw)
    assert res.mmu_min_px == 50
    assert [(e.category, e.label) for e in res.events] == [("veg_loss", 1), ("veg_gain", 1)]
    loss, gain = res.events
    assert loss.pixel_count == 100 and loss.area_m2 == 10_000.0 and not loss.touches_aoi_boundary
    assert loss.median_delta == dict.fromkeys(science.CHANGE_INDICES, -0.4)
    assert loss.strength == 8.0 and (loss.support_a, loss.support_b) == (7.0, 7.0) and loss.confidence_band == "higher"
    assert gain.pixel_count == 90 and gain.touches_aoi_boundary
    assert gain.limitations == ("thresholds_untuned", "event_truncated_by_aoi")
    layers = {lay.category: lay for lay in res.layers}
    assert (layers["veg_loss"].components, layers["veg_loss"].kept, layers["veg_loss"].removed) == (2, 1, 1)
    morph = res.steps[0]
    assert morph["kind"] == "morphology" and morph["per_category"]["veg_loss"] == {"components": 2, "kept": 1, "removed": 1}


def test_events_refuse_to_run_after_coregistration_failure():
    cat = np.zeros((10, 10), np.uint8)
    kw = _events_inputs(cat, np.ones((10, 10), bool))
    kw["coreg"] = _coreg("failed", 0.9)
    with pytest.raises(InvariantViolation, match="change_after_coreg_failure"):
        extract_events(**kw)
