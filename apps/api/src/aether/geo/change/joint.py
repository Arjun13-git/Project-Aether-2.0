"""Joint-valid mask ``elig10`` and its AOI-level support (C5 §2, C6 §1).

```text
elig20(q) = in_AOI_G20(q) ∧ ∀ I∈{MNDWI,NDBI}: n_valid_A(q,I) ≥ n_min ∧ n_valid_B(q,I) ≥ n_min
elig10(p) = in_AOI_G10(p) ∧ n_valid_A(p,NDVI) ≥ n_min ∧ n_valid_B(p,NDVI) ≥ n_min ∧ elig20(parent_G20(p))
```

Validity comes from the per-pixel support counts only; composite values (and anything filled
elsewhere) never decide membership. NDWI is not a change index and does not enter the mask.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.composite import IndexComposite
from aether.geo.grid import Grid, GridArray, require_same_grid
from aether.geo.resample import r3_nearest_20_to_10


@dataclass(frozen=True)
class JointValid:
    """``mask`` = joint_valid on G10. Support is AOI-level: joint_valid / aoi_grid pixels."""

    grid: Grid
    mask: np.ndarray
    joint_valid_px: int
    aoi_grid_px: int
    steps: tuple[dict[str, Any], ...]

    @property
    def joint_coverage(self) -> float | None:
        return self.joint_valid_px / self.aoi_grid_px if self.aoi_grid_px else None

    def joint_valid_area_m2(self, pixel_area_true_m2: float) -> float:
        return self.joint_valid_px * pixel_area_true_m2

    def aoi_grid_area_m2(self, pixel_area_true_m2: float) -> float:
        return self.aoi_grid_px * pixel_area_true_m2


def sufficient(c: IndexComposite) -> np.ndarray:
    """``n_valid ≥ n_min`` for one composite, cross-checked against its validity mask."""
    if not isinstance(c, IndexComposite):
        raise InvariantViolation("change_input_type", "change primitives accept measured index composites only")
    ok = c.n_valid >= c.n_min
    if not np.array_equal(ok, c.array.valid):
        raise InvariantViolation("composite_validity_mismatch", f"{c.index}: validity differs from n_valid ≥ n_min")
    return ok


def joint_valid(
    a: Mapping[str, IndexComposite],
    b: Mapping[str, IndexComposite],
    in_aoi10: GridArray,
    in_aoi20: GridArray,
    g10: Grid,
    g20: Grid,
) -> JointValid:
    """C5 §2 ``elig10`` from period A/B composites of NDVI (G10), MNDWI and NDBI (G20)."""
    n_mins = set()
    for period in (a, b):
        for name in science.CHANGE_INDICES:
            if name not in period:
                raise InvariantViolation("change_index_missing", f"{name} composite is required")
            c = period[name]
            if not isinstance(c, IndexComposite):
                raise InvariantViolation("change_input_type", "change primitives accept measured index composites only")
            if c.index != name:
                raise InvariantViolation("change_index_mismatch", f"expected {name}, got {c.index}")
            require_same_grid(c.array, g10 if science.INDICES[name].grid == "G10" else g20)
            n_mins.add(c.n_min)
    if len(n_mins) != 1:
        raise InvariantViolation("change_n_min_mismatch", f"composites use different n_min {sorted(n_mins)}")
    for m in (in_aoi10, in_aoi20):
        if not isinstance(m, GridArray) or m.data.dtype != np.bool_:
            raise InvariantViolation("aoi_mask_input", "in-AOI masks must be boolean GridArrays")
    require_same_grid(in_aoi10, g10)  # I-19: grid identity, not shape equality
    require_same_grid(in_aoi20, g20)

    elig20 = in_aoi20.data.copy()
    for name in ("mndwi", "ndbi"):
        elig20 &= sufficient(a[name]) & sufficient(b[name])
    elig20_on_10, step = r3_nearest_20_to_10(GridArray(g20, elig20, np.ones(g20.shape, dtype=bool)), g10, "boolean")
    mask = in_aoi10.data & sufficient(a["ndvi"]) & sufficient(b["ndvi"]) & elig20_on_10.data
    return JointValid(
        grid=g10,
        mask=mask,
        joint_valid_px=int(mask.sum()),
        aoi_grid_px=int(in_aoi10.data.sum()),
        steps=({"kind": "resampling", **step, "purpose": "joint_valid elig20 → G10"},),
    )
