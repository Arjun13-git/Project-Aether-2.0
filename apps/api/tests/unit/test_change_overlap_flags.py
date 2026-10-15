"""C-17 cross-category overlap and C-18 event-level ``index_flags`` (Phase 2 Stage 4 contract resolution)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import shapely
from pyproj import Transformer
from scipy import ndimage
from shapely.geometry import box

from aether import science
from aether.geo.change.delta import IndexDelta
from aether.geo.change.events import extract_events
from aether.geo.change.joint import JointValid
from aether.geo.change.morphology import SQUARE_3X3, clean_mask, label_components
from aether.geo.change.polygons import polygonize_components
from aether.geo.change.robust import RobustReference
from aether.geo.composite import IndexComposite
from aether.geo.coreg import CoregResult, CoregSupport
from aether.geo.grid import GridArray, pixel_area_true_m2
from tests.factories import make_grid

# Verified adversarial fixture (Stage 4 contract analysis): pixel (8, 3) is claimed by both closings.
FIXTURE_14 = [
    "00000000000000",
    "02220000000000",
    "02220000000000",
    "02220000000000",
    "01110000000000",
    "01110000000000",
    "01112220000000",
    "00002220000000",
    "22202220000000",
    "22211110000000",
    "22211110000000",
    "00111112220000",
    "00000002220000",
    "00000002220000",
]
OVERLAP_14 = (8, 3)

# MMU-surviving fixture (40 × 40, rectangles painted in order): one overlap pixel at (26, 5).
RECTS_40 = [(7, 12, 9, 7, 1), (23, 8, 8, 10, 2), (12, 20, 11, 4, 2), (21, 3, 11, 9, 2), (19, 6, 8, 7, 1), (26, 1, 3, 4, 1)]
OVERLAP_40 = (26, 5)
CODE = {1: 4, 2: 5}  # fixture value → category code (veg_loss, veg_gain)


def _px_box(grid, r: int, c: int):
    return box(grid.x0 + c * grid.res, grid.y0 - (r + 1) * grid.res, grid.x0 + (c + 1) * grid.res, grid.y0 - r * grid.res)


def _joint(g, mask=None):
    m = np.ones(g.shape, bool) if mask is None else mask
    return JointValid(g, m, int(m.sum()), int(m.sum()), ())


# --- C-17 at morphology level (the verified fixture, below the MMU) -------------------------------


def test_verified_overlap_pixel_is_in_both_label_rasters_and_neither_opened_mask():
    lab = np.array([[int(ch) for ch in row] for row in FIXTURE_14])
    g = make_grid("G10", 14, 14)
    joint = _joint(g)
    labels, opened = {}, {}
    for v in (1, 2):
        m = lab == v
        opened[v] = ndimage.binary_opening(m, structure=SQUARE_3X3)
        cleaned = clean_mask(GridArray(g, m, np.ones(g.shape, bool)), joint)
        labels[v], _ = label_components(cleaned, g)
    r, c = OVERLAP_14
    assert labels[1].data[r, c] > 0 and labels[2].data[r, c] > 0
    assert not opened[1][r, c] and not opened[2][r, c]  # a gap fill of both closings
    assert lab[r, c] == 0
    overlap = (labels[1].data > 0) & (labels[2].data > 0)
    assert np.argwhere(overlap).tolist() == [list(OVERLAP_14)]

    geoms = {}
    for v in (1, 2):
        events, _ = polygonize_components(labels[v], g)
        (geoms[v],) = [e.geom_utm for e in events if e.label == labels[v].data[r, c]]
        assert geoms[v].is_valid
    inter = geoms[1].intersection(geoms[2])
    polygonal = shapely.union_all([p for p in shapely.get_parts(inter) if p.geom_type in ("Polygon", "MultiPolygon")])
    assert polygonal.equals(_px_box(g, r, c)) and inter.area == 100.0


# --- C-17 / C-18 end to end through extract_events (MMU-surviving fixture) -----------------------


def _signs(cat: np.ndarray) -> dict[str, np.ndarray]:
    """Per-pixel s_I consistent with the categories: veg_loss ↔ s_NDVI = −1, veg_gain ↔ +1."""
    s_ndvi = np.where(cat == 4, -1, np.where(cat == 5, 1, 0)).astype(np.int8)
    s_ndbi = np.zeros(cat.shape, np.int8)
    s_ndbi[cat == 4] = 1  # NDBI rises over part of the loss area (a mixed-sign index for C-18)
    s_ndbi[:24, :] = np.where(cat[:24, :] == 4, 0, s_ndbi[:24, :])
    return {"ndvi": s_ndvi, "mndwi": np.zeros(cat.shape, np.int8), "ndbi": s_ndbi}


def _extract(cat: np.ndarray, sig: dict[str, np.ndarray] | None = None):
    h, w = cat.shape
    g = make_grid("G10", w, h)
    lon, lat = Transformer.from_crs(g.epsg, 4326, always_xy=True).transform(g.x0, g.y0)
    area = pixel_area_true_m2(g, lon, lat)
    joint = _joint(g)
    ones = np.ones(g.shape, bool)
    d = {
        n: IndexDelta(n, GridArray(g, np.zeros(g.shape), ones), GridArray(g, np.zeros(g.shape), ones), ())
        for n in science.CHANGE_INDICES
    }
    comp = IndexComposite("ndvi", GridArray(g, np.zeros(g.shape, np.float32), ones), np.full(g.shape, 6, np.uint8), 3, ("o",))
    support = CoregSupport(1, 1, 1.0, (0, 1, 0, 1), True, None)
    res = extract_events(
        categories=GridArray(g, cat.astype(np.uint8), ones),
        joint=joint,
        deltas=d,
        sig=_signs(cat) if sig is None else sig,
        ref=RobustReference("absolute_only", ("robust_reference_unavailable",), int(ones.sum()), {}, {}, None, None),
        ndvi_a=comp,
        ndvi_b=comp,
        in_aoi10=GridArray(g, ones, ones),
        coreg=CoregResult("ok", support, residual_px=0.0),
        pixel_area_true_m2=area,
    )
    return g, res, area


def fixture_40() -> np.ndarray:
    cat = np.zeros((40, 40), np.uint8)
    for r, c, h, w, v in RECTS_40:
        cat[r : r + h, c : c + w] = CODE[v]
    return cat


@pytest.fixture(scope="module")
def run40():
    cat = fixture_40()
    g, res, area = _extract(cat)
    return cat, g, res, area


def test_overlap_survives_mmu_with_identity_and_global_labels(run40):
    cat, g, res, area = run40
    r, c = OVERLAP_40
    bands = {lay.code: res.event_label_bands[i].data for i, lay in enumerate(res.layers)}
    assert [lay.code for lay in res.layers] == [1, 2, 3, 4, 5, 6] and len(res.event_label_bands) == 6
    assert all(b.data.dtype == np.int32 for b in res.event_label_bands)
    loss_label, gain_label = bands[4][r, c], bands[5][r, c]
    assert loss_label > 0 and gain_label > 0  # one pixel in events of two categories
    for code in (4, 5):  # never kept by either opening; both closings restored/filled it
        assert not ndimage.binary_opening(cat == code, structure=SQUARE_3X3)[r, c]
    assert cat[r, c] == 5  # its own first-match code may be one of the two categories (C-17)
    # Identity (category_code, label); global labels 1..N in that order.
    keys = [(e.category_code, e.label) for e in res.events]
    assert keys == sorted(keys) and [e.event_label for e in res.events] == list(range(1, len(res.events) + 1))
    by_global = {e.event_label: e for e in res.events}
    loss, gain = by_global[int(loss_label)], by_global[int(gain_label)]
    assert (loss.category, gain.category) == ("veg_loss", "veg_gain")
    assert loss.pixel_count >= res.mmu_min_px and gain.pixel_count >= res.mmu_min_px
    # Each event's own area includes the shared pixel.
    assert loss.pixel_count == int((bands[4] == loss_label).sum()) and loss.area_m2 == loss.pixel_count * area
    # Overlapping geometries: valid; their polygonal intersection is exactly the shared pixel square.
    assert loss.geometry.geom_utm.is_valid and gain.geometry.geom_utm.is_valid
    inter = loss.geometry.geom_utm.intersection(gain.geometry.geom_utm)
    polygonal = shapely.union_all([p for p in shapely.get_parts(inter) if p.geom_type in ("Polygon", "MultiPolygon")])
    assert polygonal.equals(_px_box(g, r, c))


def test_changed_area_is_a_union_and_category_sum_exceeds_it_by_the_overlap(run40):
    _, _, res, area = run40
    member = np.stack([b.data > 0 for b in res.event_label_bands])
    union_px = int(member.any(axis=0).sum())
    overlap_px = int((member.sum(axis=0) > 1).sum())
    assert overlap_px == 1
    assert res.changed_px == union_px and res.changed_area_m2 == union_px * area
    for i, lay in enumerate(res.layers):
        assert res.changed_px_by_category[lay.category] == int(member[i].sum())
    total_by_cat = sum(res.changed_area_category_m2(lay.category) for lay in res.layers)
    assert total_by_cat - res.changed_area_m2 == pytest.approx(overlap_px * area, rel=0, abs=1e-6)
    assert res.overlap_px == overlap_px


# --- C-18 -----------------------------------------------------------------------------------------


def _direct_counts(sig, px):
    return {n: (int((sig[n][px] == 1).sum()), int((sig[n][px] == -1).sum()), int((sig[n][px] == 0).sum())) for n in sig}


def test_flag_counts_match_direct_numpy_counts_for_every_event(run40):
    cat, _, res, _ = run40
    sig = _signs(cat)
    for e in res.events:
        band = res.event_label_bands[e.category_code - 1].data
        px = band == e.event_label
        direct = _direct_counts(sig, px)
        for n in science.CHANGE_INDICES:
            pos, neg = e.flag_pixels[n]["positive"], e.flag_pixels[n]["negative"]
            assert (pos, neg, e.flag_zero_pixels(n)) == direct[n]
            assert pos + neg + e.flag_zero_pixels(n) == e.pixel_count
            expected = [s for s, v in (("-", neg), ("0", e.flag_zero_pixels(n)), ("+", pos)) if v > 0]
            assert e.index_flags[n] == expected and e.index_flags[n]  # never empty


def _blocks():
    cat = np.zeros((40, 40), np.uint8)
    cat[2:12, 2:12] = 4  # veg_loss, NDVI-only negative
    cat[2:12, 20:30] = 6  # other_significant: mixed MNDWI signs + NDBI negative
    cat[25:35, 10:20] = 5  # veg_gain block with all-zero signs (unit fixture for the zero case)
    sig = {n: np.zeros(cat.shape, np.int8) for n in science.CHANGE_INDICES}
    sig["ndvi"][2:12, 2:12] = -1
    sig["mndwi"][2:6, 20:30] = 1
    sig["mndwi"][6:9, 20:30] = -1
    sig["ndbi"][9:12, 20:30] = -1
    return cat, sig


def test_single_index_mixed_and_all_zero_events():
    cat, sig = _blocks()
    _, res, _ = _extract(cat, sig)
    loss, zero, mixed = res.events  # ordered by category code: veg_loss (4), veg_gain (5), other_significant (6)
    assert (loss.category, zero.category, mixed.category) == ("veg_loss", "veg_gain", "other_significant")
    # NDVI-only negative.
    assert loss.flag_pixels["ndvi"] == {"positive": 0, "negative": 100} and loss.flag_zero_pixels("ndvi") == 0
    assert loss.index_flags == {"ndvi": ["-"], "mndwi": ["0"], "ndbi": ["0"]}
    # Mixed: exact per-index positive/negative/zero counts.
    assert mixed.flag_pixels["mndwi"] == {"positive": 40, "negative": 30} and mixed.flag_zero_pixels("mndwi") == 30
    assert mixed.flag_pixels["ndbi"] == {"positive": 0, "negative": 30} and mixed.flag_zero_pixels("ndbi") == 70
    assert mixed.index_flags == {"ndvi": ["0"], "mndwi": ["-", "0", "+"], "ndbi": ["-", "0"]}
    # All zero: counts 0, every index reads ["0"] (C-18: a list is never empty).
    assert all(zero.flag_pixels[n] == {"positive": 0, "negative": 0} for n in science.CHANGE_INDICES)
    assert zero.index_flags == {n: ["0"] for n in science.CHANGE_INDICES}
    assert list(loss.index_flags) == list(science.CHANGE_INDICES)


# --- determinism across hash seeds ----------------------------------------------------------------


def fingerprint() -> str:
    """JSON fingerprint of the C-17/C-18 outputs (used in a subprocess under different hash seeds)."""
    out = []
    for cat, sig in ((fixture_40(), None), _blocks()):
        _, res, _ = _extract(cat, sig)
        out.append(
            {
                "events": [
                    [
                        e.category_code,
                        e.label,
                        e.event_label,
                        e.pixel_count,
                        e.flag_pixels,
                        e.index_flags,
                        e.geometry.geom_utm.wkb_hex,
                    ]
                    for e in res.events
                ],
                "bands": [hashlib.sha256(b.data.tobytes()).hexdigest() for b in res.event_label_bands],
                "changed": [res.changed_px, res.changed_px_by_category],
            }
        )
    return json.dumps(out, sort_keys=True)


def test_outputs_are_identical_under_different_hash_seeds():
    root = Path(__file__).resolve().parents[2]
    outs = []
    for seed in ("0", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        cmd = [sys.executable, "-c", "from tests.unit.test_change_overlap_flags import fingerprint; print(fingerprint())"]
        outs.append(subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, check=True).stdout)
    assert outs[0] == outs[1] and outs[0].strip() == fingerprint()
