"""Before/after image-content shift check ``coreg.phase_corr.v1`` (C3 §9; Phase 2 freeze C-01–C-03).

Inputs are the unfilled NIR QA composites of periods A and B (C4 §3) on the identical G10 grid.

- Support (grid-level, C-03): the jointly valid fraction of the **whole G10 grid** must be ≥ 0.5
  and the bounding window of the jointly valid pixels must be ≥ 256 × 256 px; otherwise
  ``not_assessable``. This is distinct from the AOI-level ``joint_coverage`` gate (C6 §5.1),
  which is evaluated *before* co-registration (:func:`pre_registration_outcome`).
- Fill isolation (C-01): invalid window pixels are median-filled per image on private in-memory
  copies used only by the estimator. The copies never leave :func:`_estimate_shift`; the result
  type holds scalars only, so filled values cannot reach any measurement, statistic or geometry.
- Gate: ``residual_px = hypot(dy, dx)``; > 0.5 px → ``coregistration_failed``. The phase-correlation
  error and the NIR-QA σ inside the window are observational, non-gating metrics (C-02).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from skimage.registration import phase_cross_correlation

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.composite import QaComposite
from aether.geo.grid import require_same_grid

CoregStatus = Literal["ok", "failed", "not_assessable"]


@dataclass(frozen=True)
class CoregSupport:
    """Grid-level registration support (C-03). ``window`` = (row0, row1, col0, col1), end-exclusive."""

    joint_valid_px: int
    grid_px: int
    grid_fraction: float
    window: tuple[int, int, int, int] | None
    assessable: bool
    reason: str | None  # "insufficient_grid_support" | "insufficient_window" | None

    @property
    def window_shape(self) -> tuple[int, int] | None:
        if self.window is None:
            return None
        r0, r1, c0, c1 = self.window
        return (r1 - r0, c1 - c0)


@dataclass(frozen=True)
class CoregResult:
    """Scalar-only result: no array field exists, so no filled pixel can be returned (C-01)."""

    status: CoregStatus
    support: CoregSupport
    shift_dy_px: float | None = None
    shift_dx_px: float | None = None
    residual_px: float | None = None
    pcc_error: float | None = None  # observational (C-02)
    nir_qa_std_a: float | None = None  # observational (C-02): unfilled jointly valid window pixels
    nir_qa_std_b: float | None = None
    upsample_factor: int = science.COREG_UPSAMPLE_FACTOR

    @property
    def unavailable_reason(self) -> str | None:
        """Reason for the unavailable registration measurements of a not-assessable check."""
        return "insufficient_support" if self.status == "not_assessable" else None

    @property
    def limitations(self) -> tuple[str, ...]:
        """C3 §9: not assessable → blocking ``coregistration_not_verified`` (and band cap ``lower``, C5 §8)."""
        return ("coregistration_not_verified",) if self.status == "not_assessable" else ()


def pre_registration_outcome(
    observations_usable_a: int, observations_usable_b: int, joint_coverage: float | None, n_min: int
) -> str | None:
    """C-03 steps 1–2, evaluated before co-registration. ``None`` → co-registration may run.

    ``joint_coverage`` is AOI-level (joint_valid_area / aoi_grid_area, C6); it is never the
    grid-level registration support.
    """
    if observations_usable_a < n_min or observations_usable_b < n_min:
        return "insufficient_observations"
    if joint_coverage is None or joint_coverage < science.COVERAGE_INSUFFICIENT_BELOW.value:
        return "insufficient_coverage"
    return None


def coregistration_outcome(result: CoregResult) -> str | None:
    """C-03 step 3: only a residual above the gate ends the run; not assessable continues."""
    return "coregistration_failed" if result.status == "failed" else None


def registration_support(qa_a: QaComposite, qa_b: QaComposite) -> CoregSupport:
    """P13: grid-level jointly valid fraction and the bounding window of jointly valid pixels."""
    _check_inputs(qa_a, qa_b)
    joint = qa_a.array.valid & qa_b.array.valid
    n = int(joint.sum())
    grid_px = int(joint.size)
    frac = n / grid_px
    if n == 0:
        return CoregSupport(0, grid_px, 0.0, None, False, "insufficient_grid_support")
    rows = np.flatnonzero(joint.any(axis=1))
    cols = np.flatnonzero(joint.any(axis=0))
    window = (int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1)
    if frac < science.COREG_MIN_GRID_FRACTION:
        return CoregSupport(n, grid_px, frac, window, False, "insufficient_grid_support")
    h, w = window[1] - window[0], window[3] - window[2]
    if h < science.COREG_MIN_WINDOW_PX or w < science.COREG_MIN_WINDOW_PX:
        return CoregSupport(n, grid_px, frac, window, False, "insufficient_window")
    return CoregSupport(n, grid_px, frac, window, True, None)


def assess_coregistration(qa_a: QaComposite, qa_b: QaComposite) -> CoregResult:
    """C3 §9 step 2. Call only after :func:`pre_registration_outcome` returned ``None`` (C-03)."""
    support = registration_support(qa_a, qa_b)
    if not support.assessable or support.window is None:
        return CoregResult("not_assessable", support)
    r0, r1, c0, c1 = support.window
    joint = (qa_a.array.valid & qa_b.array.valid)[r0:r1, c0:c1]
    win_a = qa_a.array.data[r0:r1, c0:c1]
    win_b = qa_b.array.data[r0:r1, c0:c1]
    dy, dx, error = _estimate_shift(win_a, win_b, joint)
    residual = math.hypot(dy, dx)
    status: CoregStatus = "failed" if residual > science.COREG_MAX_RESIDUAL_PX.value else "ok"
    return CoregResult(
        status,
        support,
        shift_dy_px=dy,
        shift_dx_px=dx,
        residual_px=residual,
        pcc_error=error,
        nir_qa_std_a=float(np.std(win_a[joint].astype(np.float64))),
        nir_qa_std_b=float(np.std(win_b[joint].astype(np.float64))),
    )


def coregistration_step(result: CoregResult) -> dict[str, object]:
    """Provenance step for the co-registration check (scalars only)."""
    s = result.support
    return {
        "kind": "coregistration",
        "version": science.COREG_VERSION,
        "method": "skimage.registration.phase_cross_correlation",
        "input": "nir_qa_composite (unfilled; jointly valid pixels)",
        "fill": "per-image median of jointly valid window pixels, estimator-only in-memory copy (C-01)",
        "upsample_factor": result.upsample_factor,
        "min_grid_fraction": science.COREG_MIN_GRID_FRACTION,
        "min_window_px": science.COREG_MIN_WINDOW_PX,
        "max_residual_px": science.COREG_MAX_RESIDUAL_PX.value,
        "status": result.status,
        "support_reason": s.reason,
        "joint_valid_px": s.joint_valid_px,
        "grid_px": s.grid_px,
        "window": list(s.window) if s.window is not None else None,
    }


def _check_inputs(qa_a: QaComposite, qa_b: QaComposite) -> None:
    for q in (qa_a, qa_b):
        if not isinstance(q, QaComposite) or q.band != "nir":
            raise InvariantViolation("coreg_input_type", "co-registration accepts NIR QA composites only (C3 §9)")
    require_same_grid(qa_a.array, qa_b.array)  # step 1: exact grid identity
    if qa_a.array.grid.name != "G10":
        raise InvariantViolation("coreg_grid", "co-registration runs on G10")


def _estimate_shift(win_a: np.ndarray, win_b: np.ndarray, joint: np.ndarray) -> tuple[float, float, float]:
    """P14/P15: fill private copies, run phase correlation, return scalars only."""
    fill_a = _filled_copy(win_a, joint)
    fill_b = _filled_copy(win_b, joint)
    shift, error, _phasediff = phase_cross_correlation(fill_a, fill_b, upsample_factor=science.COREG_UPSAMPLE_FACTOR)
    return float(shift[0]), float(shift[1]), float(error)


def _filled_copy(window: np.ndarray, joint: np.ndarray) -> np.ndarray:
    """Copy of ``window`` with non-jointly-valid pixels set to this image's jointly valid median."""
    out = np.array(window, dtype=np.float64, copy=True)
    out[~joint] = float(np.median(out[joint]))
    return out
