"""Spectral indices ``indices.s2.v1`` (C4 §4) on single observations.

The frozen MVP set is NDVI, NDWI, MNDWI and NDBI. All four are normalised differences
``(a − b) / (a + b)`` of BOA reflectance. The value is **undefined** (NaN, valid = False) when:
1. a or b is invalid (radiometry or SCL mask);
2. ``a + b ≤ 1e-4`` (near-zero denominator);
3. ``|result| > 1`` (only possible with negative reflectance after the offset).

Undefined pixels carry an explicit ``valid = False``; downstream code selects samples with the
mask, never by relying on NaN arithmetic (C6 §3: NaN never propagates into a reported number).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.grid import GridArray, require_same_grid


@dataclass(frozen=True)
class Reflectance:
    """Normalised BOA reflectance of one asset of one observation (datatake)."""

    asset_key: str
    observation_id: str
    array: GridArray


@dataclass(frozen=True)
class IndexObservation:
    """One index computed from one observation. The only input a measured composite accepts."""

    index: str
    observation_id: str
    array: GridArray
    undefined_counts: dict[str, int]


def compute_index(spec: science.IndexSpec, a: Reflectance, b: Reflectance, quality: GridArray) -> IndexObservation:
    """Compute ``spec`` for one observation; ``quality`` is the SCL validity on the index grid."""
    if (a.asset_key, b.asset_key) != (spec.a, spec.b):
        raise InvariantViolation(
            "index_band_mapping", f"{spec.name} requires ({spec.a}, {spec.b}), got ({a.asset_key}, {b.asset_key})"
        )
    if a.observation_id != b.observation_id:
        # I-18: index terms must come from the same observation.
        raise InvariantViolation("index_mixed_observations", "index inputs come from different observations")
    grid = require_same_grid(a.array, b.array, quality)
    if grid.name != spec.grid:
        raise InvariantViolation("index_grid", f"{spec.name} is defined on {spec.grid}, got {grid.name}")

    av, bv = a.array.data.astype(np.float64), b.array.data.astype(np.float64)
    inputs_ok = a.array.valid & b.array.valid & quality.valid
    denom = av + bv
    small = inputs_ok & ~(denom > science.INDEX_DENOM_EPS)
    with np.errstate(invalid="ignore", divide="ignore"):
        val = (av - bv) / denom
    out_of_range = inputs_ok & ~small & (np.abs(val) > 1.0)
    defined = inputs_ok & ~small & ~out_of_range
    data = np.where(defined, val, np.nan).astype(np.float32)
    counts = {
        "input_invalid": int((~inputs_ok).sum()),
        "denominator_le_eps": int(small.sum()),
        "abs_gt_1": int(out_of_range.sum()),
        "defined": int(defined.sum()),
    }
    return IndexObservation(spec.name, a.observation_id, GridArray(grid, data, defined), counts)


def index_step(spec: science.IndexSpec, observation_id: str, counts: dict[str, int]) -> dict[str, object]:
    """Provenance step ``index`` (C2 §6)."""
    return {
        "kind": "index",
        "version": science.INDICES_VERSION,
        "index": spec.name,
        "formula": spec.formula,
        "bands": {"a": spec.a, "b": spec.b},
        "grid": spec.grid,
        "observation_id": observation_id,
        "undefined_rule": "invalid input | a+b <= 1e-4 | |x| > 1",
        "counts": counts,
    }
