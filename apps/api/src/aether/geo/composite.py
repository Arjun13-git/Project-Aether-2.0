"""Per-period composites (C4 §3, invariant I-18).

MEASUREMENT:  reflectance_obs → index_obs (per observation) → per-pixel median over observations
NOT USED:     reflectance_obs → per-band median → index of the medians

The measured composite accepts only :class:`IndexObservation` objects, which can only be produced
by :func:`aether.geo.indices.compute_index` from two reflectances of the **same** observation.
Per-band reflectance medians are different types (:class:`DisplayComposite`, :class:`QaComposite`)
that no measurement function accepts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.grid import GridArray, require_same_grid
from aether.geo.indices import IndexObservation, Reflectance


@dataclass(frozen=True)
class IndexComposite:
    """Median index composite of one period. ``array.valid`` = sufficient (n_valid ≥ n_min)."""

    index: str
    array: GridArray
    n_valid: np.ndarray  # uint8 per-pixel support count
    n_min: int
    observation_ids: tuple[str, ...]


@dataclass(frozen=True)
class DisplayComposite:
    """Per-band reflectance median. DISPLAY ONLY — never a measurement input (C4 §3)."""

    purpose: str  # "display_rgb"
    bands: tuple[str, ...]
    arrays: tuple[GridArray, ...]


@dataclass(frozen=True)
class QaComposite:
    """Per-band NIR reflectance median. CO-REGISTRATION QA ONLY — never a measurement input (C4 §3).

    ``array.valid`` = ``n_valid_nir ≥ n_min``; insufficient pixels are NaN and never filled.
    """

    purpose: str  # "coregistration_qa"
    band: str  # "nir"
    array: GridArray
    n_valid: np.ndarray  # uint8
    n_min: int
    observation_ids: tuple[str, ...]


def median_index_composite(observations: Sequence[IndexObservation], n_min: int) -> IndexComposite:
    if not observations:
        raise InvariantViolation("composite_empty", "no observations to composite")
    for o in observations:
        if not isinstance(o, IndexObservation):
            raise InvariantViolation(
                "composite_input_type", "measured composites accept per-observation index values only (I-18)"
            )
    names = {o.index for o in observations}
    if len(names) != 1:
        raise InvariantViolation("composite_mixed_indices", f"mixed indices {sorted(names)}")
    ids = [o.observation_id for o in observations]
    if len(set(ids)) != len(ids):
        raise InvariantViolation("composite_duplicate_observation", "an observation appears twice")
    grid = require_same_grid(*(o.array for o in observations))
    if len(observations) > 255:
        raise InvariantViolation("composite_too_many_observations", "n_valid is stored as uint8")

    stack = np.stack([np.where(o.array.valid, o.array.data, np.nan) for o in observations])
    n_valid = np.stack([o.array.valid for o in observations]).sum(axis=0).astype(np.uint8)
    sufficient = n_valid >= n_min
    med = np.full(grid.shape, np.nan, dtype=np.float32)
    if sufficient.any():
        # numpy nanmedian: even count → mean of the two middle values (C4 §3).
        med[sufficient] = np.nanmedian(stack[:, sufficient], axis=0).astype(np.float32)
    return IndexComposite(
        index=names.pop(),
        array=GridArray(grid, med, sufficient),
        n_valid=n_valid,
        n_min=n_min,
        observation_ids=tuple(sorted(ids)),
    )


def display_rgb_composite(
    observations: Sequence[tuple[Reflectance, Reflectance, Reflectance]], quality: Sequence[GridArray]
) -> DisplayComposite:
    """Per-band median of red/green/blue over pixels valid in all three bands and the SCL mask."""
    if not observations:
        raise InvariantViolation("composite_empty", "no observations to composite")
    grid = require_same_grid(*(r.array for obs in observations for r in obs), *quality)
    out: list[GridArray] = []
    for band_idx, band in enumerate(("red", "green", "blue")):
        vals = []
        for obs, q in zip(observations, quality, strict=True):
            if obs[band_idx].asset_key != band:
                raise InvariantViolation("display_band_order", "expected (red, green, blue)")
            ok = obs[0].array.valid & obs[1].array.valid & obs[2].array.valid & q.valid
            vals.append(np.where(ok, obs[band_idx].array.data, np.nan))
        stack = np.stack(vals)
        has = ~np.all(np.isnan(stack), axis=0)
        med = np.full(grid.shape, np.nan, dtype=np.float32)
        if has.any():
            med[has] = np.nanmedian(stack[:, has], axis=0)
        out.append(GridArray(grid, med, has))
    return DisplayComposite("display_rgb", ("red", "green", "blue"), tuple(out))


def nir_qa_composite(observations: Sequence[Reflectance], quality: Sequence[GridArray], n_min: int) -> QaComposite:
    """C4 §3 NIR QA composite: per-pixel median of ``nir`` over valid observations (reflectance and SCL)."""
    if not observations:
        raise InvariantViolation("composite_empty", "no observations to composite")
    if len(observations) > 255:
        raise InvariantViolation("composite_too_many_observations", "n_valid is stored as uint8")
    ids = [r.observation_id for r in observations]
    if len(set(ids)) != len(ids):
        raise InvariantViolation("composite_duplicate_observation", "an observation appears twice")
    for r in observations:
        if not isinstance(r, Reflectance) or r.asset_key != "nir":
            raise InvariantViolation("qa_composite_input", "the QA composite accepts nir reflectance only")
    grid = require_same_grid(*(r.array for r in observations), *quality)
    if grid.name != "G10":
        raise InvariantViolation("qa_composite_grid", f"the NIR QA composite is defined on G10, got {grid.name}")
    ok = [r.array.valid & q.valid for r, q in zip(observations, quality, strict=True)]
    stack = np.stack([np.where(v, r.array.data, np.nan) for r, v in zip(observations, ok, strict=True)])
    n_valid = np.stack(ok).sum(axis=0).astype(np.uint8)
    sufficient = n_valid >= n_min
    med = np.full(grid.shape, np.nan, dtype=np.float32)
    if sufficient.any():
        med[sufficient] = np.nanmedian(stack[:, sufficient], axis=0).astype(np.float32)
    return QaComposite("coregistration_qa", "nir", GridArray(grid, med, sufficient), n_valid, n_min, tuple(sorted(ids)))


def compositing_step(c: IndexComposite) -> dict[str, object]:
    """Provenance step ``compositing`` (C2 §6)."""
    return {
        "kind": "compositing",
        "version": science.COMPOSITE_VERSION,
        "index": c.index,
        "statistic": "median",
        "strategy": "per_observation_index_then_temporal_median",
        "n_min": c.n_min,
        "observation_ids": list(c.observation_ids),
        "sufficient_px": int(c.array.valid.sum()),
    }
