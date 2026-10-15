"""The complete list of analysis resampling operations (C3 §6, I-5).

Only exact operations exist. G20 nests G10 exactly (one G20 cell = a 2 × 2 G10 block), so:
- R1: categorical G20 → G10 by nearest (block replication);
- R2: continuous reflectance G10 → G20 by 2 × 2 block mean, invalid unless all 4 cells valid.
- R3: 20 m index deltas / validity G20 → G10 by nearest (block replication), for the joint
  change rules on G10 (C5 §3; Phase 2).
Reprojection between CRSs does not exist in MVP analysis.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np

from aether.domain.errors import InvariantViolation
from aether.geo.grid import Grid, GridArray


def _check_nesting(g10: Grid, g20: Grid) -> None:
    if (
        g10.res != 10
        or g20.res != 20
        or g10.epsg != g20.epsg
        or g10.grid_hash != g20.grid_hash
        or g10.x0 != g20.x0
        or g10.y0 != g20.y0
        or g10.width != 2 * g20.width
        or g10.height != 2 * g20.height
    ):
        raise InvariantViolation("grid_mismatch", "G10/G20 do not nest exactly")


def r1_nearest_20_to_10(src: GridArray, g10: Grid) -> tuple[GridArray, dict[str, Any]]:
    """R1: categorical (SCL / boolean masks) G20 → G10 by exact block replication."""
    _check_nesting(g10, src.grid)
    data = np.repeat(np.repeat(src.data, 2, axis=0), 2, axis=1)
    valid = np.repeat(np.repeat(src.valid, 2, axis=0), 2, axis=1)
    step = {
        "rule": "R1",
        "data_class": "categorical",
        "method": "nearest",
        "source_grid": src.grid.name,
        "target_grid": g10.name,
    }
    return GridArray(g10, data, valid), step


def r2_block_mean_10_to_20(src: GridArray, g20: Grid) -> tuple[GridArray, dict[str, Any]]:
    """R2: continuous reflectance G10 → G20; invalid unless all four children are valid."""
    _check_nesting(src.grid, g20)
    h, w = g20.shape
    vals = src.data.reshape(h, 2, w, 2).astype(np.float64)
    ok = src.valid.reshape(h, 2, w, 2).all(axis=(1, 3))
    mean = np.where(ok, np.nan_to_num(vals).mean(axis=(1, 3)), np.nan).astype(np.float32)
    step = {
        "rule": "R2",
        "data_class": "continuous",
        "method": "block_mean_2x2_all_valid",
        "source_grid": src.grid.name,
        "target_grid": g20.name,
    }
    return GridArray(g20, mean, ok), step


def r3_nearest_20_to_10(
    src: GridArray, g10: Grid, data_class: Literal["continuous", "boolean"]
) -> tuple[GridArray, dict[str, Any]]:
    """R3: 20 m index deltas (continuous) or validity (boolean) G20 → G10 by exact block replication."""
    _check_nesting(g10, src.grid)
    data = np.repeat(np.repeat(src.data, 2, axis=0), 2, axis=1)
    valid = np.repeat(np.repeat(src.valid, 2, axis=0), 2, axis=1)
    step = {
        "rule": "R3",
        "data_class": data_class,
        "method": "nearest",
        "source_grid": src.grid.name,
        "target_grid": g10.name,
    }
    return GridArray(g10, data, valid), step
