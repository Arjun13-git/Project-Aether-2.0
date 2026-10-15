"""Robust standardisation, mode selection (C5 §4) and per-index significance (C5 §5).

```text
med_I = median(Δ_I)   MAD_I = median(|Δ_I − med_I|)   z_I = (Δ_I − med_I) / (1.4826 · MAD_I)
```

over the joint_valid G10 pixels. Mode, first applicable row wins:
- ``n_eligible < 1000`` or ``MAD_I == 0`` for any I → ``absolute_only`` + ``robust_reference_unavailable``;
- more than 30 % of eligible pixels with ``|z_NDVI| > 2`` → ``absolute_only`` + ``widespread_change``;
- otherwise ``robust``.

All thresholds are frozen, untuned defaults (C5 §9); there are no overrides (C-09).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.change.delta import IndexDelta
from aether.geo.change.joint import JointValid
from aether.geo.grid import require_same_grid

Mode = Literal["robust", "absolute_only"]


@dataclass(frozen=True)
class RobustReference:
    mode: Mode
    limitations: tuple[str, ...]  # blocking (C5 §4)
    n_eligible: int
    median: dict[str, float | None]
    mad: dict[str, float | None]
    widespread_fraction: float | None  # fraction of eligible pixels with |z_NDVI| > 2
    z: dict[str, np.ndarray] | None  # G10 float64, NaN outside joint_valid; only in robust mode


def _checked(deltas: Mapping[str, IndexDelta], joint: JointValid) -> dict[str, np.ndarray]:
    if not isinstance(joint, JointValid) or not all(isinstance(d, IndexDelta) for d in deltas.values()):
        raise InvariantViolation("change_input_type", "robust reference accepts IndexDelta and JointValid only")
    if sorted(deltas) != sorted(science.CHANGE_INDICES):
        raise InvariantViolation("change_index_set", f"expected deltas for {list(science.CHANGE_INDICES)}")
    out = {}
    for name in science.CHANGE_INDICES:
        d = deltas[name]
        if d.index != name:
            raise InvariantViolation("change_index_mismatch", f"expected {name}, got {d.index}")
        require_same_grid(d.g10, joint.grid)
        if np.any(joint.mask & ~d.g10.valid):
            raise InvariantViolation("joint_valid_outside_delta", f"joint_valid includes pixels without Δ {name}")
        out[name] = d.g10.data
    return out


def robust_reference(deltas: Mapping[str, IndexDelta], joint: JointValid) -> RobustReference:
    data = _checked(deltas, joint)
    n = joint.joint_valid_px
    meds: dict[str, float] = {}
    mads: dict[str, float] = {}
    if n > 0:
        for name, arr in data.items():
            v = arr[joint.mask]
            meds[name] = float(np.median(v))
            mads[name] = float(np.median(np.abs(v - meds[name])))
    med: dict[str, float | None] = {name: meds.get(name) for name in science.CHANGE_INDICES}
    mad: dict[str, float | None] = {name: mads.get(name) for name in science.CHANGE_INDICES}
    if n < science.ROBUST_MIN_ELIGIBLE_PX.value or any(m == 0.0 for m in mads.values()):
        return RobustReference("absolute_only", ("robust_reference_unavailable",), n, med, mad, None, None)

    z = {
        name: np.where(joint.mask, (arr - meds[name]) / (science.ROBUST_MAD_SCALE * mads[name]), np.nan)
        for name, arr in data.items()
    }
    frac = float(np.mean(np.abs(z["ndvi"][joint.mask]) > science.WIDESPREAD_Z.value))
    if frac > science.WIDESPREAD_FRAC.value:
        return RobustReference("absolute_only", ("widespread_change",), n, med, mad, frac, None)
    return RobustReference("robust", (), n, med, mad, frac, z)


def significance(deltas: Mapping[str, IndexDelta], joint: JointValid, ref: RobustReference) -> dict[str, np.ndarray]:
    """C5 §5: ``s_I`` ∈ {−1, 0, +1} (int8) on G10; 0 outside joint_valid.

    robust: ``|z_I| ≥ k ∧ |Δ_I| ≥ a_I``; absolute_only: ``|Δ_I| ≥ a_I``.
    """
    data = _checked(deltas, joint)
    zs = ref.z
    if ref.mode == "robust" and zs is None:
        raise InvariantViolation("robust_reference_incomplete", "robust mode without z-scores")
    out = {}
    for name, arr in data.items():
        safe = np.where(joint.mask, arr, 0.0)
        sig = joint.mask & (np.abs(safe) >= science.CHANGE_ABS_THRESHOLDS[name].value)
        if ref.mode == "robust" and zs is not None:
            sig &= np.abs(np.where(joint.mask, zs[name], 0.0)) >= science.CHANGE_K.value
        out[name] = np.where(sig, np.sign(safe), 0).astype(np.int8)
    return out
