"""Sentinel-2 C1 L2A radiometric contract ``radiometry.s2c1.v1`` (C2).

The only path from raw DN to a number science may touch. Metadata comes from the per-asset STAC
``raster:bands[0]`` object; only the single verified combination is accepted (I-2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from aether import science

RadiometryReason = str  # radiometry_metadata_missing | _unexpected | _inconsistent | _dtype_mismatch


@dataclass(frozen=True)
class AssetRadiometry:
    """Validated radiometric metadata for one asset (recorded in provenance)."""

    asset_key: str
    data_type: str
    scale: float | None
    offset: float | None
    nodata: int
    source: str = "stac:asset.raster:bands"


def validate_stac_metadata(asset_key: str, asset: dict[str, Any]) -> AssetRadiometry | RadiometryReason:
    """C2 §3: return validated metadata, or the exclusion reason code."""
    kind = science.ASSETS[asset_key].kind
    allow = science.RADIOMETRY_ALLOW[kind]
    bands = asset.get("raster:bands")
    if not isinstance(bands, list) or not bands or not isinstance(bands[0], dict):
        return "radiometry_metadata_missing"
    band = bands[0]
    if "data_type" not in band or "nodata" not in band:
        return "radiometry_metadata_missing"
    if kind == "reflectance" and ("scale" not in band or "offset" not in band):
        return "radiometry_metadata_missing"
    if band["data_type"] != allow.data_type or band["nodata"] != allow.nodata:
        return "radiometry_metadata_unexpected"
    if kind == "reflectance":
        # Exact float comparison after JSON parse (C2 §3).
        if band["scale"] != allow.scale or band["offset"] != allow.offset:
            return "radiometry_metadata_unexpected"
    elif ("scale" in band and band["scale"] not in (None, 1, 1.0)) or ("offset" in band and band["offset"] not in (None, 0, 0.0)):
        return "radiometry_metadata_unexpected"
    return AssetRadiometry(asset_key, allow.data_type, allow.scale, allow.offset, allow.nodata)


def check_header(
    meta: AssetRadiometry, dtype: str, header_scale: float, header_offset: float, header_nodata: float | None
) -> RadiometryReason | None:
    """C1 §7 cross-check of the opened file against STAC. GDAL reports (1, 0) when absent."""
    if dtype != meta.data_type:
        return "radiometry_dtype_mismatch"
    if header_nodata is not None and header_nodata != meta.nodata:
        return "radiometry_metadata_inconsistent"
    scale_present = header_scale != 1.0 or header_offset != 0.0
    if (
        scale_present
        and meta.scale is not None
        and (
            not np.isclose(header_scale, meta.scale, rtol=0, atol=1e-12)
            or not np.isclose(header_offset, meta.offset or 0.0, rtol=0, atol=1e-12)
        )
    ):
        return "radiometry_metadata_inconsistent"
    if scale_present and meta.scale is None:
        return "radiometry_metadata_inconsistent"
    return None


@dataclass(frozen=True)
class Normalized:
    reflectance: np.ndarray  # float32, NaN where invalid
    valid: np.ndarray  # bool
    n_nodata: int
    n_saturated: int


def normalize(dn: np.ndarray, meta: AssetRadiometry) -> Normalized:
    """C2 §4–§5: invalid-pixel mask on raw DN, then ρ = DN × scale + offset in float32.

    Values < 0 and > 1 are kept (not clipped); clipping is a display concern only.
    """
    if meta.scale is None or meta.offset is None:
        raise ValueError(f"asset {meta.asset_key} has no reflectance scale/offset")
    if dn.dtype != np.uint16:
        raise ValueError(f"expected uint16 DN, got {dn.dtype}")
    nodata = dn == meta.nodata
    saturated = dn == science.SATURATED_DN
    valid = ~(nodata | saturated)
    rho = dn.astype(np.float32) * np.float32(meta.scale) + np.float32(meta.offset)
    rho[~valid] = np.nan
    return Normalized(rho, valid, int(nodata.sum()), int(saturated.sum()))
