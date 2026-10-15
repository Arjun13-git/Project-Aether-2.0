"""Per-pixel change categories (C5 §6), first match wins, on G10.

| Code | Category | Rule |
|---|---|---|
| 1 | water_increase | s_MNDWI = +1 ∧ MNDWI_B > 0 |
| 2 | water_decrease | s_MNDWI = −1 ∧ MNDWI_A > 0 |
| 3 | built_bare_increase | s_NDBI = +1 ∧ Δ_NDVI ≤ 0 |
| 4 | veg_loss | s_NDVI = −1 |
| 5 | veg_gain | s_NDVI = +1 |
| 6 | other_significant | any other s_I ≠ 0 |
| 0 | none | otherwise |

A category is a typical-correlate label of an index change, never a land-cover or object claim
(C5 §6, C4 §4). The per-index flags ``s_I`` are retained alongside (the significance layers).
MNDWI_A/B enter the rules on G10 through R3 nearest replication of the G20 composites.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import numpy as np

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.change.delta import IndexDelta
from aether.geo.change.joint import JointValid
from aether.geo.change.robust import RobustReference
from aether.geo.composite import IndexComposite
from aether.geo.grid import GridArray, require_same_grid
from aether.geo.resample import r3_nearest_20_to_10

CATEGORIES: Final[dict[int, str]] = {
    1: "water_increase",
    2: "water_decrease",
    3: "built_bare_increase",
    4: "veg_loss",
    5: "veg_gain",
    6: "other_significant",
}


def categorize(
    sig: Mapping[str, np.ndarray],
    deltas: Mapping[str, IndexDelta],
    mndwi_a: IndexComposite,
    mndwi_b: IndexComposite,
    joint: JointValid,
) -> tuple[GridArray, list[dict[str, Any]]]:
    """uint8 category codes 0–6 on G10 (0 outside joint_valid); returns the R3 steps used."""
    if sorted(sig) != sorted(science.CHANGE_INDICES):
        raise InvariantViolation("change_index_set", f"expected significance for {list(science.CHANGE_INDICES)}")
    for c in (mndwi_a, mndwi_b):
        if not isinstance(c, IndexComposite) or c.index != "mndwi":
            raise InvariantViolation("change_input_type", "categories need the MNDWI index composites of A and B")
    if not isinstance(deltas.get("ndvi"), IndexDelta) or not isinstance(joint, JointValid):
        raise InvariantViolation("change_input_type", "categories need Δ NDVI and joint_valid")
    g10 = joint.grid
    require_same_grid(deltas["ndvi"].g10, g10)
    for name in science.CHANGE_INDICES:
        if sig[name].shape != g10.shape or sig[name].dtype != np.int8:
            raise InvariantViolation("grid_mismatch", f"significance layer {name} is not an int8 G10 raster")

    steps = []
    on10 = {}
    for label, c in (("a", mndwi_a), ("b", mndwi_b)):
        r3, step = r3_nearest_20_to_10(c.array, g10, "continuous")
        on10[label] = np.where(r3.valid, r3.data, np.nan)
        steps.append({"kind": "resampling", **step, "purpose": f"MNDWI_{label.upper()} → G10 for category rules"})

    s_n, s_m, s_b = sig["ndvi"], sig["mndwi"], sig["ndbi"]
    d_ndvi = np.where(joint.mask, deltas["ndvi"].g10.data, np.nan)
    with np.errstate(invalid="ignore"):
        rules = [
            (1, (s_m == 1) & (on10["b"] > 0)),
            (2, (s_m == -1) & (on10["a"] > 0)),
            (3, (s_b == 1) & (d_ndvi <= 0)),
            (4, s_n == -1),
            (5, s_n == 1),
            (6, (s_n != 0) | (s_m != 0) | (s_b != 0)),
        ]
    codes = np.zeros(g10.shape, dtype=np.uint8)
    unassigned = joint.mask.copy()
    for code, hit in rules:
        take = unassigned & hit
        codes[take] = code
        unassigned &= ~take
    return GridArray(g10, codes, joint.mask.copy()), steps


def change_rules_step(ref: RobustReference, categories: GridArray) -> dict[str, Any]:
    """Provenance step ``change_rules`` (C7 §6): mode, robust reference and the frozen parameters."""
    counts = np.bincount(categories.data.ravel(), minlength=7)
    return {
        "kind": "change_rules",
        "version": science.CHANGE_VERSION,
        "mode": ref.mode,
        "limitations": list(ref.limitations),
        "n_eligible": ref.n_eligible,
        "median": ref.median,
        "mad": ref.mad,
        "mad_scale": science.ROBUST_MAD_SCALE,
        "widespread_fraction": ref.widespread_fraction,
        "parameters": {
            science.CHANGE_K.name: science.CHANGE_K.value,
            **{p.name: p.value for p in science.CHANGE_ABS_THRESHOLDS.values()},
            science.ROBUST_MIN_ELIGIBLE_PX.name: science.ROBUST_MIN_ELIGIBLE_PX.value,
            science.WIDESPREAD_Z.name: science.WIDESPREAD_Z.value,
            science.WIDESPREAD_FRAC.name: science.WIDESPREAD_FRAC.value,
            "calibration": str(science.Calibration.UNTUNED),
        },
        "category_px": {CATEGORIES[c]: int(counts[c]) for c in CATEGORIES},
    }
