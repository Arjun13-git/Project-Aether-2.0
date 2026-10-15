"""Synthetic test inputs generated in code (ADR-023: synthetic first). No real imagery here."""

from __future__ import annotations

import copy
from typing import Any

import numpy as np
from shapely.geometry import box, mapping

from aether import science
from aether.geo.grid import Grid, GridArray
from aether.geo.io.windows import ItemExcluded, RawWindow, WindowMeta

PREFIX = science.ASSET_HREF_PREFIX

REFL_BANDS = [{"data_type": "uint16", "scale": 0.0001, "offset": -0.1, "nodata": 0}]
SCL_BANDS = [{"data_type": "uint8", "nodata": 0}]

# A small AOI (~1.5 km × 1.1 km) near Davis, California (UTM 10N), used across tests.
AOI_BOX = box(-121.78, 38.53, -121.762, 38.54)
AOI_GEOJSON: dict[str, Any] = dict(mapping(AOI_BOX))


def make_grid(
    name: str = "G10",
    w: int = 8,
    h: int = 6,
    epsg: int = 32610,
    x0: float = 600000.0,
    y0: float = 4200000.0,
    grid_hash: str = "h",
) -> Grid:
    res = 10 if name == "G10" else 20
    return Grid(name, epsg, res, x0, y0, w, h, grid_hash)


def garr(grid: Grid, data: np.ndarray, valid: np.ndarray | None = None) -> GridArray:
    data = np.asarray(data)
    if valid is None:
        valid = ~np.isnan(data) if data.dtype.kind == "f" else np.ones(data.shape, bool)
    return GridArray(grid, data, valid)


def stac_item(
    item_id: str = "S2B_10SFH_20240615_0_L2A",
    *,
    datatake: str = "GS2B_20240615T184919_037983_N05.10",
    baseline: str = "05.10",
    epsg: int = 32610,
    dt: str = "2024-06-15T18:58:21.024000Z",
    grid_code: str = "MGRS-10SFH",
    generation_time: str = "2024-06-15T22:00:00.000000Z",
    cloud: float = 5.0,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tile = grid_code.split("-")[-1]
    assets: dict[str, Any] = {}
    for key in science.REQUIRED_ASSETS:
        assets[key] = {
            "href": f"{PREFIX}{tile[:2]}/{tile[2]}/{tile[3:]}/2024/6/{item_id}/{key.upper()}.tif",
            "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            "raster:bands": copy.deepcopy(SCL_BANDS if key == "scl" else REFL_BANDS),
            "file:checksum": "1220" + "ab" * 32,
            "file:size": 1000,
        }
    item = {
        "type": "Feature",
        "stac_version": "1.1.0",
        "id": item_id,
        "collection": science.STAC_COLLECTION,
        "geometry": dict(mapping(box(-122.5, 37.9, -121.2, 38.9))),
        "bbox": [-122.5, 37.9, -121.2, 38.9],
        "properties": {
            "datetime": dt,
            "platform": "sentinel-2b",
            "s2:processing_baseline": baseline,
            "s2:datatake_id": datatake,
            "s2:generation_time": generation_time,
            "grid:code": grid_code,
            "proj:code": f"EPSG:{epsg}",
            "eo:cloud_cover": cloud,
            "view:sun_elevation": 70.1,
            "view:sun_azimuth": 140.2,
            "updated": "2024-06-16T00:00:00Z",
        },
        "assets": assets,
        "links": [],
    }
    if overrides:
        for path, value in overrides.items():
            target = item
            keys = path.split(".")
            for k in keys[:-1]:
                target = target[k]
            if value is None:
                target.pop(keys[-1], None)
            else:
                target[keys[-1]] = value
    return item


class SyntheticReader:
    """Serves synthetic DN windows; ``cloudy`` item IDs get SCL = 9 everywhere."""

    def __init__(self, nir_dn: dict[str, int], cloudy: set[str] = frozenset(), unreadable: set[str] = frozenset()):
        self.nir_dn, self.cloudy, self.unreadable = nir_dn, cloudy, unreadable

    def read(self, href, grid, fill, *, cache_key=None):
        _, item_id, key = cache_key
        if item_id in self.unreadable and key == "swir16":
            raise ItemExcluded("asset_unreadable", href)
        if key == "scl":
            data = np.full(grid.shape, 9 if item_id in self.cloudy else 4, np.uint8)
            dtype = "uint8"
        else:
            dn = {"red": 1500, "green": 1800, "blue": 1400, "nir": self.nir_dn[item_id], "swir16": 2500}[key]
            data = np.full(grid.shape, dn, np.uint16)
            dtype = "uint16"
        meta = WindowMeta(
            href,
            dtype,
            grid.epsg,
            tuple(grid.transform)[:6],
            (0, 0, grid.width, grid.height),
            1.0,
            0.0,
            0.0,
            grid.width * grid.height,
        )
        return RawWindow(data, meta, False, 0.0)
