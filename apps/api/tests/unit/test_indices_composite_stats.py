"""Indices (C4 §4), composite strategy pin (I-18), statistics (C4 §5), coverage (C6 §5.2)."""

from __future__ import annotations

import numpy as np
import pytest

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.composite import DisplayComposite, display_rgb_composite, median_index_composite
from aether.geo.indices import Reflectance, compute_index
from aether.geo.stats import snapshot_coverage, snapshot_outcome, zonal_stats
from tests.factories import garr, make_grid

G10 = make_grid("G10", w=3, h=1)
G20 = make_grid("G20", w=3, h=1)


def refl(key: str, obs: str, values, grid=G10) -> Reflectance:
    return Reflectance(key, obs, garr(grid, np.array([values], np.float32)))


def ok(grid=G10, n=3):
    return garr(grid, np.ones((1, n), bool), np.ones((1, n), bool))


@pytest.mark.parametrize(
    ("name", "a", "b", "grid"),
    [
        ("ndvi", "nir", "red", G10),
        ("ndwi", "green", "nir", G10),
        ("mndwi", "green", "swir16", G20),
        ("ndbi", "swir16", "nir", G20),
    ],
)
def test_hand_computed_values(name, a, b, grid):
    spec = science.INDICES[name]
    av, bv = [0.4, 0.05, 0.3], [0.1, 0.15, 0.3]
    io = compute_index(spec, refl(a, "o1", av, grid), refl(b, "o1", bv, grid), ok(grid))
    expected = [(x - y) / (x + y) for x, y in zip(av, bv, strict=True)]
    assert np.allclose(io.array.data[0], expected, atol=1e-6)
    assert io.array.valid.all()


def test_undefined_rules():
    spec = science.INDICES["ndvi"]
    nir = refl("nir", "o", [0.00005, 0.3, np.nan])
    red = refl("red", "o", [0.00004, -0.1, 0.1])  # (0.3+0.1)/(0.3-0.1) = 2 → |x| > 1
    io = compute_index(spec, nir, red, ok())
    assert not io.array.valid.any() and np.isnan(io.array.data).all()
    assert io.undefined_counts == {"input_invalid": 1, "denominator_le_eps": 1, "abs_gt_1": 1, "defined": 0}


def test_masked_pixels_never_defined():
    spec = science.INDICES["ndvi"]
    q = garr(G10, np.array([[True, False, True]]), np.array([[True, False, True]]))
    io = compute_index(spec, refl("nir", "o", [0.4, 0.4, 0.4]), refl("red", "o", [0.1, 0.1, 0.1]), q)
    assert io.array.valid.tolist() == [[True, False, True]] and np.isnan(io.array.data[0, 1])


def test_wrong_band_mapping_and_mixed_observations_rejected():
    spec = science.INDICES["ndvi"]
    with pytest.raises(InvariantViolation):
        compute_index(spec, refl("red", "o", [0.1] * 3), refl("nir", "o", [0.4] * 3), ok())
    with pytest.raises(InvariantViolation) as e:
        compute_index(spec, refl("nir", "o1", [0.4] * 3), refl("red", "o2", [0.1] * 3), ok())
    assert e.value.code == "index_mixed_observations"


def test_index_on_wrong_grid_rejected():
    spec = science.INDICES["mndwi"]  # G20 index
    with pytest.raises(InvariantViolation):
        compute_index(spec, refl("green", "o", [0.1] * 3), refl("swir16", "o", [0.2] * 3), ok())


# --- I-18 strategy pin ---------------------------------------------------------------------


def _three_obs():
    # One pixel, three observations. Chosen so median(NDVI_i) ≠ NDVI(median(nir), median(red)).
    nir = [0.50, 0.20, 0.30]
    red = [0.05, 0.15, 0.20]
    return nir, red


def test_composite_is_median_of_per_observation_index_not_index_of_median_bands():
    nir, red = _three_obs()
    spec = science.INDICES["ndvi"]
    g = make_grid("G10", w=1, h=1)
    obs = [
        compute_index(spec, refl("nir", f"o{i}", [n], g), refl("red", f"o{i}", [r], g), ok(g, 1))
        for i, (n, r) in enumerate(zip(nir, red, strict=True))
    ]
    c = median_index_composite(obs, n_min=3)
    per_obs = sorted((n - r) / (n + r) for n, r in zip(nir, red, strict=True))
    index_first = per_obs[1]
    band_first = (np.median(nir) - np.median(red)) / (np.median(nir) + np.median(red))
    assert abs(index_first - band_first) > 0.05  # the fixture really discriminates
    assert c.array.data[0, 0] == pytest.approx(index_first, abs=1e-6)
    assert c.array.data[0, 0] != pytest.approx(band_first, abs=1e-3)


def test_composite_rejects_reflectance_and_display_inputs():
    g = make_grid("G10", w=1, h=1)
    with pytest.raises(InvariantViolation) as e:
        median_index_composite([refl("nir", "o", [0.3], g)], n_min=1)  # type: ignore[list-item]
    assert e.value.code == "composite_input_type"
    disp = display_rgb_composite(
        [(refl("red", "o", [0.1], g), refl("green", "o", [0.1], g), refl("blue", "o", [0.1], g))], [ok(g, 1)]
    )
    assert isinstance(disp, DisplayComposite) and disp.purpose == "display_rgb"
    with pytest.raises(InvariantViolation):
        zonal_stats(disp, np.ones((1, 1), bool))  # type: ignore[arg-type]


