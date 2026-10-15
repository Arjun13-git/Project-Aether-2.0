"""C-12 synthetic reference tests (Phase 2 freeze; TESTING-STRATEGY change consistency (b)/(b′)).

- A, noisy offset: seed 0, Gaussian noise σ = 0.02, global +0.05 offset, ≥ 1,000 eligible pixels
  → ``robust`` mode, 0 change events.
- B, exact uniform offset: uniform +0.05 → ``absolute_only`` + ``robust_reference_unavailable``,
  0 change events (a uniform offset has MAD = 0).

The parameters are frozen by C-12 and must not be changed to make an implementation pass.

"0 change events" is checked through every primitive that creates events: significance →
opening/closing ∧ joint_valid → 8-connected components → MMU. It is evaluated on the union of all
significant pixels: every category mask is a subset of that union, and opening and closing are
increasing operators, so an empty cleaned union implies zero events in every category.
"""

from __future__ import annotations

import numpy as np
import pytest
from pyproj import Transformer

from aether import science
from aether.geo.change.delta import index_delta
from aether.geo.change.joint import joint_valid
from aether.geo.change.morphology import apply_mmu, clean_mask, label_components, mmu_min_pixels
from aether.geo.change.robust import robust_reference, significance
from aether.geo.composite import IndexComposite
from aether.geo.grid import GridArray, pixel_area_true_m2
from tests.factories import make_grid

SEED = 0
SIGMA = 0.02
OFFSET = 0.05
SIZE = 300  # 300 × 300 = 90,000 eligible G10 pixels (≥ 1,000)
BASE = {"ndvi": 0.40, "mndwi": -0.30, "ndbi": -0.15}


def joint_valid_np(a, b, in10: np.ndarray, in20: np.ndarray, g10, g20):
    """Test helper: wrap plain in-AOI masks as GridArrays on the given grids."""
    return joint_valid(
        a, b, GridArray(g10, in10, np.ones(g10.shape, bool)), GridArray(g20, in20, np.ones(g20.shape, bool)), g10, g20
    )


def composite(index: str, grid, values: np.ndarray) -> IndexComposite:
    data = np.asarray(values, dtype=np.float32)
    return IndexComposite(index, GridArray(grid, data, np.ones(grid.shape, bool)), np.full(grid.shape, 6, np.uint8), 3, ("o",))


def count_events(offsets: dict[str, float], noise_sigma: float | None):
    g10, g20 = make_grid("G10", SIZE, SIZE), make_grid("G20", SIZE // 2, SIZE // 2)
    rng = np.random.default_rng(SEED)
    a, b = {}, {}
    for name in science.CHANGE_INDICES:
        g = g10 if science.INDICES[name].grid == "G10" else g20
        before = np.full(g.shape, BASE[name])
        noise = rng.normal(0.0, noise_sigma, g.shape) if noise_sigma is not None else 0.0
        a[name] = composite(name, g, before)
        b[name] = composite(name, g, before + offsets[name] + noise)
    j = joint_valid_np(a, b, np.ones(g10.shape, bool), np.ones(g20.shape, bool), g10, g20)
    deltas = {n: index_delta(a[n], b[n], g10) for n in science.CHANGE_INDICES}
    ref = robust_reference(deltas, j)
    s = significance(deltas, j, ref)

    lon, lat = Transformer.from_crs(g10.epsg, 4326, always_xy=True).transform(g10.x0, g10.y0)
    min_px = mmu_min_pixels(science.CHANGE_MMU_M2.value, pixel_area_true_m2(g10, lon, lat))
    any_sig = np.zeros(g10.shape, bool)
    for v in s.values():
        any_sig |= v != 0
    cleaned = clean_mask(GridArray(g10, any_sig, j.mask.copy()), j)
    labels, n = label_components(cleaned, g10)
    _, events, _ = apply_mmu(labels, n, min_px, g10)
    return j, ref, int(any_sig.sum()), events


@pytest.mark.parametrize(
    "offsets",
    [
        pytest.param({"ndvi": OFFSET, "mndwi": 0.0, "ndbi": 0.0}, id="ndvi_offset"),
        pytest.param(dict.fromkeys(science.CHANGE_INDICES, OFFSET), id="all_indices_offset"),
    ],
)
def test_c12_a_noisy_global_offset_is_robust_with_zero_events(offsets):
    j, ref, _n_sig, events = count_events(offsets, SIGMA)
    assert j.joint_valid_px == SIZE * SIZE >= 1000
    assert ref.mode == "robust" and ref.limitations == ()
    assert ref.median["ndvi"] == pytest.approx(offsets["ndvi"], abs=0.001)
    assert events == 0


def test_c12_b_exact_uniform_offset_is_absolute_only_with_zero_events():
    j, ref, n_sig, events = count_events(dict.fromkeys(science.CHANGE_INDICES, OFFSET), None)
    assert j.joint_valid_px == SIZE * SIZE
    assert all(m == 0.0 for m in ref.mad.values())
    assert ref.mode == "absolute_only" and ref.limitations == ("robust_reference_unavailable",)
    assert ref.z is None
    assert n_sig == 0 and events == 0


def test_identical_periods_give_zero_events():
    _j, ref, n_sig, events = count_events(dict.fromkeys(science.CHANGE_INDICES, 0.0), None)
    assert ref.mode == "absolute_only" and n_sig == 0 and events == 0
