"""Phase 1 snapshot engine: one AOI, one period → composites, coverage, statistics, provenance.

Pure engine (no DB, no web): STAC items in, arrays + provenance out. The stage order is the C2 §1
pipeline: window read → radiometric normalisation → grid placement (no resampling of reflectance
except the exact R2 block mean) → SCL mask → per-observation indices → median composite →
statistics/coverage. Change detection does not exist here (Phase 2).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.composite import DisplayComposite, IndexComposite
from aether.geo.grid import AnalysisGrid, pixel_area_true_m2
from aether.geo.io.windows import AssetReader
from aether.geo.period import CancelFn, Cancelled, ProgressFn, run_period
from aether.geo.stac.eligibility import SceneRecord
from aether.geo.stats import SnapshotCoverage, ZonalStats, snapshot_coverage, stats_step, zonal_stats

log = logging.getLogger(__name__)

__all__ = ["Cancelled", "SnapshotResult", "run_snapshot"]


@dataclass
class SnapshotResult:
    grid: AnalysisGrid
    scenes: list[SceneRecord]
    observations_total: int
    observations_used: list[str]  # datatake ids that contributed (after read-time exclusions)
    observations_usable: list[str]  # ≥ 1 defined in-AOI NDVI pixel
    composites: dict[str, IndexComposite]
    display: DisplayComposite | None
    coverage: SnapshotCoverage
    stats: dict[str, ZonalStats]
    mask_reason_px: dict[str, int]  # in-AOI G10 pixel-observations by reason
    aoi_pixel_observations: int
    steps: list[dict[str, Any]]
    timings_s: dict[str, float] = field(default_factory=dict)


def run_snapshot(
    *,
    grid: AnalysisGrid,
    aoi_area_m2: float,
    scenes: list[SceneRecord],
    reader: AssetReader,
    n_min: int = int(science.N_MIN.value),
    progress: ProgressFn | None = None,
    cancelled: CancelFn | None = None,
) -> SnapshotResult:
    """Run the Phase 1 snapshot pipeline. ``scenes`` must already carry eligibility (C1 §5)."""
    t_start = time.perf_counter()
    progress = progress or (lambda stage, frac: None)
    cancelled = cancelled or (lambda: False)

    p = run_period(grid=grid, scenes=scenes, reader=reader, n_min=n_min, progress=progress, cancelled=cancelled)
    timings = dict(p.timings_s)
    steps = p.steps
    in_aoi10, in_aoi20 = p.in_aoi10, p.in_aoi20
    composites, display = p.composites, p.display
    n_obs_total, used, usable = p.observations_total, p.observations_used, p.observations_usable
    reason_px, aoi_px_obs = p.mask_reason_px, p.aoi_pixel_observations
    timings.setdefault("coverage_stats", 0.0)

    # --- coverage (C6 §5.2) and statistics (C4 §5) ---
    progress("coverage", 0.0)
    t0 = time.perf_counter()
    pa = pixel_area_true_m2(grid.g10, grid.lon_c, grid.lat_c)
    cov = snapshot_coverage(
        aoi_area_m2=aoi_area_m2,
        pixel_area_true_m2=pa,
        in_aoi_10=in_aoi10,
        in_aoi_20=in_aoi20,
        ndvi_n_valid=composites["ndvi"].n_valid if "ndvi" in composites else None,
        observations_total=n_obs_total,
        observations_usable=len(usable),
        n_min=n_min,
    )
    stats: dict[str, ZonalStats] = {}
    for name, spec in science.INDICES.items():
        region = in_aoi10 if spec.grid == "G10" else in_aoi20
        if name not in composites:
            stats[name] = ZonalStats(name, False, "insufficient_observations", int(region.sum()), 0, None)
        else:
            z = zonal_stats(composites[name], region)
            if cov.outcome != "completed":
                # C6 §5.2: an insufficient run reports no statistic; support counts stay factual.
                z = ZonalStats(name, False, cov.outcome, z.region_pixels, z.n_pixels, z.support_fraction)
            stats[name] = z
        steps.append(stats_step(stats[name], spec.grid))
    if sum(reason_px.values()) != aoi_px_obs:
        raise InvariantViolation("mask_reason_accounting", "masked-reason pixel counts do not sum to the total")
    timings["coverage_stats"] += time.perf_counter() - t0
    timings["total"] = time.perf_counter() - t_start
    log.info(
        "snapshot.done outcome=%s usable=%d/%d coverage_a=%s %.1fs",
        cov.outcome,
        len(usable),
        n_obs_total,
        cov.coverage_a,
        timings["total"],
    )

    return SnapshotResult(
        grid=grid,
        scenes=scenes,
        observations_total=n_obs_total,
        observations_used=used,
        observations_usable=usable,
        composites=composites,
        display=display,
        coverage=cov,
        stats=stats,
        mask_reason_px=reason_px,
        aoi_pixel_observations=aoi_px_obs,
        steps=steps,
        timings_s=dict(timings),
    )