def test_composite_even_count_n_valid_and_n_min():
    spec = science.INDICES["ndvi"]
    nirs = [[0.4, 0.4, np.nan], [0.5, 0.4, 0.4], [0.6, np.nan, 0.4], [0.7, np.nan, 0.4]]
    obs = [compute_index(spec, refl("nir", f"o{i}", n), refl("red", f"o{i}", [0.1, 0.1, 0.1]), ok()) for i, n in enumerate(nirs)]
    c = median_index_composite(obs, n_min=3)
    assert c.n_valid.tolist() == [[4, 2, 3]] and c.n_valid.dtype == np.uint8
    vals = sorted((n - 0.1) / (n + 0.1) for n in (0.4, 0.5, 0.6, 0.7))
    assert c.array.data[0, 0] == pytest.approx((vals[1] + vals[2]) / 2, abs=1e-6)  # even → mean of middle
    assert not c.array.valid[0, 1] and np.isnan(c.array.data[0, 1])  # 2 < n_min: no value, not filled
    assert c.array.valid[0, 2]


def test_composite_rejects_duplicate_observation_and_mixed_grids():
    spec = science.INDICES["ndvi"]
    o = compute_index(spec, refl("nir", "o", [0.4] * 3), refl("red", "o", [0.1] * 3), ok())
    with pytest.raises(InvariantViolation):
        median_index_composite([o, o], n_min=1)
    other = make_grid("G10", w=3, h=1, x0=500000.0)
    o2 = compute_index(spec, refl("nir", "p", [0.4] * 3, other), refl("red", "p", [0.1] * 3, other), ok(other))
    with pytest.raises(InvariantViolation) as e:
        median_index_composite([o, o2], n_min=1)
    assert e.value.code == "grid_mismatch"


# --- statistics and coverage ---------------------------------------------------------------


def _composite(values, valid):
    from aether.geo.composite import IndexComposite

    g = make_grid("G10", w=len(values), h=1)
    return IndexComposite(
        "ndvi",
        garr(g, np.array([values], np.float32), np.array([valid])),
        np.zeros((1, len(values)), np.uint8),
        3,
        ("o",),
    )


def test_zonal_stats_values():
    c = _composite([0.1, 0.2, 0.3, 0.4, np.nan], [True, True, True, True, False])
    s = zonal_stats(c, np.ones((1, 5), bool))
    assert s.available and s.n_pixels == 4 and s.region_pixels == 5 and s.support_fraction == pytest.approx(0.8)
    assert s.mean == pytest.approx(0.25) and s.median == pytest.approx(0.25)
    assert s.p10 == pytest.approx(0.13) and s.p90 == pytest.approx(0.37)


def test_zonal_stats_insufficient_support_is_unavailable_not_zero():
    c = _composite([0.1, np.nan, np.nan, np.nan], [True, False, False, False])
    s = zonal_stats(c, np.ones((1, 4), bool))
    assert not s.available and s.unavailable_reason == "insufficient_support"
    assert s.mean is None and s.median is None


@pytest.mark.parametrize(
    ("usable", "cov", "outcome", "lims"),
    [
        (2, 1.0, "insufficient_observations", []),
        (3, 0.49, "insufficient_coverage", []),
        (3, 0.5, "completed", ["partial_coverage"]),
        (3, 0.79, "completed", ["partial_coverage"]),
        (3, 0.8, "completed", []),
        (5, None, "insufficient_coverage", []),
    ],
)
def test_snapshot_outcomes(usable, cov, outcome, lims):
    assert snapshot_outcome(usable, cov, 3) == (outcome, lims)


def test_coverage_denominators():
    in10 = np.array([[True, True, True, True, False]])
    in20 = np.array([[True, False]])
    n_valid = np.array([[5, 3, 1, 0, 9]], np.uint8)
    cov = snapshot_coverage(
        aoi_area_m2=410.0,
        pixel_area_true_m2=100.0,
        in_aoi_10=in10,
        in_aoi_20=in20,
        ndvi_n_valid=n_valid,
        observations_total=6,
        observations_usable=5,
        n_min=3,
    )
    assert cov.aoi_grid_px_10m == 4 and cov.aoi_grid_area_m2 == 400.0
    assert cov.observed_px == 3 and cov.valid_px == 2  # outside-AOI pixel never counts
    assert cov.coverage_a == pytest.approx(0.5)  # valid_area_a / aoi_grid_area (not aoi_area)
    assert cov.outcome == "completed" and cov.limitations == ["partial_coverage"]


def test_no_valid_support():
    in10 = np.ones((1, 4), bool)
    cov = snapshot_coverage(
        aoi_area_m2=400.0,
        pixel_area_true_m2=100.0,
        in_aoi_10=in10,
        in_aoi_20=np.ones((1, 2), bool),
        ndvi_n_valid=None,
        observations_total=0,
        observations_usable=0,
        n_min=3,
    )
    assert cov.valid_px == 0 and cov.coverage_a == 0.0 and cov.outcome == "insufficient_observations"
