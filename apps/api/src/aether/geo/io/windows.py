"""AOI-windowed COG reads onto the analysis grid (C1 §8, C3 §4–§5, Security §4).

Only the grid window is fetched (HTTP range requests through GDAL /vsicurl/). No resampling
happens at read time: the source must share the grid CRS and pixel lattice exactly, otherwise
the item is excluded. Pixels of the window outside the source extent are filled with nodata.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import rasterio
from rasterio.errors import RasterioError
from rasterio.windows import Window

from aether import science
from aether.geo.grid import Grid

log = logging.getLogger(__name__)

# Security design §4 — GDAL hardening for remote reads.
GDAL_REMOTE_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "GDAL_HTTP_TIMEOUT": "30",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "VSI_CACHE": "TRUE",
}


class ItemExcluded(Exception):
    """Raised when an item fails a read-time rule. ``reason`` is a C1/C2/C3 reason code.

    ``attempts`` is filled only for ``asset_unreadable``: one entry per failed transport attempt
    (error class + underlying GDAL/HTTP message), so the exclusion is explainable in provenance.
    """

    def __init__(self, reason: str, message: str = "", attempts: list[dict[str, str]] | None = None) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.attempts = attempts or []


def error_chain(exc: BaseException, limit: int = 400) -> str:
    """Flatten an exception and its causes. rasterio's "Read failed. See previous exception" keeps
    the actual GDAL/curl error (e.g. HTTP status, timeout) in ``__cause__``/``__context__``."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(parts) < 4:
        seen.add(id(cur))
        parts.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    return " <- ".join(parts)[:limit]


@dataclass(frozen=True)
class WindowMeta:
    href: str
    dtype: str
    crs_epsg: int
    source_transform: tuple[float, ...]
    window: tuple[int, int, int, int]  # col_off, row_off, width, height in source pixels
    header_scale: float
    header_offset: float
    header_nodata: float | None
    bytes_read_px: int  # source pixels actually read (intersection)


@dataclass(frozen=True)
class RawWindow:
    data: np.ndarray  # source dtype, grid shape
    meta: WindowMeta
    from_cache: bool
    seconds: float


class AssetReader(Protocol):
    def read(self, href: str, grid: Grid, fill: int, *, cache_key: tuple[str, str, str] | None = None) -> RawWindow: ...


def check_href(href: str) -> None:
    if not href.startswith(science.ASSET_HREF_PREFIX):
        raise ItemExcluded("href_not_allowed", href[:120])


