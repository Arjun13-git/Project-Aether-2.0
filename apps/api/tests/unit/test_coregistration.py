"""Co-registration check ``coreg.phase_corr.v1`` (C3 §9; Phase 2 freeze C-01, C-02, C-03, C-12).

Synthetic ≥ 256² fixtures are generated programmatically (C-12, ADR-023): a seeded, smooth random
texture translated by the Fourier shift theorem, so the true sub-pixel shift is known exactly.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from scipy import ndimage

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo import coreg
from aether.geo.composite import QaComposite, nir_qa_composite
from aether.geo.coreg import (
    assess_coregistration,
    coregistration_outcome,
    coregistration_step,
    pre_registration_outcome,
    registration_support,
)
from aether.geo.grid import GridArray
from aether.geo.indices import Reflectance
from aether.geo.stats import zonal_stats
from tests.factories import make_grid


def texture(shape: tuple[int, int], seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return ndimage.gaussian_filter(rng.normal(size=shape), 2.0, mode="wrap")


def translate(img: np.ndarray, dy: float, dx: float) -> np.ndarray:
    return np.fft.ifftn(ndimage.fourier_shift(np.fft.fftn(img), (dy, dx))).real


def qa(grid, values: np.ndarray, valid: np.ndarray | None = None) -> QaComposite:
    valid = np.ones(grid.shape, bool) if valid is None else valid
    data = np.where(valid, 0.3 + 0.05 * values, np.nan).astype(np.float32)
    n_valid = np.where(valid, 5, 0).astype(np.uint8)
    return QaComposite("coregistration_qa", "nir", GridArray(grid, data, valid), n_valid, 3, ("o1",))


def pair(dy: float, dx: float, size: int = 300, valid_b: np.ndarray | None = None):
    g = make_grid("G10", size, size)
    base = texture(g.shape)
    return qa(g, base), qa(g, translate(base, dy, dx), valid_b)


# --- translation recovery and the residual gate ---------------------------------------------------


@pytest.mark.parametrize(
    ("dy", "dx", "status"),
    [(0.0, 0.0, "ok"), (0.3, 0.0, "ok"), (0.0, -0.3, "ok"), (0.6, 0.0, "failed"), (2.0, -1.0, "failed")],
)
def test_translation_is_recovered_and_gated(dy, dx, status):
    a, b = pair(dy, dx)
    r = assess_coregistration(a, b)
    assert r.status == status
    # phase_cross_correlation(A, B) returns the shift that registers B onto A: −(applied shift).
    assert r.shift_dy_px == pytest.approx(-dy, abs=0.05)
    assert r.shift_dx_px == pytest.approx(-dx, abs=0.05)
    assert r.residual_px == pytest.approx(np.hypot(dy, dx), abs=0.05)


def test_residual_above_half_pixel_is_coregistration_failed():
    r = assess_coregistration(*pair(0.6, 0.0))
    assert r.residual_px is not None and r.residual_px > science.COREG_MAX_RESIDUAL_PX.value
    assert coregistration_outcome(r) == "coregistration_failed"
    ok = assess_coregistration(*pair(0.3, 0.0))
    assert coregistration_outcome(ok) is None and ok.limitations == ()


def test_upsample_factor_10_is_used(monkeypatch):
    calls = []
    real = coreg.phase_cross_correlation

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(coreg, "phase_cross_correlation", spy)
    r = assess_coregistration(*pair(0.3, 0.0))
    assert calls == [{"upsample_factor": 10}]
    assert r.upsample_factor == 10
    # Behavioural check: a 0.3 px shift is resolved at 0.1 px, which integer correlation cannot do.
    assert r.shift_dy_px == pytest.approx(-0.3, abs=0.05)


# --- support (grid-level) and the not-assessable state --------------------------------------------


def test_window_255_is_not_assessable_256_is():
    g = make_grid("G10", 300, 300)
    base = texture(g.shape)
    for rows, expected in ((255, False), (256, True)):
        valid = np.zeros(g.shape, bool)
        valid[:rows, :] = True
        s = registration_support(qa(g, base, valid), qa(g, base, valid))
        assert s.window_shape == (rows, 300)
        assert s.grid_fraction >= 0.5
        assert s.assessable is expected
        assert s.reason == (None if expected else "insufficient_window")


def test_grid_fraction_boundary_is_half_of_the_whole_grid():
    g = make_grid("G10", 512, 512)
    base = texture(g.shape)
    valid = np.zeros(g.shape, bool)
    valid[:256, :] = True  # exactly 0.5 of the grid; window 256 × 512
    s = registration_support(qa(g, base, valid), qa(g, base, valid))
    assert s.grid_fraction == 0.5 and s.assessable
    valid[100, 100] = False  # one pixel less, same window → below 0.5
    s = registration_support(qa(g, base, valid), qa(g, base, valid))
    assert s.grid_fraction < 0.5 and not s.assessable and s.reason == "insufficient_grid_support"
    assert s.window_shape == (256, 512)


def test_not_assessable_result_is_unavailable_with_blocking_limitation():
    g = make_grid("G10", 200, 200)  # smaller than 256 px per axis (C-15)
    r = assess_coregistration(qa(g, texture(g.shape)), qa(g, texture(g.shape)))
    assert r.status == "not_assessable"
    assert (r.shift_dy_px, r.shift_dx_px, r.residual_px, r.pcc_error, r.nir_qa_std_a) == (None,) * 5
    assert r.unavailable_reason == "insufficient_support"
    assert r.limitations == ("coregistration_not_verified",)
    assert coregistration_outcome(r) is None  # continues; no new outcome (C-03)
    assert coregistration_step(r)["status"] == "not_assessable"


def test_support_uses_the_joint_validity_of_both_images():
    g = make_grid("G10", 300, 300)
    base = texture(g.shape)
    half = np.zeros(g.shape, bool)
    half[:, :140] = True
    other = np.zeros(g.shape, bool)
    other[:, 140:] = True
    s = registration_support(qa(g, base, half), qa(g, base, other))
    assert s.joint_valid_px == 0 and not s.assessable


# --- AOI-level coverage gate precedes registration (C-03 steps 1–2) -------------------------------


def test_aoi_joint_coverage_below_half_is_rejected_before_registration():
    assert pre_registration_outcome(5, 5, 0.49, 3) == "insufficient_coverage"
    assert pre_registration_outcome(5, 5, None, 3) == "insufficient_coverage"
    assert pre_registration_outcome(5, 5, 0.5, 3) is None
    # Observations are checked first (step 1).
    assert pre_registration_outcome(2, 5, 0.1, 3) == "insufficient_observations"
    assert pre_registration_outcome(5, 2, 0.9, 3) == "insufficient_observations"


# --- joint-valid masking and fill isolation (C-01) ------------------------------------------------


def _invalid_block(shape) -> np.ndarray:
    valid = np.ones(shape, bool)
    valid[40:90, 50:120] = False
    return valid


def test_values_under_invalid_pixels_do_not_change_the_result():
    g = make_grid("G10", 300, 300)
    base = texture(g.shape)
    valid = _invalid_block(g.shape)
    a = qa(g, base)
    b = qa(g, translate(base, 0.3, 0.0), valid)
    garbage = b.array.data.copy()
    garbage[~valid] = 1e6  # values a filled or corrupted pixel could carry
    b2 = dataclasses.replace(b, array=GridArray(g, garbage, valid))
    r1, r2 = assess_coregistration(a, b), assess_coregistration(a, b2)
    assert r1 == r2


def test_qa_sigma_and_error_use_unfilled_jointly_valid_window_pixels():
    g = make_grid("G10", 300, 300)
    base = texture(g.shape)
    valid = _invalid_block(g.shape)
    a, b = qa(g, base), qa(g, translate(base, 0.3, 0.0), valid)
    r = assess_coregistration(a, b)
    r0, r1, c0, c1 = r.support.window
    joint = (a.array.valid & b.array.valid)[r0:r1, c0:c1]
    assert r.nir_qa_std_a == pytest.approx(float(np.std(a.array.data[r0:r1, c0:c1][joint].astype(np.float64))))
    assert r.nir_qa_std_b == pytest.approx(float(np.std(b.array.data[r0:r1, c0:c1][joint].astype(np.float64))))
    assert r.pcc_error is not None and np.isfinite(r.pcc_error)
    assert r.status == "ok"


def test_fill_never_leaves_the_estimator_and_qa_stays_unfilled():
    g = make_grid("G10", 300, 300)
    base = texture(g.shape)
    valid = _invalid_block(g.shape)
    a, b = qa(g, base), qa(g, translate(base, 0.3, 0.0), valid)
    before = [(q.array.data.tobytes(), q.array.valid.tobytes(), q.n_valid.tobytes()) for q in (a, b)]
    r = assess_coregistration(a, b)
    after = [(q.array.data.tobytes(), q.array.valid.tobytes(), q.n_valid.tobytes()) for q in (a, b)]
    assert before == after
    assert np.isnan(b.array.data[~valid]).all()  # persisted QA composite remains unfilled
    for f in dataclasses.fields(r):
        assert not isinstance(getattr(r, f.name), np.ndarray)
    assert all(not isinstance(v, np.ndarray) for v in dataclasses.asdict(r.support).values())


def test_filled_copy_is_private():
    window = np.array([[1.0, np.nan], [3.0, 5.0]], dtype=np.float32)
    joint = ~np.isnan(window)
    out = coreg._filled_copy(window, joint)
    assert not np.shares_memory(out, window)
    assert out[0, 1] == 3.0 and np.isnan(window[0, 1])


# --- NIR QA composite (C4 §3) and its type isolation ----------------------------------------------


def _nir(obs_id: str, g, values, valid=None) -> Reflectance:
    values = np.asarray(values, dtype=np.float32)
    valid = np.ones(values.shape, bool) if valid is None else valid
    return Reflectance("nir", obs_id, GridArray(g, values, valid))


def test_nir_qa_composite_median_and_n_min():
    g = make_grid("G10", 2, 1)
    q = GridArray(g, np.zeros(g.shape, np.uint8), np.ones(g.shape, bool))
    cloudy = GridArray(g, np.zeros(g.shape, np.uint8), np.array([[True, False]]))
    obs = [_nir("a", g, [[0.1, 0.1]]), _nir("b", g, [[0.2, 0.2]]), _nir("c", g, [[0.4, 0.4]]), _nir("d", g, [[0.9, 0.9]])]
    c = nir_qa_composite(obs, [q, q, q, cloudy], n_min=3)
    assert c.purpose == "coregistration_qa" and c.band == "nir"
    assert c.n_valid.tolist() == [[4, 3]]
    assert c.array.data[0, 0] == pytest.approx(0.3)  # even count → mean of the two middle values
    assert c.array.data[0, 1] == pytest.approx(0.2)
    c4 = nir_qa_composite(obs, [q, q, q, cloudy], n_min=4)
    assert c4.array.valid.tolist() == [[True, False]] and np.isnan(c4.array.data[0, 1])


def test_qa_composite_rejects_non_nir_and_is_rejected_by_measurements():
    g = make_grid("G10", 2, 1)
    q = GridArray(g, np.zeros(g.shape, np.uint8), np.ones(g.shape, bool))
    red = Reflectance("red", "a", GridArray(g, np.zeros(g.shape, np.float32), np.ones(g.shape, bool)))
    with pytest.raises(InvariantViolation, match="qa_composite_input"):
        nir_qa_composite([red], [q], n_min=1)
    c = nir_qa_composite([_nir("a", g, [[0.1, 0.2]])], [q], n_min=1)
    with pytest.raises(InvariantViolation, match="stats_input_type"):
        zonal_stats(c, np.ones(g.shape, bool))  # type: ignore[arg-type]


def test_coregistration_rejects_index_composites_and_grid_mismatch():
    from aether.geo.composite import IndexComposite

    g = make_grid("G10", 4, 4)
    ga = GridArray(g, np.zeros(g.shape, np.float32), np.ones(g.shape, bool))
    idx = IndexComposite("ndvi", ga, np.full(g.shape, 5, np.uint8), 3, ("o",))
    with pytest.raises(InvariantViolation, match="coreg_input_type"):
        registration_support(idx, idx)  # type: ignore[arg-type]
    other = make_grid("G10", 4, 4, x0=600010.0)
    with pytest.raises(InvariantViolation, match="grid_mismatch"):
        registration_support(qa(g, np.zeros(g.shape)), qa(other, np.zeros(other.shape)))
