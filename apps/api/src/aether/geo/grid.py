"""Analysis grid (C3 §3–§8) and grid-tagged rasters (invariant I-19).

Every raster array in the engine travels as a :class:`GridArray`, tagged with the exact grid it
lives on. Operations combining rasters call :func:`require_same_grid`, so arithmetic between
rasters on different grids raises instead of silently broadcasting.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

import numpy as np
import shapely
from affine import Affine
from pyproj import Proj, Transformer
from rasterio.features import geometry_mask
from shapely.geometry import MultiPolygon

from aether import science
from aether.domain.errors import AetherError, InvariantViolation


@dataclass(frozen=True)
class Grid:
    """A north-up, pixel-is-area grid in one UTM CRS."""

    name: str  # "G10" | "G20"
    epsg: int
    res: int
    x0: float  # upper-left x
    y0: float  # upper-left y
    width: int
    height: int
    grid_hash: str  # shared by G10 and G20 of one analysis grid (C3 §3)

    @property
    def transform(self) -> Affine:
        return Affine(self.res, 0.0, self.x0, 0.0, -self.res, self.y0)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0 - self.height * self.res, self.x0 + self.width * self.res, self.y0)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    def identity(self) -> tuple[object, ...]:
        return (self.epsg, tuple(self.transform)[:6], self.width, self.height, self.res, self.grid_hash)

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "epsg": self.epsg,
            "res_m": self.res,
            "transform": list(tuple(self.transform)[:6]),
            "width": self.width,
            "height": self.height,
            "bounds": list(self.bounds),
            "grid_hash": self.grid_hash,
            "version": science.GRID_VERSION,
        }


@dataclass(frozen=True)
class AnalysisGrid:
    g10: Grid
    g20: Grid
    epsg: int
    grid_hash: str
    aoi_utm: MultiPolygon
    lon_c: float
    lat_c: float

    def by_name(self, name: str) -> Grid:
        return {"G10": self.g10, "G20": self.g20}[name]


def to_utm(geom_4326: MultiPolygon, epsg: int) -> MultiPolygon:
    tr = Transformer.from_crs(4326, epsg, always_xy=True)
    return shapely.transform(geom_4326, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))


def build_analysis_grid(aoi_4326: MultiPolygon, epsg: int) -> AnalysisGrid:
    """C3 §3: snap the buffered AOI bounds to the 20 m lattice in the AOI's UTM zone."""
    aoi_utm = to_utm(aoi_4326, epsg)
    minx, miny, maxx, maxy = aoi_utm.bounds
    b = science.GRID_BUFFER_M
    s = science.GRID_SNAP_M
    x0 = math.floor((minx - b) / s) * s
    y0 = math.ceil((maxy + b) / s) * s
    x1 = math.ceil((maxx + b) / s) * s
    y1 = math.floor((miny - b) / s) * s
    w10, h10 = (x1 - x0) // 10, (y0 - y1) // 10
    if w10 > science.GRID_MAX_PX_10M or h10 > science.GRID_MAX_PX_10M:
        raise AetherError(
            "aoi_extent_too_large",
            f"analysis grid {w10}×{h10} px exceeds {science.GRID_MAX_PX_10M}² at 10 m",
            details={"width": int(w10), "height": int(h10)},
        )
    gh = hashlib.sha256(f"{science.GRID_VERSION}|EPSG:{epsg}|{x0}|{y0}|{x1}|{y1}".encode()).hexdigest()
    g10 = Grid("G10", epsg, 10, float(x0), float(y0), int(w10), int(h10), gh)
    g20 = Grid("G20", epsg, 20, float(x0), float(y0), int(w10 // 2), int(h10 // 2), gh)
    c = aoi_4326.centroid
    return AnalysisGrid(g10, g20, epsg, gh, aoi_utm, c.x, c.y)


def in_aoi_mask(grid: Grid, aoi_utm: MultiPolygon) -> np.ndarray:
    """C3 §8: a pixel is in-AOI iff its centre lies inside the AOI (all_touched=False)."""
    return geometry_mask(
        [aoi_utm.__geo_interface__],
        out_shape=grid.shape,
        transform=grid.transform,
        all_touched=False,
        invert=True,
    )


def areal_scale(epsg: int, lon: float, lat: float) -> float:
    return float(Proj(f"EPSG:{epsg}").get_factors(lon, lat).areal_scale)


def pixel_area_true_m2(grid: Grid, lon_c: float, lat_c: float) -> float:
    """C3 §7: (Δx·Δy) / areal_scale at the AOI centroid."""
    return grid.res * grid.res / areal_scale(grid.epsg, lon_c, lat_c)


# --- grid-tagged rasters -------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class GridArray:
    """A 2-D raster tagged with its grid.

    ``data`` holds values (float32 with NaN where invalid, or integer classes); ``valid`` is the
    explicit validity mask (C2 §4: invalidity is never encoded by a sentinel number alone).
    """

    grid: Grid
    data: np.ndarray
    valid: np.ndarray

    def __post_init__(self) -> None:
        if self.data.shape != self.grid.shape or self.valid.shape != self.grid.shape:
            raise InvariantViolation(
                "grid_shape_mismatch",
                f"array shape {self.data.shape} does not match grid {self.grid.name} {self.grid.shape}",
            )
        if self.valid.dtype != np.bool_:
            raise InvariantViolation("grid_mask_dtype", "validity mask must be boolean")


def require_same_grid(*arrays: GridArray | Grid) -> Grid:
    """Raise ``grid_mismatch`` unless all arguments live on the identical grid (I-4, I-19)."""
    grids = [a if isinstance(a, Grid) else a.grid for a in arrays]
    first = grids[0]
    for g in grids[1:]:
        if g.identity() != first.identity():
            raise InvariantViolation(
                "grid_mismatch",
                "raster operation across different grids",
                details={"expected": first.describe(), "got": g.describe()},
            )
    return first
