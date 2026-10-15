"""Quality mask ``mask.scl.v1`` — SCL-only, deterministic (C4 §2).

Valid = SCL ∈ {4, 5, 6} and not within the 60 m dilation (disk radius 3 px on G20) of
{3, 8, 9, 10}. Every invalid pixel keeps a reason so masked fractions can be reported by cause.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import binary_dilation

from aether import science
from aether.geo.grid import GridArray

# Reason codes stored in the per-pixel reason raster (uint8). Order = registry MASK_REASONS.
CLEAR, NODATA, CLOUD_SHADOW, SNOW_ICE, OTHER_INVALID = range(5)
REASON_NAMES = ("clear", "nodata", "cloud_shadow", "snow_ice", "other_invalid")


def disk(radius: int) -> np.ndarray:
    """All offsets with Euclidean distance ≤ radius (C4 §2)."""
    r = np.arange(-radius, radius + 1)
    yy, xx = np.meshgrid(r, r, indexing="ij")
    return (xx * xx + yy * yy) <= radius * radius


@dataclass(frozen=True)
class SclMask:
    valid: GridArray  # G20 boolean (data = valid flag)
    reason: np.ndarray  # G20 uint8 reason codes
    class_counts: dict[str, int]
    dilated_px: int  # valid-class pixels removed by dilation


def scl_mask(scl: GridArray, profile: science.MaskProfile = science.SCL_MASK) -> SclMask:
    """Classify an SCL window (G20, uint8; nodata = 0) into validity + reason."""
    if scl.grid.res != 20:
        raise ValueError("SCL mask is defined on G20")
    codes = np.where(scl.valid, scl.data, 0).astype(np.uint8)
    seed = np.isin(codes, list(profile.dilated_classes))
    dilated = binary_dilation(seed, structure=disk(profile.dilation_radius_px_20m))
    valid_class = np.isin(codes, list(profile.valid_classes))
    valid = valid_class & ~dilated

    reason = np.full(codes.shape, OTHER_INVALID, dtype=np.uint8)
    reason[valid] = CLEAR
    reason[(codes == 2) | seed | (valid_class & dilated)] = CLOUD_SHADOW
    reason[codes == profile.snow_class] = SNOW_ICE
    reason[codes == 0] = NODATA

    counts = {
        science.SCL_LABELS.get(int(c), f"UNKNOWN_{int(c)}"): int(n)
        for c, n in zip(*np.unique(codes, return_counts=True), strict=True)
    }
    return SclMask(
        valid=GridArray(scl.grid, valid, valid.copy()),
        reason=reason,
        class_counts=counts,
        dilated_px=int((valid_class & dilated).sum()),
    )


def masking_step(mask: SclMask, profile: science.MaskProfile = science.SCL_MASK) -> dict[str, object]:
    """Provenance step ``masking`` (C2 §6)."""
    return {
        "kind": "masking",
        "profile": profile.version,
        "valid_classes": sorted(profile.valid_classes),
        "dilated_classes": sorted(profile.dilated_classes),
        "dilation_radius_px": profile.dilation_radius_px_20m,
        "dilation_grid": "G20",
        "snow_class": profile.snow_class,
        "class_counts": mask.class_counts,
        "dilated_valid_class_px": mask.dilated_px,
    }
