"""Write analysis rasters as Cloud-Optimised GeoTIFFs on their analysis grid (storage design)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.shutil import copy as rio_copy

from aether.geo.grid import Grid


def write_cog(
    path: Path,
    grid: Grid,
    bands: Sequence[np.ndarray],
    *,
    dtype: str,
    nodata: float | int | None,
    band_names: Sequence[str],
    categorical: bool,
) -> dict[str, object]:
    """Write ``bands`` (each grid-shaped) to a DEFLATE COG (512 tiles). Returns raster metadata."""
    for b in bands:
        if b.shape != grid.shape:
            raise ValueError("band does not match grid")
    profile = {
        "driver": "GTiff",
        "width": grid.width,
        "height": grid.height,
        "count": len(bands),
        "dtype": dtype,
        "crs": f"EPSG:{grid.epsg}",
        "transform": grid.transform,
        "nodata": nodata,
    }
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            for i, (b, name) in enumerate(zip(bands, band_names, strict=True), start=1):
                dst.write(b.astype(dtype), i)
                dst.set_band_description(i, name)
        with mem.open() as src:
            rio_copy(
                src,
                str(path),
                driver="COG",
                compress="DEFLATE",
                blocksize=512,
                resampling="nearest" if categorical else "average",
                overview_resampling=("nearest" if categorical else "average"),
            )
    with rasterio.open(path) as chk:  # self-check: the written grid is the analysis grid
        if tuple(chk.transform)[:6] != tuple(grid.transform)[:6] or chk.crs.to_epsg() != grid.epsg:
            raise RuntimeError("COG georeferencing does not match the analysis grid")
    return {
        "crs_epsg": grid.epsg,
        "transform": list(tuple(grid.transform)[:6]),
        "width": grid.width,
        "height": grid.height,
        "dtype": dtype,
        "nodata": None if nodata is None else float(nodata),
        "resolution_m": float(grid.res),
        "band_names": list(band_names),
    }
