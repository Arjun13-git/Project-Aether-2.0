"""Pixel-edge polygonisation with lossless validity repair (C5 §7 step 4/4a; Phase 2 freeze C-04).

Per component label, in the analysis UTM CRS:
1. ``rasterio.features.shapes`` with 8-connectivity → exact pixel-edge rings (no simplification,
   no buffering, no clipping to the AOI);
2. validate; ``make_valid`` only where invalid (diagonal touches give self-touching rings);
3. accept only Polygon / MultiPolygon;
4. assert the planar area equals ``pixel_count × res²`` exactly. This is the same pixel set as the
   ``pixel_count × pixel_area_true_m2`` event area, which always comes from the pixel count;
5. reproject the vertices to EPSG:4326 and re-validate.

Any violation raises ``polygon_validity_not_lossless`` and fails the job. A MultiPolygon from a
lossless repair is still one event: identity is the 8-connected component, not the geometry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import shapely
from pyproj import Transformer
from rasterio.features import shapes
from shapely.geometry import MultiPolygon, Polygon, shape

from aether.domain.errors import InvariantViolation
from aether.geo.grid import Grid, GridArray, require_same_grid


@dataclass(frozen=True)
class EventGeometry:
    label: int
    pixel_count: int
    geom_utm: Polygon | MultiPolygon
    geom_4326: Polygon | MultiPolygon
    repaired: bool


def _fail(label: int, why: str, **details: Any) -> InvariantViolation:
    return InvariantViolation("polygon_validity_not_lossless", f"event label {label}: {why}", {"label": label, **details})


def _accepted(label: int, g: shapely.Geometry) -> Polygon | MultiPolygon:
    if isinstance(g, Polygon | MultiPolygon) and not g.is_empty:
        return g
    raise _fail(label, f"geometry type {g.geom_type} is not Polygon/MultiPolygon")


def polygonize_components(labels_ga: GridArray, grid: Grid) -> tuple[list[EventGeometry], dict[str, Any]]:
    """Polygonise components 1..n of ``labels_ga`` (int32 on ``grid``, I-19); geometries in label order."""
    if not isinstance(labels_ga, GridArray):
        raise InvariantViolation("label_input", "polygonisation expects a GridArray label raster")
    require_same_grid(labels_ga, grid)
    labels = labels_ga.data
    if labels.shape != grid.shape or labels.dtype != np.int32:
        raise InvariantViolation("grid_mismatch", "label raster must be int32 on the analysis grid")
    counts = np.bincount(labels.ravel())
    present = [int(v) for v in np.flatnonzero(counts) if v != 0]
    parts: dict[int, list[Polygon]] = {v: [] for v in present}
    for geom, value in shapes(labels, mask=labels > 0, connectivity=8, transform=grid.transform):
        parts[int(value)].append(shape(geom))

    to_4326 = Transformer.from_crs(grid.epsg, 4326, always_xy=True)
    out: list[EventGeometry] = []
    repairs = 0
    for label in present:
        n_px = int(counts[label])
        polys = parts[label]
        geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)
        repaired = False
        if not geom.is_valid:
            geom = shapely.make_valid(geom)
            repaired = True
            repairs += 1
        utm = _accepted(label, geom)
        if not utm.is_valid:
            raise _fail(label, "geometry still invalid after make_valid", reason=shapely.is_valid_reason(utm))
        expected = n_px * grid.res * grid.res
        if utm.area != expected:
            raise _fail(label, f"planar area {utm.area} != pixel_count × res² = {expected}", pixel_count=n_px)
        wgs = shapely.transform(utm, lambda xy: np.column_stack(to_4326.transform(xy[:, 0], xy[:, 1])))
        wgs = _accepted(label, wgs)
        if not wgs.is_valid:
            raise _fail(label, "geometry invalid after reprojection to EPSG:4326", reason=shapely.is_valid_reason(wgs))
        out.append(EventGeometry(label, n_px, utm, wgs, repaired))

    step = {
        "kind": "polygonization",
        "method": "rasterio.features.shapes",
        "connectivity": 8,
        "geometry": "pixel_edges",
        "crs": f"EPSG:{grid.epsg}",
        "simplification": None,
        "validity_rule": "make_valid if invalid; Polygon/MultiPolygon only; planar area == pixel_count × res² exactly",
        "events": len(out),
        "repair_count": repairs,
    }
    return out, step
