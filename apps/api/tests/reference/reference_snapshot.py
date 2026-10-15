"""Independent reference implementation of the Phase 1 snapshot maths (evaluation plan, Phase 1).

Written separately from ``aether.geo`` and deliberately simple. It uses only numpy and rasterio and
does NOT import ``aether``. It reads windows that already sit on one 20 m-aligned square (10 m
assets 2N × 2N, 20 m assets N × N) and recomputes the following from the contract text:
- reflectance: header scale/offset; DN 0 and 65535 invalid;
- SCL validity with a radius-3 dilation;
- the four indices with the undefined rules;
- per-pixel medians over observations with n ≥ 3;
- AOI zonal statistics.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.warp import transform_geom

BANDS10 = ("blue", "green", "red", "nir")
BANDS20 = ("swir16", "scl")
N_MIN = 3


def _load(path: Path) -> tuple[np.ndarray, float, float, rasterio.Affine, str]:
    with rasterio.open(path) as src:
        return src.read(1), src.scales[0], src.offsets[0], src.transform, src.crs.to_string()


def _reflectance(dn: np.ndarray, scale: float, offset: float) -> np.ndarray:
    out = dn.astype(np.float64) * scale + offset
    out[(dn == 0) | (dn == 65535)] = np.nan
    return out


def _dilate_disk3(mask: np.ndarray) -> np.ndarray:
    out = np.zeros_like(mask)
    h, w = mask.shape
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            if dx * dx + dy * dy > 9:
                continue
            shifted = np.zeros_like(mask)
            ys, yd = (slice(max(dy, 0), h), slice(0, h - max(dy, 0))) if dy >= 0 else (slice(0, h + dy), slice(-dy, h))
            xs, xd = (slice(max(dx, 0), w), slice(0, w - max(dx, 0))) if dx >= 0 else (slice(0, w + dx), slice(-dx, w))
            shifted[yd, xd] = mask[ys, xs]
            out |= shifted
    return out


def _nd(a: np.ndarray, b: np.ndarray, ok: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        v = (a - b) / (a + b)
    bad = ~ok | np.isnan(a) | np.isnan(b) | ((a + b) <= 1e-4) | (np.abs(v) > 1)
    v[bad] = np.nan
    return v


def _block_mean(x: np.ndarray) -> np.ndarray:
    h, w = x.shape[0] // 2, x.shape[1] // 2
    return x.reshape(h, 2, w, 2).mean(axis=(1, 3))  # NaN in any child → NaN


def run(fixture_dir: Path, item_ids: list[str], size_tag: str, aoi_geojson_4326: dict) -> dict:
    per_index: dict[str, list[np.ndarray]] = {"ndvi": [], "ndwi": [], "mndwi": [], "ndbi": []}
    t10 = t20 = crs = None
    for item in item_ids:
        refl = {}
        for band in BANDS10 + BANDS20:
            dn, scale, offset, t, crs = _load(fixture_dir / f"{item}_{band}_{size_tag}.tif")
            if band in BANDS10:
                t10 = t
            else:
                t20 = t
            refl[band] = dn if band == "scl" else _reflectance(dn, scale, offset)
        scl = refl["scl"]
        dil = _dilate_disk3(np.isin(scl, [3, 8, 9, 10]))
        ok20 = np.isin(scl, [4, 5, 6]) & ~dil
        ok10 = np.kron(ok20, np.ones((2, 2), dtype=bool)).astype(bool)
        per_index["ndvi"].append(_nd(refl["nir"], refl["red"], ok10))
        per_index["ndwi"].append(_nd(refl["green"], refl["nir"], ok10))
        g20, n20 = _block_mean(refl["green"]), _block_mean(refl["nir"])
        per_index["mndwi"].append(_nd(g20, refl["swir16"], ok20))
        per_index["ndbi"].append(_nd(refl["swir16"], n20, ok20))

    geom = transform_geom("EPSG:4326", crs, aoi_geojson_4326)
    out: dict = {}
    for name, stack_list in per_index.items():
        stack = np.stack(stack_list)
        count = np.sum(~np.isnan(stack), axis=0)
        comp = np.full(stack.shape[1:], np.nan)
        good = count >= N_MIN
        comp[good] = np.nanmedian(stack[:, good], axis=0)
        t = t10 if name in ("ndvi", "ndwi") else t20
        in_aoi = geometry_mask([geom], out_shape=comp.shape, transform=t, all_touched=False, invert=True)
        sample = comp[in_aoi & good]
        support = sample.size / in_aoi.sum()
        stats = None
        if support >= 0.5:
            p10, med, p90 = np.percentile(sample, [10, 50, 90])
            stats = {"mean": float(sample.mean()), "median": float(med), "p10": float(p10), "p90": float(p90)}
        out[name] = {"composite": comp, "n_valid": count, "support": support, "stats": stats, "n_pixels": int(sample.size)}
    return out
