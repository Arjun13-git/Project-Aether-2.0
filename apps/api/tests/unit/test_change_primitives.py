"""Change primitives for ``change.index_diff.v1``: R3, joint_valid, Δ, delta statistics (C-06),
robust reference and significance (C5 §4–§5), morphology, 8-connected components and MMU (C5 §7).

Each expected value is computed independently in numpy from the contract formulas.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.change.delta import IndexDelta, delta_statistics, index_delta
from aether.geo.change.joint import joint_valid
from aether.geo.change.morphology import apply_mmu, clean_mask, label_components, mmu_min_pixels
from aether.geo.change.robust import RobustReference, robust_reference, significance
from aether.geo.composite import IndexComposite, QaComposite
from aether.geo.grid import GridArray
from aether.geo.resample import r3_nearest_20_to_10
from tests.factories import make_grid

N_MIN = 3


def joint_valid_np(a, b, in10: np.ndarray, in20: np.ndarray, g10, g20):
    """Test helper: wrap plain in-AOI masks as GridArrays on the given grids."""
    return joint_valid(
        a, b, GridArray(g10, in10, np.ones(g10.shape, bool)), GridArray(g20, in20, np.ones(g20.shape, bool)), g10, g20
    )


def grids(w10: int = 8, h10: int = 6):
    return make_grid("G10", w10, h10), make_grid("G20", w10 // 2, h10 // 2)


def comp(index: str, grid, values, n_valid) -> IndexComposite:
    values = np.broadcast_to(np.asarray(values, dtype=np.float32), grid.shape)
    n_valid = np.broadcast_to(np.asarray(n_valid, dtype=np.uint8), grid.shape).copy()
    ok = n_valid >= N_MIN
    return IndexComposite(index, GridArray(grid, np.where(ok, values, np.nan).astype(np.float32), ok), n_valid, N_MIN, ("o",))


def period(g10, g20, *, ndvi=0.5, mndwi=-0.2, ndbi=-0.1, nv10=5, nv20=5, ndwi_nv=5):
    return {
        "ndvi": comp("ndvi", g10, ndvi, nv10),
        "mndwi": comp("mndwi", g20, mndwi, nv20),
        "ndbi": comp("ndbi", g20, ndbi, nv20),
        "ndwi": comp("ndwi", g10, 0.0, ndwi_nv),
    }


# --- R3 -------------------------------------------------------------------------------------------


def test_r3_replicates_each_g20_cell_to_its_2x2_block():
    g10, g20 = grids()
    src = GridArray(g20, np.arange(12, dtype=np.float64).reshape(3, 4), np.ones(g20.shape, bool))
    out, step = r3_nearest_20_to_10(src, g10, "continuous")
    assert out.grid is g10
    assert np.array_equal(out.data, np.kron(src.data, np.ones((2, 2))))
    assert step["rule"] == "R3" and step["method"] == "nearest" and step["data_class"] == "continuous"


def test_r3_requires_exact_nesting():
    g10, _ = grids()
    bad = make_grid("G20", 4, 3, x0=600020.0)
    with pytest.raises(InvariantViolation, match="grid_mismatch"):
        r3_nearest_20_to_10(GridArray(bad, np.zeros(bad.shape), np.ones(bad.shape, bool)), g10, "boolean")


# --- joint_valid (C5 §2) --------------------------------------------------------------------------


def reference_joint(a, b, in10, in20):
    """Independent numpy statement of C5 §2."""
    elig20 = in20.copy()
    for name in ("mndwi", "ndbi"):
        elig20 &= (a[name].n_valid >= N_MIN) & (b[name].n_valid >= N_MIN)
    elig20_10 = np.kron(elig20, np.ones((2, 2), dtype=bool)).astype(bool)
    return in10 & (a["ndvi"].n_valid >= N_MIN) & (b["ndvi"].n_valid >= N_MIN) & elig20_10


def test_joint_valid_is_the_intersection_of_every_clause():
    g10, g20 = grids()
    nv_ndvi_a = np.full(g10.shape, 5)
    nv_ndvi_a[0, 0] = 2  # NDVI insufficient in A at one G10 pixel
    nv_mndwi_b = np.full(g20.shape, 5)
    nv_mndwi_b[1, 1] = 1  # MNDWI insufficient in B at one G20 cell → 4 G10 pixels
    nv_ndbi_a = np.full(g20.shape, 5)
    nv_ndbi_a[2, 3] = 0  # NDBI insufficient in A
    a = period(g10, g20, nv10=nv_ndvi_a)
    a["ndbi"] = comp("ndbi", g20, -0.1, nv_ndbi_a)
    b = period(g10, g20)
    b["mndwi"] = comp("mndwi", g20, -0.2, nv_mndwi_b)
    in10 = np.ones(g10.shape, bool)
    in10[5, 7] = False
    in20 = np.ones(g20.shape, bool)
    in20[0, 3] = False  # parent G20 cell outside the AOI
    j = joint_valid_np(a, b, in10, in20, g10, g20)
    expected = reference_joint(a, b, in10, in20)
    assert np.array_equal(j.mask, expected)
    assert not j.mask[0, 0] and not j.mask[2:4, 2:4].any() and not j.mask[4:6, 6:8].any()
    assert not j.mask[0:2, 6:8].any() and not j.mask[5, 7]
    assert j.joint_valid_px == int(expected.sum()) == 48 - 1 - 4 - 4 - 4  # (5,7) already in the NDBI block
    assert j.steps[0]["rule"] == "R3" and j.steps[0]["data_class"] == "boolean"


def test_joint_valid_excludes_ndwi():
    g10, g20 = grids()
    in10, in20 = np.ones(g10.shape, bool), np.ones(g20.shape, bool)
    a, b = period(g10, g20), period(g10, g20, ndwi_nv=0)  # NDWI insufficient everywhere in B
    assert joint_valid_np(a, b, in10, in20, g10, g20).mask.all()
    without = {k: v for k, v in b.items() if k != "ndwi"}
    assert joint_valid_np(a, without, in10, in20, g10, g20).mask.all()


def test_joint_coverage_uses_the_aoi_grid_denominator():
    g10, g20 = grids()
    in10 = np.zeros(g10.shape, bool)
    in10[:, :4] = True  # 24 AOI pixels of 48 grid pixels
    in20 = np.zeros(g20.shape, bool)
    in20[:, :2] = True
    nv = np.full(g10.shape, 5)
    nv[:3, :4] = 0  # 12 of the 24 AOI pixels insufficient
    j = joint_valid_np(period(g10, g20, nv10=nv), period(g10, g20), in10, in20, g10, g20)
    assert (j.joint_valid_px, j.aoi_grid_px) == (12, 24)
    assert j.joint_coverage == 0.5  # 12 / 24, not 12 / 48
    assert j.joint_valid_area_m2(99.5) == 12 * 99.5 and j.aoi_grid_area_m2(99.5) == 24 * 99.5


def test_composite_values_cannot_make_a_pixel_joint_valid():
    """No fill leakage: membership comes from n_valid only, never from composite values."""
    g10, g20 = grids()
    in10, in20 = np.ones(g10.shape, bool), np.ones(g20.shape, bool)
    nv = np.full(g10.shape, 5)
    nv[1, 1] = 2
    a = period(g10, g20, nv10=nv)
    filled = a["ndvi"].array.data.copy()
    filled[1, 1] = 0.42  # a value written under an insufficient pixel (e.g. by a fill)
    leaky = dict(a, ndvi=IndexComposite("ndvi", GridArray(g10, filled, a["ndvi"].array.valid), a["ndvi"].n_valid, N_MIN, ("o",)))
    j = joint_valid_np(leaky, period(g10, g20), in10, in20, g10, g20)
    assert not j.mask[1, 1]
    d = index_delta(leaky["ndvi"], period(g10, g20)["ndvi"], g10)
    assert np.isnan(d.native.data[1, 1]) and not d.native.valid[1, 1]
    # A validity mask that disagrees with n_valid is a defect, not something to trust.
    forged = IndexComposite("ndvi", GridArray(g10, filled, np.ones(g10.shape, bool)), a["ndvi"].n_valid, N_MIN, ("o",))
    with pytest.raises(InvariantViolation, match="composite_validity_mismatch"):
        joint_valid_np(dict(a, ndvi=forged), period(g10, g20), in10, in20, g10, g20)


def test_joint_valid_rejects_same_shaped_aoi_masks_from_other_grids():
    g10, g20 = grids()
    a = period(g10, g20)
    ok10 = GridArray(g10, np.ones(g10.shape, bool), np.ones(g10.shape, bool))
    ok20 = GridArray(g20, np.ones(g20.shape, bool), np.ones(g20.shape, bool))
    o10 = make_grid("G10", 8, 6, x0=600020.0)
    o20 = make_grid("G20", 4, 3, grid_hash="other")
    with pytest.raises(InvariantViolation, match="grid_mismatch"):
        joint_valid(a, a, GridArray(o10, np.ones(o10.shape, bool), np.ones(o10.shape, bool)), ok20, g10, g20)
    with pytest.raises(InvariantViolation, match="grid_mismatch"):
        joint_valid(a, a, ok10, GridArray(o20, np.ones(o20.shape, bool), np.ones(o20.shape, bool)), g10, g20)
    with pytest.raises(InvariantViolation, match="aoi_mask_input"):
        joint_valid(a, a, np.ones(g10.shape, bool), ok20, g10, g20)  # type: ignore[arg-type]
    assert joint_valid(a, a, ok10, ok20, g10, g20).mask.all()


def test_change_functions_reject_qa_composites_with_a_coded_error():
    g10, g20 = grids()
    a = period(g10, g20)
    qa = QaComposite("coregistration_qa", "nir", a["ndvi"].array, a["ndvi"].n_valid, 3, ("o",))
    in10, in20 = np.ones(g10.shape, bool), np.ones(g20.shape, bool)
    with pytest.raises(InvariantViolation, match="change_input_type"):
        joint_valid_np(dict(a, ndvi=qa), a, in10, in20, g10, g20)
    j = joint_valid_np(a, a, in10, in20, g10, g20)
    deltas = {n: index_delta(a[n], a[n], g10) for n in science.CHANGE_INDICES}
    with pytest.raises(InvariantViolation, match="change_input_type"):
        delta_statistics(qa, j)  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation, match="change_input_type"):
        robust_reference(dict(deltas, ndvi=qa), j)  # type: ignore[dict-item]
    ref = robust_reference(deltas, j)
    with pytest.raises(InvariantViolation, match="change_input_type"):
        significance(dict(deltas, ndvi=qa), j, ref)  # type: ignore[dict-item]


def test_joint_valid_requires_change_indices_on_their_grids():
    g10, g20 = grids()
    in10, in20 = np.ones(g10.shape, bool), np.ones(g20.shape, bool)
    a = period(g10, g20)
    with pytest.raises(InvariantViolation, match="change_index_missing"):
        joint_valid_np({k: v for k, v in a.items() if k != "ndbi"}, a, in10, in20, g10, g20)
    wrong = dict(a, mndwi=comp("mndwi", g10, 0.0, 5))
    with pytest.raises(InvariantViolation, match="grid_mismatch"):
        joint_valid_np(wrong, a, in10, in20, g10, g20)


# --- Δ and delta statistics (C5 §3, C-06) ---------------------------------------------------------


def test_delta_is_b_minus_a_exactly_on_native_grid():
    g10, _g20 = grids()
    rng = np.random.default_rng(0)
    va, vb = rng.uniform(-1, 1, g10.shape), rng.uniform(-1, 1, g10.shape)
    a, b = comp("ndvi", g10, va, 5), comp("ndvi", g10, vb, 5)
    d = index_delta(a, b, g10)
    expected = b.array.data.astype(np.float64) - a.array.data.astype(np.float64)
    assert np.array_equal(d.native.data, expected) and d.native.data.dtype == np.float64
    assert d.g10 is d.native and d.steps == ()


def test_g20_delta_is_r3_replicated_to_g10():
    g10, g20 = grids()
    va = np.arange(12, dtype=np.float32).reshape(3, 4) / 20
    nv = np.full(g20.shape, 5)
    nv[0, 0] = 0
    a, b = comp("mndwi", g20, va, nv), comp("mndwi", g20, va + np.float32(0.25), 5)
    d = index_delta(a, b, g10)
    assert d.native.grid is g20 and d.g10.grid is g10
    assert np.array_equal(d.g10.data[2:, 2:], np.kron(d.native.data, np.ones((2, 2)))[2:, 2:])
    assert np.isnan(d.g10.data[:2, :2]).all() and not d.g10.valid[:2, :2].any()
    assert d.steps[0]["rule"] == "R3" and d.steps[0]["data_class"] == "continuous"


def test_ndwi_and_mixed_indices_are_rejected():
    g10, g20 = grids()
    with pytest.raises(InvariantViolation, match="change_index_not_supported"):
        index_delta(comp("ndwi", g10, 0.1, 5), comp("ndwi", g10, 0.2, 5), g10)
    with pytest.raises(InvariantViolation, match="change_index_mismatch"):
        index_delta(comp("ndvi", g10, 0.1, 5), comp("ndwi", g10, 0.2, 5), g10)
    qa = QaComposite("coregistration_qa", "nir", comp("ndvi", g10, 0.1, 5).array, np.full(g10.shape, 5, np.uint8), 3, ("o",))
    for args in ((qa, qa), (qa, comp("ndvi", g10, 0.1, 5)), (comp("ndvi", g10, 0.1, 5), qa)):
        with pytest.raises(InvariantViolation, match="change_input_type") as exc:
            index_delta(*args, g10)  # type: ignore[arg-type]
        assert exc.value.code == "change_input_type" and exc.value.http_status == 500
    ga = GridArray(g10, np.zeros(g10.shape), np.ones(g10.shape, bool))
    j = joint_valid_np(period(g10, g20), period(g10, g20), np.ones(g10.shape, bool), np.ones(g20.shape, bool), g10, g20)
    with pytest.raises(InvariantViolation, match="change_index_not_supported"):
        delta_statistics(IndexDelta("ndwi", ga, ga, ()), j)


def _stats_setup(nv_b):
    g10, g20 = grids(20, 10)
    rng = np.random.default_rng(0)
    a = period(g10, g20, ndvi=rng.uniform(0, 0.8, g10.shape))
    b = period(g10, g20, ndvi=rng.uniform(0, 0.8, g10.shape), nv10=nv_b)
    in10, in20 = np.ones(g10.shape, bool), np.ones(g20.shape, bool)
    in10[0, :] = False  # AOI is a strict subset of the grid
    in20[0, :] = False
    in10[1, :] = False  # the G10 children of excluded G20 row 0
    j = joint_valid_np(a, b, in10, in20, g10, g20)
    return index_delta(a["ndvi"], b["ndvi"], g10), j


def test_delta_statistics_match_numpy_over_joint_valid_only():
    nv = np.full((10, 20), 5)
    nv[5, 3:9] = 0  # Δ undefined (NaN) inside the AOI
    d, j = _stats_setup(nv)
    s = delta_statistics(d, j)
    vals = d.g10.data[j.mask]
    assert np.isfinite(vals).all() and np.isnan(d.g10.data).sum() == 6
    p10, med, p90 = np.percentile(vals, [10, 50, 90], method="linear")
    assert s.available and s.unavailable_reason is None
    assert (s.mean, s.median, s.p10, s.p90) == (float(vals.mean()), float(med), float(p10), float(p90))
    assert (s.n_pixels, s.region_pixels) == (j.joint_valid_px, j.aoi_grid_px) == (154, 160)
    assert s.support_fraction == j.joint_coverage == 154 / 160


def test_delta_statistics_unavailable_below_half_support():
    nv = np.full((10, 20), 5)
    nv[2:7, :] = 0  # 100 of 160 AOI pixels insufficient → support 0.375
    d, j = _stats_setup(nv)
    s = delta_statistics(d, j)
    assert not s.available and s.unavailable_reason == "insufficient_support"
    assert s.mean is s.median is s.p10 is s.p90 is None
    assert s.support_fraction == pytest.approx(60 / 160)


# --- robust reference and significance (C5 §4–§5) -------------------------------------------------


def change_inputs(d_ndvi, d_mndwi, d_ndbi, size: int = 40):
    """Deltas on a size × size G10 grid fully in-AOI; G20 deltas given on G20."""
    g10, g20 = grids(size, size)
    a = period(g10, g20, ndvi=0.3, mndwi=0.1, ndbi=-0.1)
    b = period(
        g10,
        g20,
        ndvi=np.float32(0.3) + np.asarray(d_ndvi, np.float32),
        mndwi=np.float32(0.1) + np.asarray(d_mndwi, np.float32),
        ndbi=np.float32(-0.1) + np.asarray(d_ndbi, np.float32),
    )
    j = joint_valid_np(a, b, np.ones(g10.shape, bool), np.ones(g20.shape, bool), g10, g20)
    deltas = {n: index_delta(a[n], b[n], g10) for n in science.CHANGE_INDICES}
    return deltas, j


def test_robust_median_mad_and_z_match_the_formula():
    rng = np.random.default_rng(1)
    deltas, j = change_inputs(rng.normal(0, 0.02, (40, 40)), rng.normal(0, 0.02, (20, 20)), rng.normal(0, 0.02, (20, 20)))
    ref = robust_reference(deltas, j)
    assert ref.mode == "robust" and ref.limitations == () and ref.n_eligible == 1600
    for name in science.CHANGE_INDICES:
        v = deltas[name].g10.data[j.mask]
        med = np.median(v)
        mad = np.median(np.abs(v - med))
        assert ref.median[name] == med and ref.mad[name] == mad
        assert ref.z is not None
        assert np.array_equal(ref.z[name][j.mask], (v - med) / (1.4826 * mad))
    assert ref.widespread_fraction == float(np.mean(np.abs(ref.z["ndvi"][j.mask]) > 2.0))


def test_fewer_than_1000_eligible_pixels_is_absolute_only():
    rng = np.random.default_rng(1)
    deltas, j = change_inputs(rng.normal(0, 0.02, (30, 30)), rng.normal(0, 0.02, (15, 15)), rng.normal(0, 0.02, (15, 15)), 30)
    ref = robust_reference(deltas, j)
    assert j.joint_valid_px == 900
    assert ref.mode == "absolute_only" and ref.limitations == ("robust_reference_unavailable",) and ref.z is None


def test_mad_zero_for_any_index_is_absolute_only():
    rng = np.random.default_rng(1)
    deltas, j = change_inputs(rng.normal(0, 0.02, (40, 40)), rng.normal(0, 0.02, (20, 20)), np.zeros((20, 20)))
    ref = robust_reference(deltas, j)
    assert ref.mad["ndbi"] == 0.0
    assert ref.mode == "absolute_only" and ref.limitations == ("robust_reference_unavailable",)


def test_widespread_change_is_absolute_only():
    rng = np.random.default_rng(1)
    d_ndvi = rng.normal(0, 0.02, (40, 40))
    d_ndvi[:16, :] -= 0.4  # 40 % of the AOI changed
    deltas, j = change_inputs(d_ndvi, rng.normal(0, 0.02, (20, 20)), rng.normal(0, 0.02, (20, 20)))
    ref = robust_reference(deltas, j)
    assert ref.widespread_fraction is not None and ref.widespread_fraction > 0.30
    assert ref.mode == "absolute_only" and ref.limitations == ("widespread_change",) and ref.z is None


def _manual(deltas_g10: dict[str, np.ndarray], z: dict[str, np.ndarray] | None, mode):
    g10 = make_grid("G10", deltas_g10["ndvi"].shape[1], deltas_g10["ndvi"].shape[0])
    ones = np.ones(g10.shape, bool)
    from aether.geo.change.joint import JointValid

    j = JointValid(g10, ones, int(ones.sum()), int(ones.sum()), ())
    deltas = {n: IndexDelta(n, GridArray(g10, v, ones), GridArray(g10, v, ones), ()) for n, v in deltas_g10.items()}
    ref = RobustReference(mode, (), j.joint_valid_px, {}, {}, None, z)
    return significance(deltas, j, ref)


def test_absolute_significance_threshold_is_inclusive_and_signed():
    d = {
        "ndvi": np.array([[0.15, -0.15, 0.1499, -0.3]]),
        "mndwi": np.array([[0.20, -0.1999, 0.0, 0.5]]),
        "ndbi": np.array([[0.10, 0.0999, -0.10, 0.0]]),
    }
    s = _manual(d, None, "absolute_only")
    assert s["ndvi"].tolist() == [[1, -1, 0, -1]]
    assert s["mndwi"].tolist() == [[1, 0, 0, 1]]
    assert s["ndbi"].tolist() == [[1, 0, -1, 0]]
    assert all(v.dtype == np.int8 for v in s.values())


def test_robust_significance_requires_both_z_and_absolute_tests():
    d = {n: np.array([[0.5, 0.5, 0.01, -0.5]]) for n in science.CHANGE_INDICES}
    z = {n: np.array([[3.0, 2.99, 10.0, -3.0]]) for n in science.CHANGE_INDICES}
    s = _manual(d, z, "robust")
    for n in science.CHANGE_INDICES:
        assert s[n].tolist() == [[1, 0, 0, -1]]


def test_significance_is_zero_outside_joint_valid():
    rng = np.random.default_rng(1)
    deltas, j = change_inputs(np.full((40, 40), -0.5), rng.normal(0, 0.02, (20, 20)), rng.normal(0, 0.02, (20, 20)))
    hole = j.mask.copy()
    hole[:5, :5] = False
    from aether.geo.change.joint import JointValid

    jj = JointValid(j.grid, hole, int(hole.sum()), j.aoi_grid_px, ())
    s = significance(deltas, jj, robust_reference(deltas, jj))
    assert (s["ndvi"][:5, :5] == 0).all() and (s["ndvi"][hole] == -1).all()


# --- morphology, components and MMU (C5 §7) -------------------------------------------------------


def mga(m: np.ndarray, **grid_kw) -> GridArray:
    """Boolean mask on a G10 grid of matching size."""
    return GridArray(make_grid("G10", m.shape[1], m.shape[0], **grid_kw), m, np.ones(m.shape, bool))


def jv(joint: np.ndarray, **grid_kw):
    from aether.geo.change.joint import JointValid

    g = make_grid("G10", joint.shape[1], joint.shape[0], **grid_kw)
    return JointValid(g, joint, int(joint.sum()), int(joint.sum()), ())


def test_opening_removes_specks_and_closing_stays_inside_joint_valid():
    m = np.zeros((16, 16), bool)
    m[1, 1] = True  # speck → removed by opening
    m[3:13, 3:13] = True  # 10 × 10 block
    m[8, 8] = False  # one-pixel interior hole → closed
    joint = np.ones_like(m)
    joint[5, 5] = False  # an unobserved pixel inside the block stays out
    out = clean_mask(mga(m), jv(joint))
    assert out.grid == mga(m).grid
    assert not out.data[1, 1] and out.data[8, 8] and not out.data[5, 5]
    assert not (out.data & ~joint).any()
    assert out.data.sum() == 100 - 1  # block kept, hole closed, unobserved pixel excluded


def test_clean_mask_rejects_same_shaped_masks_from_different_grids():
    m = np.ones((8, 8), bool)
    joint = np.ones((8, 8), bool)
    for kw in ({"x0": 600010.0}, {"y0": 4200020.0}, {"epsg": 32611}, {"grid_hash": "other"}):
        with pytest.raises(InvariantViolation, match="grid_mismatch"):
            clean_mask(mga(m), jv(joint, **kw))
    g20 = make_grid("G20", 8, 8)
    with pytest.raises(InvariantViolation, match="grid_mismatch"):
        clean_mask(GridArray(g20, m, np.ones(m.shape, bool)), jv(joint))


def test_morphology_rejects_untyped_inputs():
    m = np.ones((8, 8), bool)
    with pytest.raises(InvariantViolation, match="morphology_input"):
        clean_mask(m, jv(m))  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation, match="morphology_input"):
        clean_mask(mga(m), m)  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation, match="morphology_input"):
        label_components(m, make_grid("G10", 8, 8))  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation, match="label_input"):
        apply_mmu(np.zeros((8, 8), np.int32), 0, 1, make_grid("G10", 8, 8))  # type: ignore[arg-type]


def test_labels_and_mmu_reject_a_raster_not_on_the_supplied_grid():
    m = mga(np.ones((8, 8), bool))
    other = make_grid("G10", 8, 8, x0=600010.0)
    with pytest.raises(InvariantViolation, match="grid_mismatch"):
        label_components(m, other)
    labels, n = label_components(m, m.grid)
    with pytest.raises(InvariantViolation, match="grid_mismatch"):
        apply_mmu(labels, n, 1, other)


def test_diagonal_pixels_form_one_component_under_8_connectivity():
    m = np.zeros((4, 4), bool)
    m[0, 0] = m[1, 1] = m[2, 2] = True
    labels, n = label_components(mga(m), mga(m).grid)
    assert n == 1 and labels.data.dtype == np.int32 and labels.grid == mga(m).grid
    from scipy import ndimage

    assert ndimage.label(m)[1] == 3  # 4-connectivity would split it


def test_labels_are_raster_scan_ordered_and_deterministic():
    rng = np.random.default_rng(0)
    m = rng.random((60, 60)) > 0.7
    g = mga(m).grid
    l1, n1 = label_components(mga(m), g)
    l2, n2 = label_components(mga(m.copy()), g)
    assert n1 == n2 and np.array_equal(l1.data, l2.data)
    first = [int(np.flatnonzero(l1.data.ravel() == k)[0]) for k in range(1, n1 + 1)]
    assert first == sorted(first)


def test_mmu_uses_ceil_and_keeps_stable_order():
    assert mmu_min_pixels(5000, 100.0) == 50
    assert mmu_min_pixels(5000, 99.9) == 51
    assert mmu_min_pixels(science.CHANGE_MMU_M2.value, 100.0) == 50
    m = np.zeros((20, 30), bool)
    m[0:7, 0:7] = True  # 49 px → removed
    m[10:15, 0:10] = True  # 50 px → kept
    m[0:10, 20:26] = True  # 60 px → kept
    g = mga(m).grid
    labels, n = label_components(mga(m), g)
    lab = labels.data
    assert n == 3 and lab[0, 0] == 1 and lab[0, 20] == 2 and lab[10, 0] == 3
    kept, k, removed = apply_mmu(labels, n, 50, g)
    assert (k, removed) == (2, 1) and kept.grid == g
    kd = kept.data
    assert kd[0, 0] == 0 and kd[0, 20] == 1 and kd[10, 0] == 2  # renumbered in the same order
    assert np.bincount(kd.ravel()).tolist() == [600 - 110, 60, 50]
