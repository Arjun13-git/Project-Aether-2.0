"""Synthetic two-period worlds for change-run tests (programmatic, nothing committed; ADR-023, C-12).

The reader serves DN windows computed from world (UTM) coordinates, so every read of the same item
on the same grid is identical, and a B-period ``shift_px`` is a real image-content shift:
- NIR carries a smooth seeded texture anchored to a world lattice (co-registration signal);
- every band gets seeded per-item Gaussian noise (so robust MADs are non-zero);
- ``event_boxes`` raise red DN inside world rectangles (an NDVI drop: a veg_loss correlate);
- ``cloud_west_of`` sets SCL = 9 (cloud) west of a world x; ``cloud_boxes`` set SCL = 9 inside boxes.
"""

from __future__ import annotations

import threading
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from typing import Any

import numpy as np
import shapely
from pyproj import Transformer
from scipy import ndimage
from shapely.geometry import box, mapping

from aether import science
from aether.domain.periods import Period
from aether.geo.aoi import normalize_aoi
from aether.geo.io.windows import RawWindow, WindowMeta
from aether.geo.stac.client import SearchResult, build_request, sort_items
from tests.factories import stac_item

EPSG = 32610
CX, CY = 609_000.0, 4_266_000.0  # near Davis, CA (inside the factory item footprint)
LATTICE_X0, LATTICE_Y0, LATTICE_N = 600_000.0, 4_275_000.0, 1800  # 18 km texture lattice at 10 m

PERIOD_A = Period(date(2023, 6, 1), date(2023, 7, 15))
PERIOD_B = Period(date(2024, 6, 1), date(2024, 7, 15))

Box = tuple[float, float, float, float]  # minx, miny, maxx, maxy (UTM)


@cache
def _texture() -> np.ndarray:
    rng = np.random.default_rng(0)
    return ndimage.gaussian_filter(rng.normal(size=(LATTICE_N, LATTICE_N)), 2.0)


@dataclass(frozen=True)
class ItemWorld:
    shift_px: int = 0
    event_boxes: tuple[Box, ...] = ()
    cloud_west_of: float | None = None
    cloud_boxes: tuple[Box, ...] = ()


def aoi_record(side_m: float, cx: float = CX, cy: float = CY) -> dict[str, Any]:
    tr = Transformer.from_crs(EPSG, 4326, always_xy=True)
    utm = box(cx - side_m / 2, cy - side_m / 2, cx + side_m / 2, cy + side_m / 2)
    poly = shapely.transform(utm, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))
    n = normalize_aoi(dict(mapping(poly)))
    return {
        "id": uuid.UUID("00000000-0000-0000-0000-00000000c4a6"),
        "geometry_hash": n.geometry_hash,
        "geometry": dict(mapping(n.geometry)),
        "utm_epsg": n.utm_epsg,
    }


def items_for(period: str, n: int) -> list[dict[str, Any]]:
    year = "2023" if period == "a" else "2024"
    out = []
    for i in range(n):
        out.append(
            stac_item(
                f"S2B_10SFH_{year}06{10 + i:02d}_0_L2A",
                datatake=f"GS2B_{year}06{10 + i:02d}T184919_0379{i:02d}_N05.10",
                dt=f"{year}-06-{10 + i:02d}T18:58:21Z",
                generation_time=f"{year}-06-{10 + i:02d}T22:00:00.000000Z",
            )
        )
    return out


class FakeSearcher:
    """STAC searcher returning fixed items per period start date (no network)."""

    api_url = "https://earth-search.aws.element84.com/v1"

    def __init__(self, by_start: dict[date, list[dict[str, Any]]]) -> None:
        self.by_start = by_start
        self.calls: list[tuple[date, date]] = []

    def search(self, geometry: dict[str, Any], start: date, end: date, max_tile_cloud: float) -> SearchResult:
        self.calls.append((start, end))
        items = sort_items(self.by_start.get(start, []))
        req = build_request(science.STAC_COLLECTION, geometry, start, end, max_tile_cloud)
        return SearchResult(self.api_url, science.STAC_COLLECTION, req, items, len(items))


@dataclass
class WorldReader:
    worlds: dict[str, ItemWorld]
    noise_dn: float = 20.0
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def read(self, href, grid, fill, *, cache_key=None):
        gh, item_id, key = cache_key
        with self._lock:
            self.calls.append((gh, item_id, key))
        w = self.worlds.get(item_id, ItemWorld())
        xs = grid.x0 + (np.arange(grid.width) + 0.5) * grid.res
        ys = grid.y0 - (np.arange(grid.height) + 0.5) * grid.res
        x, y = np.meshgrid(xs, ys)
        if key == "scl":
            scl = np.full(grid.shape, 4, np.uint8)
            if w.cloud_west_of is not None:
                scl[x < w.cloud_west_of] = 9
            for bx in w.cloud_boxes:
                scl[(x >= bx[0]) & (x < bx[2]) & (y >= bx[1]) & (y < bx[3])] = 9
            data, dtype = scl, "uint8"
        else:
            base = {"red": 1500.0, "green": 1800.0, "blue": 1400.0, "nir": 3000.0, "swir16": 2500.0}[key]
            dn = np.full(grid.shape, base)
            if key == "nir":
                col = ((x - LATTICE_X0) // 10).astype(int) - w.shift_px
                row = ((LATTICE_Y0 - y) // 10).astype(int)
                dn = dn + 300.0 * _texture()[row, col]
            if key == "red":
                for bx in w.event_boxes:
                    dn[(x >= bx[0]) & (x < bx[2]) & (y >= bx[1]) & (y < bx[3])] = 2800.0
            seed = [zlib.crc32(item_id.encode()), science.REQUIRED_ASSETS.index(key)]
            dn = dn + np.random.default_rng(seed).normal(0.0, self.noise_dn, grid.shape)
            data, dtype = np.clip(np.rint(dn), 1, 65534).astype(np.uint16), "uint16"
        meta = WindowMeta(
            href, dtype, grid.epsg, tuple(grid.transform)[:6], (0, 0, grid.width, grid.height), 1.0, 0.0, 0.0, data.size
        )
        return RawWindow(data, meta, False, 0.0)


def world(
    *,
    n_a: int = 6,
    n_b: int = 6,
    b: ItemWorld | None = None,
    a: ItemWorld | None = None,
) -> tuple[FakeSearcher, WorldReader]:
    a, b = a or ItemWorld(), b or ItemWorld()
    ia, ib = items_for("a", n_a), items_for("b", n_b)
    worlds = {it["id"]: a for it in ia} | {it["id"]: b for it in ib}
    return FakeSearcher({PERIOD_A.start: ia, PERIOD_B.start: ib}), WorldReader(worlds)


def centred_box(half_m: float, cx: float = CX, cy: float = CY) -> Box:
    return (cx - half_m, cy - half_m, cx + half_m, cy + half_m)
