"""Signed index deltas Δ_I = I_B − I_A (C5 §3) and delta statistics (C-06).

- Only NDVI, MNDWI and NDBI have deltas (NDWI is statistics-only, C-06).
- Δ is computed on the index's native grid; MNDWI/NDBI deltas are replicated to G10 by R3.
- Δ is defined only where both periods are sufficient; everything else is NaN, valid = False.
- Delta statistics sample the ``joint_valid`` G10 pixels; support = joint_valid / aoi_grid pixels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.change.joint import JointValid, sufficient
from aether.geo.composite import IndexComposite
from aether.geo.grid import Grid, GridArray, require_same_grid
from aether.geo.resample import r3_nearest_20_to_10
from aether.geo.stats import ZonalStats


@dataclass(frozen=True)
class IndexDelta:
    index: str
    native: GridArray  # Δ on the index's own grid (float64)
    g10: GridArray  # Δ on G10 (R3-replicated for G20 indices; identical to ``native`` for NDVI)
    steps: tuple[dict[str, Any], ...]


def index_delta(a: IndexComposite, b: IndexComposite, g10: Grid) -> IndexDelta:
    """Δ = B − A where both composites are sufficient; NaN elsewhere."""
    if not isinstance(a, IndexComposite) or not isinstance(b, IndexComposite):
        raise InvariantViolation("change_input_type", "change primitives accept measured index composites only")
    if a.index != b.index:
        raise InvariantViolation("change_index_mismatch", f"cannot difference {a.index} and {b.index}")
    if a.index not in science.CHANGE_INDICES:
        raise InvariantViolation("change_index_not_supported", f"{a.index} is not a change index (C-06)")
    grid = require_same_grid(a.array, b.array)
    if grid.name != science.INDICES[a.index].grid:
        raise InvariantViolation("index_grid", f"{a.index} is defined on {science.INDICES[a.index].grid}")
    ok = sufficient(a) & sufficient(b)
    diff = b.array.data.astype(np.float64) - a.array.data.astype(np.float64)
    native = GridArray(grid, np.where(ok, diff, np.nan), ok)
    if grid.name == "G10":
        require_same_grid(grid, g10)
        return IndexDelta(a.index, native, native, ())
    on10, step = r3_nearest_20_to_10(native, g10, "continuous")
    return IndexDelta(a.index, native, on10, ({"kind": "resampling", **step, "purpose": f"delta_{a.index} → G10"},))


def delta_statistics(delta: IndexDelta, joint: JointValid) -> ZonalStats:
    """C-06: mean/median/p10/p90 of Δ over joint_valid; unavailable if support < 0.5."""
    if not isinstance(delta, IndexDelta) or not isinstance(joint, JointValid):
        raise InvariantViolation("change_input_type", "delta statistics accept IndexDelta and JointValid only")
    if delta.index not in science.CHANGE_INDICES:
        raise InvariantViolation("change_index_not_supported", f"{delta.index} has no delta statistics (C-06)")
    require_same_grid(delta.g10, joint.grid)
    if joint.aoi_grid_px == 0:
        return ZonalStats(delta.index, False, "not_applicable", 0, 0, None)
    if np.any(joint.mask & ~delta.g10.valid):
        raise InvariantViolation("joint_valid_outside_delta", f"joint_valid includes pixels without Δ {delta.index}")
    n = joint.joint_valid_px
    support = n / joint.aoi_grid_px
    if support < science.STATS_MIN_SUPPORT.value:
        return ZonalStats(delta.index, False, "insufficient_support", joint.aoi_grid_px, n, support)
    vals = delta.g10.data[joint.mask]
    if not np.all(np.isfinite(vals)):
        raise InvariantViolation("delta_not_finite", f"non-finite Δ {delta.index} inside joint_valid")
    p10, med, p90 = np.percentile(vals, [10, 50, 90], method="linear")
    return ZonalStats(
        delta.index,
        True,
        None,
        joint.aoi_grid_px,
        n,
        support,
        mean=float(vals.mean()),
        median=float(med),
        p10=float(p10),
        p90=float(p90),
    )
