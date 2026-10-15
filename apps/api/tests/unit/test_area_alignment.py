"""Area gate suite (C3 §7) and map-tile alignment (ADR-021 / evaluation plan Phase 1)."""

from __future__ import annotations

import math

import numpy as np
import pytest
from affine import Affine
from pyproj import Transformer
from shapely.geometry import Polygon, box

from aether.geo.aoi import geodesic_area_m2
from aether.geo.grid import Grid, pixel_area_true_m2


def _pixel_block_polygon_4326(grid: Grid, r0: int, c0: int, nr: int, nc: int) -> Polygon:
    """Pixel-edge polygon of a block, densified (1 vertex per pixel edge), reprojected to 4326."""
    t = grid.transform
    edge = [(c0 + i, r0) for i in range(nc)] + [(c0 + nc, r0 + j) for j in range(nr)]
    edge += [(c0 + nc - i, r0 + nr) for i in range(nc)] + [(c0, r0 + nr - j) for j in range(nr)]
    xy = [t @ (c, r) for c, r in edge]
    tr = Transformer.from_crs(grid.epsg, 4326, always_xy=True)
    return Polygon([tr.transform(x, y) for x, y in xy])


@pytest.mark.parametrize(
    ("epsg", "x0", "y0"), [(32610, 600000.0, 4270000.0), (32633, 400000.0, 7700000.0), (32755, 700000.0, 6100000.0)]
)
def test_pixel_count_area_matches_geodesic_polygon(epsg, x0, y0):
    """C3 §7 invariant: geodesic area of a pixel-edge polygon ≈ n_pixels × pixel_area_true (< 0.1 %)."""
    g = Grid("G10", epsg, 10, x0, y0, 400, 400, "h")
    poly = _pixel_block_polygon_4326(g, 50, 70, 300, 250)
    tr = Transformer.from_crs(epsg, 4326, always_xy=True)
    lon_c, lat_c = tr.transform(x0 + 70 * 10 + 1250, y0 - 50 * 10 - 1500)
    area_px = 300 * 250 * pixel_area_true_m2(g, lon_c, lat_c)
    assert abs(area_px - geodesic_area_m2(poly)) / geodesic_area_m2(poly) < 0.001


def test_planar_utm_area_would_be_wrong_away_from_central_meridian():
    """Guard that the areal-scale correction matters: raw planar UTM area deviates measurably."""
    g = Grid("G10", 32610, 10, 200000.0, 4270000.0, 100, 100, "h")  # far from the central meridian
    tr = Transformer.from_crs(32610, 4326, always_xy=True)
    lon, lat = tr.transform(200500.0, 4269500.0)
    assert abs(pixel_area_true_m2(g, lon, lat) - 100.0) > 0.05  # > 0.05 % difference


def test_tile_alignment_within_one_pixel(tmp_path):
    """A single marked pixel in a UTM COG must land at its true position in a z17 Web-Mercator tile."""
    import rasterio
    from rio_tiler.io import Reader

    from aether.geo.io.cog import write_cog

    g = Grid("G10", 32610, 10, 610000.0, 4266000.0, 100, 100, "h")
    arr = np.zeros(g.shape, np.float32)
    r, c = 37, 61
    arr[r, c] = 1.0
    path = tmp_path / "mark.tif"
    write_cog(path, g, [arr], dtype="float32", nodata=None, band_names=["mark"], categorical=True)
    x, y = g.transform @ (c + 0.5, r + 0.5)  # pixel centre in UTM
    lon, lat = Transformer.from_crs(32610, 4326, always_xy=True).transform(x, y)
    z = 17
    n = 2**z
    fx = (lon + 180) / 360 * n
    fy = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    tx, ty = int(fx), int(fy)
    with Reader(str(path)) as src:
        img = src.tile(tx, ty, z, tilesize=256, resampling_method="nearest")
    data = img.array.data[0]
    rows, cols = np.nonzero(data > 0.5)
    assert rows.size > 0
    py, px = rows.mean() + 0.5, cols.mean() + 0.5  # marked-pixel centroid in tile pixels
    m_per_px = 40075016.686 * math.cos(math.radians(lat)) / (n * 256)
    err_m = math.hypot((px / 256 - (fx - tx)) * 256 * m_per_px, (py / 256 - (fy - ty)) * 256 * m_per_px)
    assert err_m < 10.0, f"misplaced by {err_m:.2f} m"
    assert rasterio.open(path).transform == Affine(*tuple(g.transform)[:6])


def test_geodesic_area_unit_square_reference():
    a = geodesic_area_m2(box(0.0, 0.0, 0.01, 0.01))
    # Independent check: at the equator 0.01° of longitude = 1113.195 m (a·π/180·0.01) and 0.01° of
    # latitude = 1105.743 m (meridional radius of curvature M = a(1−e²)) → ≈ 1.230907e6 m².
    assert a == pytest.approx(1113.195 * 1105.743, rel=1e-4)
