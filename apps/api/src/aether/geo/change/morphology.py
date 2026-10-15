"""Morphology, 8-connected components and minimum mapping unit (C5 §7 steps 1–3).

- Per category on G10: binary opening (3 × 3 square), then binary closing (3 × 3 square), then
  AND ``joint_valid`` so closing never creates pixels outside the compared area.
- Components: 8-connectivity (``scipy.ndimage.label`` with a 3 × 3 ones structure).
- MMU: components with fewer than ``ceil(mmu_m2 / pixel_area_true_m2)`` pixels are removed.

Labels are deterministic: ``scipy.ndimage.label`` numbers components in raster-scan order of their
first pixel, and :func:`apply_mmu` renumbers the kept components 1..m in that same order. The label
of a component therefore depends only on the mask, never on hashing, process or wall clock.

Border handling: scipy treats outside-the-grid as background. The analysis grid carries a 200 m
buffer around the AOI (C3 §3) and ``joint_valid`` is in-AOI only, so the grid border never touches
a candidate pixel.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import ndimage

from aether.domain.errors import InvariantViolation
from aether.geo.change.joint import JointValid
from aether.geo.grid import Grid, GridArray, require_same_grid

SQUARE_3X3 = np.ones((3, 3), dtype=bool)
CONNECTIVITY_8 = np.ones((3, 3), dtype=int)


def _bool_mask(mask: GridArray) -> np.ndarray:
    if not isinstance(mask, GridArray) or mask.data.dtype != np.bool_:
        raise InvariantViolation("morphology_input", "morphology expects a boolean GridArray mask")
    return mask.data


def clean_mask(mask: GridArray, joint: JointValid) -> GridArray:
    """Opening then closing (3 × 3 square), ANDed with ``joint_valid`` (same grid required, I-19)."""
    data = _bool_mask(mask)
    if not isinstance(joint, JointValid):
        raise InvariantViolation("morphology_input", "morphology expects the joint_valid result")
    grid = require_same_grid(mask, joint.grid)
    opened = ndimage.binary_opening(data, structure=SQUARE_3X3)
    closed = ndimage.binary_closing(opened, structure=SQUARE_3X3)
    return GridArray(grid, np.asarray(closed & joint.mask, dtype=bool), joint.mask.copy())


def label_components(mask: GridArray, grid: Grid) -> tuple[GridArray, int]:
    """8-connected component labels (int32, 0 = background), numbered in raster-scan order.

    ``mask`` must live on ``grid`` (I-19).
    """
    data = _bool_mask(mask)
    require_same_grid(mask, grid)
    labels, n = ndimage.label(data, structure=CONNECTIVITY_8)
    return GridArray(grid, labels.astype(np.int32), mask.valid.copy()), int(n)


def mmu_min_pixels(mmu_m2: float, pixel_area_true_m2: float) -> int:
    """C5 §7: ``ceil(mmu_m2 / pixel_area_true_m2)``."""
    if pixel_area_true_m2 <= 0:
        raise InvariantViolation("pixel_area", "pixel area must be positive")
    return math.ceil(mmu_m2 / pixel_area_true_m2)


def apply_mmu(labels: GridArray, n: int, min_pixels: int, grid: Grid) -> tuple[GridArray, int, int]:
    """Drop components smaller than ``min_pixels``; renumber the rest 1..m in label order.

    ``labels`` must live on ``grid`` (I-19). Returns ``(labels, kept, removed)``.
    """
    if not isinstance(labels, GridArray) or labels.data.dtype != np.int32:
        raise InvariantViolation("label_input", "component labels must be an int32 GridArray")
    require_same_grid(labels, grid)
    counts = np.bincount(labels.data.ravel(), minlength=n + 1)
    keep = np.flatnonzero(counts[1:] >= min_pixels) + 1
    lut = np.zeros(n + 1, dtype=np.int32)
    lut[keep] = np.arange(1, keep.size + 1, dtype=np.int32)
    return GridArray(grid, lut[labels.data], labels.valid.copy()), int(keep.size), int(n - keep.size)
