"""Pixel-edge polygonisation with lossless validity repair (C5 §7 step 4/4a; Phase 2 freeze C-04)."""

from __future__ import annotations

import numpy as np
import pytest
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Polygon, box

from aether.domain.errors import InvariantViolation
from aether.geo.change import polygons
from aether.geo.change.morphology import label_components
from aether.geo.change.polygons import polygonize_components
from aether.geo.grid import GridArray
from tests.factories import make_grid

X0, Y0 = 600000.0, 4200000.0


def run(mask_rows: list[str]):
    m = np.array([[c == "X" for c in row] for row in mask_rows])
    g = make_grid("G10", m.shape[1], m.shape[0], x0=X0, y0=Y0)
    labels, n = label_components(GridArray(g, m, np.ones(m.shape, bool)), g)
    events, step = polygonize_components(labels, g)
    return m, labels.data, n, events, step, g


def pixel_union(m: np.ndarray, g) -> shapely.Geometry:
    boxes = [box(g.x0 + c * g.res, g.y0 - (r + 1) * g.res, g.x0 + (c + 1) * g.res, g.y0 - r * g.res) for r, c in np.argwhere(m)]
    return shapely.union_all(boxes)


def test_single_pixel_is_its_exact_pixel_square():
    _m, _, n, events, step, _g = run(["...", ".X.", "..."])
    assert n == 1 and len(events) == 1
    e = events[0]
    assert isinstance(e.geom_utm, Polygon) and e.geom_utm.is_valid and not e.repaired
    assert e.geom_utm.equals(box(X0 + 10, Y0 - 20, X0 + 20, Y0 - 10))
    assert e.geom_utm.area == 100.0 and e.pixel_count == 1
    assert step["connectivity"] == 8 and step["simplification"] is None and step["repair_count"] == 0


def test_adjacent_pixels_form_one_valid_polygon():
    _m, _, n, events, _, _g = run(["XX.", "...", "..."])
    assert n == 1
    e = events[0]
    assert isinstance(e.geom_utm, Polygon) and e.geom_utm.is_valid and not e.repaired
    assert e.geom_utm.equals(box(X0, Y0 - 10, X0 + 20, Y0)) and e.geom_utm.area == 200.0


def test_diagonal_pixels_are_one_event_repaired_into_a_multipolygon():
    m, _, n, events, step, g = run(["X.", ".X"])
    assert n == 1 and len(events) == 1  # identity is the 8-connected component
    e = events[0]
    assert e.repaired and step["repair_count"] == 1
    assert isinstance(e.geom_utm, MultiPolygon) and len(e.geom_utm.geoms) == 2
    assert e.geom_utm.is_valid and e.geom_utm.area == 200.0 == e.pixel_count * 100
    assert e.geom_utm.equals(pixel_union(m, g))


def test_ring_closed_diagonally_is_repaired_losslessly():
    # C-04 fixture: the ring is closed only diagonally, so the hole touches the shell twice and the
    # unrepaired polygon's interior is disconnected.
    rows = ["XXXX.", "X...X", "XXXX."]
    m, labels, n, events, _, g = run(rows)
    raw = polygons.shape(next(iter(polygons.shapes(labels, mask=labels > 0, connectivity=8, transform=g.transform)))[0])
    assert shapely.is_valid_reason(raw).startswith("Interior is disconnected")
    assert n == 1
    e = events[0]
    assert e.repaired and e.geom_utm.is_valid and isinstance(e.geom_utm, MultiPolygon)
    assert e.geom_utm.area == 1000.0 == e.pixel_count * 100
    assert e.geom_utm.equals(pixel_union(m, g))


def test_solid_shape_with_interior_hole_needs_no_repair():
    _m, _, _n, events, step, _ = run(["XXXX", "X..X", "X..X", "XXXX"])
    e = events[0]
    assert not e.repaired and step["repair_count"] == 0
    assert isinstance(e.geom_utm, Polygon) and len(e.geom_utm.interiors) == 1
    assert e.geom_utm.area == 1200.0


def test_exact_area_and_pixel_lattice_on_random_components():
    rng = np.random.default_rng(0)
    m = rng.random((40, 50)) > 0.55
    g = make_grid("G10", 50, 40, x0=X0, y0=Y0)
    labels_ga, n = label_components(GridArray(g, m, np.ones(m.shape, bool)), g)
    events, step = polygonize_components(labels_ga, g)
    labels = labels_ga.data
    assert [e.label for e in events] == list(range(1, n + 1))  # one event per label, in label order
    assert step["events"] == n and step["repair_count"] == sum(e.repaired for e in events) > 0
    for e in events:
        assert e.pixel_count == int((labels == e.label).sum())
        assert e.geom_utm.area == e.pixel_count * 100.0  # exact, not approximate
        # No simplification or buffering: the geometry is exactly the union of its pixel squares,
        # and every vertex lies on the 10 m pixel lattice.
        assert e.geom_utm.symmetric_difference(pixel_union(labels == e.label, g)).area == 0.0
        coords = shapely.get_coordinates(e.geom_utm)
        assert np.all((coords[:, 0] - X0) % 10 == 0) and np.all((Y0 - coords[:, 1]) % 10 == 0)


def test_geometry_is_revalidated_after_reprojection_to_4326():
    _m, _, _, events, _, g = run(["X..X", ".XX.", "X..X"])
    e = events[0]
    assert e.repaired and isinstance(e.geom_4326, type(e.geom_utm)) and e.geom_4326.is_valid
    back = Transformer.from_crs(4326, g.epsg, always_xy=True)
    roundtrip = shapely.transform(e.geom_4326, lambda xy: np.column_stack(back.transform(xy[:, 0], xy[:, 1])))
    assert roundtrip.symmetric_difference(e.geom_utm).area < 1e-6
    lon, lat = shapely.get_coordinates(e.geom_4326).mean(axis=0)
    assert -124 < lon < -120 and 37 < lat < 39  # EPSG:32610 near x = 600 km


def test_lossy_repair_fails_loudly(monkeypatch):
    monkeypatch.setattr(polygons.shapely, "make_valid", lambda g: g.buffer(0.5))
    with pytest.raises(InvariantViolation, match="polygon_validity_not_lossless") as exc:
        run(["X.", ".X"])
    assert exc.value.code == "polygon_validity_not_lossless" and exc.value.http_status == 500


def test_non_polygonal_repair_fails_loudly(monkeypatch):
    monkeypatch.setattr(polygons.shapely, "make_valid", lambda g: LineString([(0, 0), (1, 1)]))
    with pytest.raises(InvariantViolation, match="not Polygon/MultiPolygon"):
        run(["X.", ".X"])


def test_rejects_labels_not_on_the_supplied_grid():
    g = make_grid("G10", 3, 3)
    ones = np.ones((3, 3), bool)
    for other in (make_grid("G10", 3, 3, x0=600010.0), make_grid("G10", 3, 3, grid_hash="other"), make_grid("G10", 3, 2)):
        lab = GridArray(other, np.zeros(other.shape, np.int32), np.ones(other.shape, bool))
        with pytest.raises(InvariantViolation, match="grid_mismatch"):
            polygonize_components(lab, g)
    with pytest.raises(InvariantViolation, match="grid_mismatch"):
        polygonize_components(GridArray(g, np.zeros((3, 3), np.int64), ones), g)
    with pytest.raises(InvariantViolation, match="label_input"):
        polygonize_components(np.zeros((3, 3), np.int32), g)  # type: ignore[arg-type]
