"""Phase 1 gate: the engine on real Sentinel-2 windows matches an independent reference within 1e-6.

Fixtures: 4 real observations (tile 10SEH, June–July 2024), 6 assets each, 640 m square aligned to
the 20 m lattice (ADR-023, provenance sidecars). The AOI is the window inset by 210 m, so the
engine's own C3 §3 grid construction lands exactly on the fixture window.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from shapely.geometry import Polygon, mapping

from aether import science
from aether.geo.aoi import normalize_aoi
from aether.geo.grid import Grid, build_analysis_grid
from aether.geo.io.windows import RawWindow, read_window_from_dataset
from aether.geo.snapshot import run_snapshot
from aether.geo.stac.eligibility import apply_eligibility, parse_item
from tests.reference import reference_snapshot

TINY = Path(__file__).resolve().parents[1] / "fixtures" / "tiny"
ITEMS = [
    "S2B_T10SEH_20240607T185839_L2A",
    "S2B_T10SEH_20240617T190041_L2A",
    "S2B_T10SEH_20240627T185741_L2A",
    "S2B_T10SEH_20240707T185734_L2A",
]
TAG = "640m"


class FixtureReader:
    """Serves the fixture window of (item, asset) through the engine's own window logic."""

    def read(self, href: str, grid: Grid, fill: int, *, cache_key=None) -> RawWindow:
        _, item_id, key = cache_key
        path = TINY / "sentinel2" / f"{item_id}_{key}_{TAG}.tif"
        with rasterio.open(path) as src:
            data, meta = read_window_from_dataset(src, href, grid, fill)
        return RawWindow(data, meta, False, 0.0)


def _aoi_geojson() -> dict:
    with rasterio.open(TINY / "sentinel2" / f"{ITEMS[0]}_nir_{TAG}.tif") as src:
        left, bottom, right, top = src.bounds
        epsg = src.crs.to_epsg()
    inset = 210.0
    tr = Transformer.from_crs(epsg, 4326, always_xy=True)
    ring = [
        (left + inset, bottom + inset),
        (right - inset, bottom + inset),
        (right - inset, top - inset),
        (left + inset, top - inset),
        (left + inset, bottom + inset),
    ]
    return dict(mapping(Polygon([tr.transform(x, y) for x, y in ring])))


@pytest.fixture(scope="module")
def results():
    items = [json.loads((TINY / "stac" / f"{i}.json").read_text()) for i in ITEMS]
    aoi = normalize_aoi(_aoi_geojson())
    grid = build_analysis_grid(aoi.geometry, aoi.utm_epsg)
    with rasterio.open(TINY / "sentinel2" / f"{ITEMS[0]}_nir_{TAG}.tif") as src:
        assert (grid.g10.x0, grid.g10.y0, grid.g10.width) == (src.transform.c, src.transform.f, src.width)
    scenes = [parse_item(i) for i in items]
    apply_eligibility(scenes, grid.epsg)
    assert all(s.status == "used" for s in scenes), [s.reason_code for s in scenes]
    engine = run_snapshot(grid=grid, aoi_area_m2=aoi.area_m2, scenes=scenes, reader=FixtureReader())
    ref = reference_snapshot.run(TINY / "sentinel2", ITEMS, TAG, aoi.geojson())
    return engine, ref


@pytest.mark.parametrize("index", sorted(science.INDICES))
def test_engine_matches_independent_reference(results, index):
    engine, ref = results
    c = engine.composites[index]
    assert np.array_equal(c.n_valid, ref[index]["n_valid"])
    np.testing.assert_allclose(c.array.data, ref[index]["composite"], atol=1e-6, equal_nan=True)
    s, r = engine.stats[index], ref[index]
    assert s.n_pixels == r["n_pixels"] and s.support_fraction == pytest.approx(r["support"], abs=1e-12)
    if r["stats"] is None:
        assert not s.available
    else:
        for k in ("mean", "median", "p10", "p90"):
            assert getattr(s, k) == pytest.approx(r["stats"][k], abs=1e-6), k


def test_real_run_is_scientifically_plausible(results):
    engine, _ = results
    assert engine.coverage.outcome == "completed" and len(engine.observations_usable) == 4
    assert engine.stats["ndvi"].available
    # Even observation count (4) → median is the mean of the two middle values, on real data too.
    assert int(engine.composites["ndvi"].n_valid.max()) == 4