def read_window_from_dataset(src: rasterio.io.DatasetReader, href: str, grid: Grid, fill: int) -> tuple[np.ndarray, WindowMeta]:
    epsg = src.crs.to_epsg() if src.crs else None
    if epsg != grid.epsg:
        raise ItemExcluded("different_utm_zone", f"source EPSG {epsg} ≠ grid EPSG {grid.epsg}")
    t = src.transform
    r = grid.res
    if (t.a, t.b, t.d, t.e) != (r, 0.0, 0.0, -r):
        raise ItemExcluded("grid_misaligned", f"pixel size/rotation {tuple(t)[:6]} ≠ ({r}, -{r})")
    dx, dy = grid.x0 - t.c, t.f - grid.y0
    if dx % r != 0 or dy % r != 0:
        raise ItemExcluded("grid_misaligned", f"source origin ({t.c}, {t.f}) not on the {r} m lattice")
    col_off, row_off = int(dx // r), int(dy // r)
    out = np.full(grid.shape, fill, dtype=src.dtypes[0])
    c0, r0 = max(col_off, 0), max(row_off, 0)
    c1, r1 = min(col_off + grid.width, src.width), min(row_off + grid.height, src.height)
    if c1 > c0 and r1 > r0:
        part = src.read(1, window=Window(c0, r0, c1 - c0, r1 - r0))
        out[r0 - row_off : r1 - row_off, c0 - col_off : c1 - col_off] = part
    meta = WindowMeta(
        href=href,
        dtype=str(src.dtypes[0]),
        crs_epsg=epsg,
        source_transform=tuple(t)[:6],
        window=(col_off, row_off, grid.width, grid.height),
        header_scale=float(src.scales[0]),
        header_offset=float(src.offsets[0]),
        header_nodata=None if src.nodata is None else float(src.nodata),
        bytes_read_px=max(c1 - c0, 0) * max(r1 - r0, 0),
    )
    return out, meta


class RasterioAssetReader:
    """Remote reader implementing the C1 §8 transient-failure policy.

    - Transport/I-O failures (``RasterioError``/``OSError`` on open or window read: timeouts, HTTP
      5xx, connection resets, truncated range responses) get 1 attempt + 3 retries with backoff
      1 s, 2 s, 4 s, on top of GDAL's own HTTP retries (``GDAL_HTTP_MAX_RETRY``). All I/O errors use
      the same bounded policy because GDAL does not expose a reliable transient/permanent split.
    - If every attempt fails, the item is excluded with ``asset_unreadable:<asset>``; each attempt's
      underlying error is kept in ``ItemExcluded.attempts`` and recorded in provenance.
    - Contract failures (other CRS, off-lattice, href not allowed) are never retried; they are
      scientific ineligibility with their own reason codes.
    - A successful retry returns the same bytes as a first-time success, so results are unchanged.
    """

    def __init__(self, sleep: Callable[[float], None] = time.sleep, opener: Callable[..., object] = rasterio.open) -> None:
        self._sleep = sleep
        self._open = opener

    def read(self, href: str, grid: Grid, fill: int, *, cache_key: tuple[str, str, str] | None = None) -> RawWindow:
        check_href(href)
        t0 = time.perf_counter()
        failures: list[dict[str, str]] = []
        for attempt in range(science.ASSET_READ_RETRIES + 1):
            try:
                with rasterio.Env(**GDAL_REMOTE_ENV), self._open(href) as src:  # type: ignore[attr-defined]
                    data, meta = read_window_from_dataset(src, href, grid, fill)
                secs = time.perf_counter() - t0
                log.info(
                    "asset.read href=%s window=%s px=%d %.2fs",
                    href.rsplit("/", 2)[-2:],
                    meta.window,
                    meta.bytes_read_px,
                    secs,
                )
                return RawWindow(data, meta, False, secs)
            except ItemExcluded:
                raise
            except (RasterioError, OSError) as exc:
                detail = error_chain(exc)
                failures.append({"attempt": str(attempt + 1), "error": type(exc).__name__, "detail": detail})
                if attempt < science.ASSET_READ_RETRIES:
                    delay = science.ASSET_READ_BACKOFF_S[attempt]
                    log.warning("asset.read retry %d in %.0fs href=%s: %s", attempt + 1, delay, href, detail)
                    self._sleep(delay)
        log.warning("asset.unreadable href=%s after %d attempts", href, len(failures))
        raise ItemExcluded("asset_unreadable", href, attempts=failures)


class CachedAssetReader:
    """Window cache keyed by (grid_hash, item_id, asset_key) (storage design, "Key layout").

    The cache is not evidence: it stores the raw DN window plus the header metadata needed for
    the C1 §7 cross-checks, so a cached read yields exactly the same provenance as a remote one.
    """

    def __init__(self, inner: AssetReader, cache_root: Path) -> None:
        self.inner = inner
        self.root = cache_root

    def _paths(self, key: tuple[str, str, str]) -> tuple[Path, Path]:
        grid_hash, item_id, asset = key
        for part in key:
            if "/" in part or ".." in part:
                raise ValueError("unsafe cache key")
        base = self.root / "cache" / "windows" / grid_hash / item_id
        return base / f"{asset}.npy", base / f"{asset}.json"

    def read(self, href: str, grid: Grid, fill: int, *, cache_key: tuple[str, str, str] | None = None) -> RawWindow:
        check_href(href)
        if cache_key is None:
            return self.inner.read(href, grid, fill)
        arr_p, meta_p = self._paths(cache_key)
        if arr_p.exists() and meta_p.exists():
            t0 = time.perf_counter()
            raw_meta: dict[str, Any] = json.loads(meta_p.read_text())
            meta = WindowMeta(
                href=raw_meta["href"],
                dtype=raw_meta["dtype"],
                crs_epsg=int(raw_meta["crs_epsg"]),
                source_transform=tuple(raw_meta["source_transform"]),
                window=(*raw_meta["window"],),
                header_scale=float(raw_meta["header_scale"]),
                header_offset=float(raw_meta["header_offset"]),
                header_nodata=None if raw_meta["header_nodata"] is None else float(raw_meta["header_nodata"]),
                bytes_read_px=int(raw_meta["bytes_read_px"]),
            )
            data = np.load(arr_p)
            if meta.href == href and data.shape == grid.shape:
                return RawWindow(data, meta, True, time.perf_counter() - t0)
        raw = self.inner.read(href, grid, fill)
        arr_p.parent.mkdir(parents=True, exist_ok=True)
        tmp = arr_p.with_suffix(".tmp.npy")
        np.save(tmp, raw.data)
        tmp.replace(arr_p)
        meta_p.write_text(json.dumps(asdict(raw.meta)))
        return raw
