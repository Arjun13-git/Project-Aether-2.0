"""Zonal statistics over a region (C4 §5) and snapshot coverage (C6 §1–§2, §5.2)."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.composite import IndexComposite


@dataclass(frozen=True)
class ZonalStats:
    index: str
    available: bool
    unavailable_reason: str | None
    region_pixels: int
    n_pixels: int
    support_fraction: float | None
    mean: float | None = None
    median: float | None = None
    p10: float | None = None
    p90: float | None = None


def zonal_stats(composite: IndexComposite, region: np.ndarray) -> ZonalStats:
    """Statistics of a measured index composite over ``region`` (boolean, on the composite grid).

    Sample set = region pixels where the composite is defined (n_valid ≥ n_min). If the support
    fraction is below 0.5, every statistic is unavailable (``insufficient_support``); they are
    never computed on the remainder and never reported as 0.
    """
    if not isinstance(composite, IndexComposite):
        raise InvariantViolation("stats_input_type", "statistics accept measured index composites only")
    if region.shape != composite.array.grid.shape or region.dtype != np.bool_:
        raise InvariantViolation("grid_mismatch", "region mask does not match the composite grid")
    region_px = int(region.sum())
    if region_px == 0:
        return ZonalStats(composite.index, False, "not_applicable", 0, 0, None)
    sample = composite.array.valid & region
    n = int(sample.sum())
    support = n / region_px
    if support < science.STATS_MIN_SUPPORT.value:
        return ZonalStats(composite.index, False, "insufficient_support", region_px, n, support)
    vals = composite.array.data[sample].astype(np.float64)
    p10, med, p90 = np.percentile(vals, [10, 50, 90], method="linear")
    return ZonalStats(
        composite.index,
        True,
        None,
        region_px,
        n,
        support,
        mean=float(vals.mean()),
        median=float(med),
        p10=float(p10),
        p90=float(p90),
    )


def stats_step(s: ZonalStats, grid_name: str) -> dict[str, object]:
    return {
        "kind": "statistics",
        "method": science.ZONAL_STATS_METHOD,
        "index": s.index,
        "grid": grid_name,
        "region": "aoi (pixel-centre rule)",
        "percentile_method": "linear",
        "min_support_fraction": science.STATS_MIN_SUPPORT.value,
        "available": s.available,
        "unavailable_reason": s.unavailable_reason,
    }


@dataclass(frozen=True)
class SnapshotCoverage:
    """C6 quantities for a snapshot run (period A only). Areas in m², raster areas on G10."""

    aoi_area_m2: float
    pixel_area_true_m2: float
    aoi_grid_px_10m: int
    aoi_grid_px_20m: int
    observed_px: int
    valid_px: int
    observations_total: int
    observations_usable: int
    outcome: str
    limitations: list[str] = field(default_factory=list)

    @property
    def aoi_grid_area_m2(self) -> float:
        return self.aoi_grid_px_10m * self.pixel_area_true_m2

    @property
    def observed_area_m2(self) -> float:
        return self.observed_px * self.pixel_area_true_m2

    @property
    def valid_area_m2(self) -> float:
        return self.valid_px * self.pixel_area_true_m2

    @property
    def coverage_a(self) -> float | None:
        return self.valid_px / self.aoi_grid_px_10m if self.aoi_grid_px_10m else None


def snapshot_outcome(observations_usable: int, coverage_a: float | None, n_min: int) -> tuple[str, list[str]]:
    """C6 §5.2, evaluated in order."""
    if observations_usable < n_min:
        return "insufficient_observations", []
    if coverage_a is None or coverage_a < science.COVERAGE_INSUFFICIENT_BELOW.value:
        return "insufficient_coverage", []
    if coverage_a < science.COVERAGE_PARTIAL_BELOW.value:
        return "completed", ["partial_coverage"]
    return "completed", []


def snapshot_coverage(
    *,
    aoi_area_m2: float,
    pixel_area_true_m2: float,
    in_aoi_10: np.ndarray,
    in_aoi_20: np.ndarray,
    ndvi_n_valid: np.ndarray | None,
    observations_total: int,
    observations_usable: int,
    n_min: int,
) -> SnapshotCoverage:
    """Coverage is defined on NDVI support (C6 §1): observed = n_valid ≥ 1, valid = n_valid ≥ n_min."""
    aoi10 = int(in_aoi_10.sum())
    if ndvi_n_valid is None:
        observed = valid = 0
    else:
        observed = int((in_aoi_10 & (ndvi_n_valid >= 1)).sum())
        valid = int((in_aoi_10 & (ndvi_n_valid >= n_min)).sum())
    cov = valid / aoi10 if aoi10 else None
    outcome, lims = snapshot_outcome(observations_usable, cov, n_min)
    return SnapshotCoverage(
        aoi_area_m2,
        pixel_area_true_m2,
        aoi10,
        int(in_aoi_20.sum()),
        observed,
        valid,
        observations_total,
        observations_usable,
        outcome,
        lims,
    )
