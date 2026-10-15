"""Change engine ``change.index_diff.v1``: two periods on one grid → co-registration → change products.

Pure engine (no DB, no web), the Phase 2 counterpart of :mod:`aether.geo.snapshot`. A change run is
self-contained (C-07): each period is processed here by the shared Phase 1 period engine
(:func:`aether.geo.period.run_period`) from its own scenes. Nothing is read from a snapshot run;
reuse between runs happens only through the window cache and content-addressed artifacts.

Order (C6 §5.1 / C-03), evaluated once per run:
1. fewer than n_min usable observations in A or in B → ``insufficient_observations``;
2. AOI ``joint_coverage`` < 0.5 → ``insufficient_coverage``;
3. co-registration (grid-level support, C3 §9): residual > 0.5 px → ``coregistration_failed``;
   not assessable → continue with blocking ``coregistration_not_verified`` (bands capped ``lower``);
then Δ (C5 §3), delta statistics (C-06), robust reference (§4), significance (§5), categories (§6),
morphology/MMU/polygons and event measurements (§7–§8).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import numpy as np

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.change.delta import IndexDelta, delta_statistics, index_delta
from aether.geo.change.events import EventsResult, extract_events
from aether.geo.change.joint import JointValid, joint_valid
from aether.geo.change.robust import RobustReference, robust_reference, significance
from aether.geo.change.rules import categorize, change_rules_step
from aether.geo.coreg import CoregResult, assess_coregistration, coregistration_step, pre_registration_outcome
from aether.geo.grid import AnalysisGrid, GridArray, pixel_area_true_m2, require_same_grid
from aether.geo.io.windows import AssetReader
from aether.geo.period import CancelFn, PeriodResult, ProgressFn, run_period
from aether.geo.stac.eligibility import SceneRecord
from aether.geo.stats import ZonalStats, stats_step, zonal_stats

log = logging.getLogger(__name__)

PeriodLabel = Literal["a", "b"]
Outcome = Literal["completed", "insufficient_observations", "insufficient_coverage", "coregistration_failed"]


@dataclass(frozen=True)
class PeriodSummary:
    """One period of a change run: the period engine result plus its C6 §1 / C4 §5 quantities."""

    label: PeriodLabel
    scenes: list[SceneRecord]
    result: PeriodResult
    aoi_grid_px: int
    observed_px: int  # in-AOI pixels with n_valid(NDVI) ≥ 1
    valid_px: int  # in-AOI pixels with n_valid(NDVI) ≥ n_min
    stats: dict[str, ZonalStats]  # per-period index statistics (own support rule, C6 §5.1)

    @property
    def coverage(self) -> float | None:
        return self.valid_px / self.aoi_grid_px if self.aoi_grid_px else None

    @property
    def observations_usable(self) -> int:
        return len(self.result.observations_usable)


@dataclass(frozen=True)
class ChangeProducts:
    """Everything computed after the outcome gate passed (outcome ``completed``)."""

    deltas: dict[str, IndexDelta]
    delta_stats: dict[str, ZonalStats]
    reference: RobustReference
    significance: dict[str, np.ndarray]  # s_I ∈ {−1, 0, +1} int8 on G10 (the per-index flags)
    categories: GridArray  # uint8 codes 0–6 on G10
    events: EventsResult


@dataclass(frozen=True)
class ChangeAnalysis:
    grid: AnalysisGrid
    pixel_area_true_m2: float
    period_a: PeriodSummary
    period_b: PeriodSummary
    outcome: Outcome
    limitations: tuple[str, ...]  # run-level; blocking unless noted in the registry
    joint: JointValid | None
    coregistration: CoregResult | None  # None ⇔ not run (early stop)
    change: ChangeProducts | None
    steps: list[dict[str, Any]]
    timings_s: dict[str, float] = field(default_factory=dict)

    @property
    def coregistration_status(self) -> str:
        return "not_run" if self.coregistration is None else self.coregistration.status

    @property
    def coregistration_unavailable_reason(self) -> str | None:
        """C-03: early stop → the run outcome; not assessable → ``insufficient_support``."""
        if self.coregistration is None:
            return self.outcome
        return self.coregistration.unavailable_reason


def _summarize(label: PeriodLabel, scenes: list[SceneRecord], p: PeriodResult, grid: AnalysisGrid, n_min: int) -> PeriodSummary:
    aoi = int(p.in_aoi10.sum())
    ndvi = p.composites.get("ndvi")
    observed = int((p.in_aoi10 & (ndvi.n_valid >= 1)).sum()) if ndvi is not None else 0
    valid = int((p.in_aoi10 & (ndvi.n_valid >= n_min)).sum()) if ndvi is not None else 0
    stats: dict[str, ZonalStats] = {}
    for name, spec in science.INDICES.items():
        region = p.in_aoi10 if spec.grid == "G10" else p.in_aoi20
        if name in p.composites:
            stats[name] = zonal_stats(p.composites[name], region)
        else:
            stats[name] = ZonalStats(name, False, "insufficient_observations", int(region.sum()), 0, None)
    return PeriodSummary(label, scenes, p, aoi, observed, valid, stats)


def _has_change_composites(p: PeriodResult) -> bool:
    return all(n in p.composites for n in science.CHANGE_INDICES)


def run_change(
    *,
    grid: AnalysisGrid,
    scenes_a: list[SceneRecord],
    scenes_b: list[SceneRecord],
    reader: AssetReader,
    n_min: int = int(science.N_MIN.value),
    progress: ProgressFn | None = None,
    cancelled: CancelFn | None = None,
) -> ChangeAnalysis:
    """Run ``change.index_diff.v1``. Scenes must already carry eligibility (C1 §5), one list per period."""
    t_start = time.perf_counter()
    progress = progress or (lambda stage, frac: None)
    timings: dict[str, float] = {}

    # --- per period: the shared Phase 1 engine, independently for A and B (C-07) ---
    periods: dict[PeriodLabel, PeriodSummary] = {}
    order: tuple[tuple[PeriodLabel, list[SceneRecord]], ...] = (("a", scenes_a), ("b", scenes_b))
    for label, scenes in order:
        p = run_period(
            grid=grid,
            scenes=scenes,
            reader=reader,
            n_min=n_min,
            progress=lambda stage, frac, lb=label: progress(f"{stage}_{lb}", frac),
            cancelled=cancelled,
            period=label,
            qa_composite=True,
        )
        for k, v in p.timings_s.items():
            timings[f"{k}_{label}"] = v
        periods[label] = _summarize(label, scenes, p, grid, n_min)
    pa, pb = periods["a"], periods["b"]
    steps: list[dict[str, Any]] = [*pa.result.steps, *pb.result.steps]
    for s in (pa, pb):
        for name, spec in science.INDICES.items():
            steps.append({**stats_step(s.stats[name], spec.grid), "period": s.label})

    t0 = time.perf_counter()
    progress("change", 0.0)
    pixel_area = pixel_area_true_m2(grid.g10, grid.lon_c, grid.lat_c)
    in10 = GridArray(grid.g10, pa.result.in_aoi10, np.ones(grid.g10.shape, bool))
    in20 = GridArray(grid.g20, pa.result.in_aoi20, np.ones(grid.g20.shape, bool))
    if not (np.array_equal(pa.result.in_aoi10, pb.result.in_aoi10) and np.array_equal(pa.result.in_aoi20, pb.result.in_aoi20)):
        raise InvariantViolation("aoi_mask_mismatch", "periods A and B were processed with different AOI masks")

    joint: JointValid | None = None
    if _has_change_composites(pa.result) and _has_change_composites(pb.result):
        # P1: A/B grid identity is enforced inside joint_valid (require_same_grid on every composite).
        joint = joint_valid(pa.result.composites, pb.result.composites, in10, in20, grid.g10, grid.g20)
        steps.extend(joint.steps)

    # --- outcome gate, steps 1–2 (C-03) ---
    outcome: Outcome = "completed"
    pre = pre_registration_outcome(pa.observations_usable, pb.observations_usable, joint.joint_coverage if joint else None, n_min)
    limitations: list[str] = []
    coreg: CoregResult | None = None
    change: ChangeProducts | None = None
    if pre is not None:
        outcome = cast(Outcome, pre)
    else:
        # --- step 3: co-registration on the unfilled NIR QA composites (grid-level support) ---
        if pa.result.qa is None or pb.result.qa is None:
            raise InvariantViolation("qa_composite_missing", "change runs require NIR QA composites for both periods")
        coreg = assess_coregistration(pa.result.qa, pb.result.qa)
        steps.append(coregistration_step(coreg))
        if coreg.status == "failed":
            outcome = "coregistration_failed"
        else:
            limitations.extend(coreg.limitations)

    if outcome == "completed":
        if joint is None or coreg is None:
            raise InvariantViolation("change_gate_inconsistent", "completed outcome without joint_valid or co-registration")
        cov = joint.joint_coverage
        if cov is not None and cov < science.COVERAGE_PARTIAL_BELOW.value:
            limitations.append("partial_coverage")
        change = _change_products(grid, pa, pb, joint, coreg, in10, pixel_area, steps)
        limitations.extend(change.reference.limitations)
    timings["change"] = time.perf_counter() - t0
    timings["total"] = time.perf_counter() - t_start
    log.info(
        "change.done outcome=%s usable_a=%d usable_b=%d joint_coverage=%s coreg=%s events=%s %.1fs",
        outcome,
        pa.observations_usable,
        pb.observations_usable,
        joint.joint_coverage if joint else None,
        coreg.status if coreg else "not_run",
        len(change.events.events) if change else 0,
        timings["total"],
    )
    return ChangeAnalysis(
        grid=grid,
        pixel_area_true_m2=pixel_area,
        period_a=pa,
        period_b=pb,
        outcome=outcome,
        limitations=tuple(limitations),
        joint=joint,
        coregistration=coreg,
        change=change,
        steps=steps,
        timings_s=timings,
    )


def _change_products(
    grid: AnalysisGrid,
    pa: PeriodSummary,
    pb: PeriodSummary,
    joint: JointValid,
    coreg: CoregResult,
    in10: GridArray,
    pixel_area: float,
    steps: list[dict[str, Any]],
) -> ChangeProducts:
    """C5 §3–§8 on index composites only. The co-registration result enters as scalars (status/residual)."""
    ca, cb = pa.result.composites, pb.result.composites
    require_same_grid(joint.grid, grid.g10)
    deltas = {n: index_delta(ca[n], cb[n], grid.g10) for n in science.CHANGE_INDICES}
    for d in deltas.values():
        steps.extend(d.steps)
    delta_stats = {n: delta_statistics(d, joint) for n, d in deltas.items()}
    for n, s in delta_stats.items():
        steps.append(
            {
                **stats_step(s, "G10"),
                "region": "joint_valid",
                "sample_set": "joint_valid (R3-replicated Δ for G20 indices)",
                "statistic_of": f"delta_{n}",
                "denominator": "aoi_grid_pixels_10m",
            }
        )
    ref = robust_reference(deltas, joint)
    sig = significance(deltas, joint, ref)
    categories, r3_steps = categorize(sig, deltas, ca["mndwi"], cb["mndwi"], joint)
    steps.extend(r3_steps)
    steps.append(change_rules_step(ref, categories))
    events = extract_events(
        categories=categories,
        joint=joint,
        deltas=deltas,
        sig=sig,
        ref=ref,
        ndvi_a=ca["ndvi"],
        ndvi_b=cb["ndvi"],
        in_aoi10=in10,
        coreg=coreg,
        pixel_area_true_m2=pixel_area,
    )
    steps.extend(events.steps)
    return ChangeProducts(deltas, delta_stats, ref, sig, categories, events)
