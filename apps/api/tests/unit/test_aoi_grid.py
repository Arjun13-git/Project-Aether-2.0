"""AOI admissibility (C3 §2) and analysis grid (C3 §3–§8, I-19)."""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Polygon, box, mapping

from aether.domain.errors import AetherError, InvariantViolation
from aether.geo.aoi import geodesic_area_m2, normalize_aoi, utm_zone_for_lon
from aether.geo.grid import (
    build_analysis_grid,
    in_aoi_mask,
    pixel_area_true_m2,
    require_same_grid,
    to_utm,
)
from tests.factories import AOI_GEOJSON, garr, make_grid


def gj(geom) -> dict:
    return dict(mapping(geom))


def test_valid_aoi_is_normalised_deterministically():
    a = normalize_aoi(AOI_GEOJSON)
    b = normalize_aoi(AOI_GEOJSON)
    assert a.geometry_hash == b.geometry_hash
    assert a.utm_epsg == 32610 and a.utm_zone == 10
    assert isinstance(a.geometry, MultiPolygon)
    assert a.geometry.geoms[0].exterior.is_ccw
    assert 1.4e6 < a.area_m2 < 1.8e6


def test_ring_orientation_does_not_change_hash():
    poly = box(-121.78, 38.53, -121.762, 38.54)
    reversed_poly = Polygon(list(poly.exterior.coords)[::-1])
    assert normalize_aoi(gj(poly)).geometry_hash == normalize_aoi(gj(reversed_poly)).geometry_hash


@pytest.mark.parametrize(
    ("geom", "code"),
    [
        ({"type": "Point", "coordinates": [0, 1]}, "invalid_geometry"),
        ({**AOI_GEOJSON, "crs": {"type": "name"}}, "invalid_geometry"),
        (gj(box(-121.0, 38.0, -119.0, 39.0)), "aoi_too_large"),
        (gj(box(-120.01, 38.5, -119.99, 38.51)), "aoi_crosses_utm_zone"),
        (gj(box(10.0, -0.01, 10.02, 0.01)), "crosses_equator"),
        (gj(box(5.0, 60.0, 5.02, 60.01)), "utm_exception_region"),
        (gj(box(20.0, 80.5, 20.02, 80.6)), "latitude_out_of_range"),
        (gj(box(-121.78, 38.53, 121.0, 38.54)), "aoi_too_large"),
    ],
)
def test_inadmissible_aois_are_rejected_with_codes(geom, code):
    with pytest.raises(AetherError) as e:
        normalize_aoi(geom)
    assert e.value.code == code


def test_too_many_vertices():
    import shapely

    circle = shapely.Point(-121.77, 38.535).buffer(0.005, quad_segs=300)
    with pytest.raises(AetherError) as e:
        normalize_aoi(gj(circle))
    assert e.value.code == "too_many_vertices"


def test_bowtie_rejected_not_silently_repaired():
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[-121.78, 38.53], [-121.76, 38.54], [-121.76, 38.53], [-121.78, 38.54], [-121.78, 38.53]]],
    }
    with pytest.raises(AetherError) as e:
        normalize_aoi(bowtie)
    assert e.value.code == "invalid_geometry"


def test_small_self_touch_repair_is_recorded():
    # Exterior ring touching itself at one vertex: valid area, invalid topology.
    ring = [[-121.78, 38.53], [-121.76, 38.53], [-121.76, 38.54], [-121.77, 38.53], [-121.78, 38.54], [-121.78, 38.53]]
    aoi = normalize_aoi({"type": "Polygon", "coordinates": [ring]})
    assert any(op.startswith("make_valid") for op in aoi.operations)


def test_zone_edges():
    assert utm_zone_for_lon(-120.000001) == 10
    assert utm_zone_for_lon(-119.999999) == 11
    assert utm_zone_for_lon(180.0) == 60
    assert utm_zone_for_lon(-180.0) == 1


def test_southern_hemisphere_epsg():
    aoi = normalize_aoi(gj(box(149.10, -35.30, 149.12, -35.29)))
    assert aoi.utm_epsg == 32755


def test_grid_snapping_and_nesting():
    aoi = normalize_aoi(AOI_GEOJSON)
    g = build_analysis_grid(aoi.geometry, aoi.utm_epsg)
    for grid in (g.g10, g.g20):
        assert grid.x0 % 20 == 0 and grid.y0 % 20 == 0
    assert g.g10.width == 2 * g.g20.width and g.g10.height == 2 * g.g20.height
    minx, _miny, _maxx, maxy = to_utm(aoi.geometry, aoi.utm_epsg).bounds
    assert g.g10.x0 <= minx - 200 and g.g10.y0 >= maxy + 200
    # deterministic hash
    assert g.grid_hash == build_analysis_grid(aoi.geometry, aoi.utm_epsg).grid_hash


def test_grid_too_large_rejected():
    # 0.24° wide strip at 38.5° N ≈ 21 km → > 2000 px at 10 m but area < 100 km².
    aoi = normalize_aoi(gj(box(-121.95, 38.53, -121.71, 38.5337)))
    with pytest.raises(AetherError) as e:
        build_analysis_grid(aoi.geometry, aoi.utm_epsg)
    assert e.value.code == "aoi_extent_too_large"


def test_pixel_centre_membership_and_area_agree_with_geodesic():
    aoi = normalize_aoi(AOI_GEOJSON)
    g = build_analysis_grid(aoi.geometry, aoi.utm_epsg)
    m = in_aoi_mask(g.g10, g.aoi_utm)
    pa = pixel_area_true_m2(g.g10, g.lon_c, g.lat_c)
    raster_area = m.sum() * pa
    # Boundary-pixel effect only: within 2 % for a ~1.6 km² AOI.
    assert abs(raster_area - aoi.area_m2) / aoi.area_m2 < 0.02
    assert 99.0 < pa < 101.0


def test_geodesic_area_of_1km_square_at_latitudes():
    for lat in (0.5, 45.0, 70.0):
        dlat = 1000.0 / 111_320.0
        import math

        dlon = 1000.0 / (111_320.0 * math.cos(math.radians(lat)))
        a = geodesic_area_m2(box(10.0, lat, 10.0 + dlon, lat + dlat))
        assert abs(a - 1e6) / 1e6 < 0.01


@pytest.mark.parametrize(
    "other",
    [
        make_grid(epsg=32611),
        make_grid(x0=600010.0),
        make_grid(w=9),
        make_grid(name="G20"),
        make_grid(grid_hash="other"),
    ],
)
def test_grid_mismatch_is_detected(other):
    a = make_grid()
    with pytest.raises(InvariantViolation) as e:
        require_same_grid(garr(a, np.zeros(a.shape, np.float32)), garr(other, np.zeros(other.shape, np.float32)))
    assert e.value.code == "grid_mismatch"


def test_identical_grids_compare_equal():
    a, b = make_grid(), make_grid()
    assert require_same_grid(a, b) == a


def test_gridarray_rejects_wrong_shape():
    g = make_grid()
    with pytest.raises(InvariantViolation):
        garr(g, np.zeros((2, 2), np.float32))
