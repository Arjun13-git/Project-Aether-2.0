"""Per-period processing engine (one AOI grid, one period of observations).

Factored out of the Phase 1 snapshot pipeline (Phase 2 gate G2) so that snapshot runs and future
change runs share exactly one implementation of: window read → radiometric normalisation → SCL mask
→ R1/R2 → per-observation indices → per-period median index composites (I-18) + RGB display
composite. Coverage, outcomes and statistics are the caller's responsibility.

This module is a verbatim extraction: snapshot outputs are byte-identical to Phase 1 (pinned by
tests/unit/test_phase1_identity.py and scripts/repro_check.py).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from aether import science
from aether.geo import masks, resample
from aether.geo.composite import (
    DisplayComposite,
    IndexComposite,
    compositing_step,
    display_rgb_composite,
    median_index_composite,
)
from aether.geo.grid import AnalysisGrid, GridArray, in_aoi_mask
from aether.geo.indices import IndexObservation, Reflectance, compute_index, index_step
from aether.geo.io.windows import AssetReader, ItemExcluded, RawWindow
from aether.geo.radiometry import check_header, normalize
from aether.geo.stac.eligibility import SceneRecord, group_observations

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, float], None]
CancelFn = Callable[[], bool]


@dataclass
class PeriodResult:
    """Everything computed for one period on one analysis grid."""

    in_aoi10: np.ndarray
    in_aoi20: np.ndarray
    observations_total: int
    observations_used: list[str]  # datatake ids that contributed (after read-time exclusions)
    observations_usable: list[str]  # ≥ 1 defined in-AOI NDVI pixel
    composites: dict[str, IndexComposite]
    display: DisplayComposite | None
    mask_reason_px: dict[str, int]  # in-AOI G10 pixel-observations by reason
    aoi_pixel_observations: int
    steps: list[dict[str, Any]]
    timings_s: dict[str, float] = field(default_factory=dict)


class Cancelled(Exception):
    pass


READ_WORKERS = 8  # concurrent window reads (HTTP range requests); results are order-independent


def _read_one(scene: SceneRecord, key: str, reader: AssetReader, grid: AnalysisGrid) -> RawWindow:
    spec = science.ASSETS[key]
    g = grid.g10 if spec.native_res_m == 10 else grid.g20
    meta = scene.radiometry[key]
    try:
        raw = reader.read(scene.assets[key].href, g, meta.nodata, cache_key=(grid.grid_hash, scene.item_id, key))
    except ItemExcluded as exc:
        if exc.reason == "asset_unreadable":
            raise ItemExcluded(f"asset_unreadable:{key}", str(exc), attempts=exc.attempts) from exc
        raise
    reason = check_header(meta, raw.meta.dtype, raw.meta.header_scale, raw.meta.header_offset, raw.meta.header_nodata)
    if reason is not None:
        raise ItemExcluded(reason, f"{scene.item_id}/{key}")
    return raw


def _read_observation(
    items: tuple[SceneRecord, ...], reader: AssetReader, grid: AnalysisGrid, pool: ThreadPoolExecutor
) -> list[tuple[SceneRecord, dict[str, RawWindow] | ItemExcluded]]:
    """Read all six assets of every item of one observation concurrently.

    Any failing asset excludes its whole item. When several assets fail, the reason reported is the
    first in C1 §3 asset order, so the outcome does not depend on thread timing.
    """
    futures = {(s.item_id, k): pool.submit(_read_one, s, k, reader, grid) for s in items for k in science.REQUIRED_ASSETS}
    out: list[tuple[SceneRecord, dict[str, RawWindow] | ItemExcluded]] = []
    for s in items:
        wins: dict[str, RawWindow] = {}
        failure: ItemExcluded | None = None
        for k in science.REQUIRED_ASSETS:
            try:
                wins[k] = futures[(s.item_id, k)].result()
            except ItemExcluded as exc:
                failure = failure or exc
        out.append((s, failure if failure is not None else wins))
    return out


def _mosaic(windows: list[RawWindow], nodata: int, kind: str) -> np.ndarray:
    """C1 §6: per pixel, the first item (id ascending) with a valid value supplies it."""
    result = windows[0].data.copy()
    have = _dn_valid(result, nodata, kind)
    for w in windows[1:]:
        take = ~have & _dn_valid(w.data, nodata, kind)
        result[take] = w.data[take]
        have |= take
    return result


def _dn_valid(dn: np.ndarray, nodata: int, kind: str) -> np.ndarray:
    ok = dn != nodata
    if kind == "reflectance":
        ok &= dn != science.SATURATED_DN
    return ok


def run_period(
    *,
    grid: AnalysisGrid,
    scenes: list[SceneRecord],
    reader: AssetReader,
    n_min: int = int(science.N_MIN.value),
    progress: ProgressFn | None = None,
    cancelled: CancelFn | None = None,
) -> PeriodResult:
    """Process one period. ``scenes`` must already carry eligibility (C1 §5)."""
    timings: dict[str, float] = defaultdict(float)
    steps: list[dict[str, Any]] = []
    progress = progress or (lambda stage, frac: None)
    cancelled = cancelled or (lambda: False)

    in_aoi10 = in_aoi_mask(grid.g10, grid.aoi_utm)
    in_aoi20 = in_aoi_mask(grid.g20, grid.aoi_utm)
    observations = group_observations(scenes)
    n_obs_total = len(observations)
    log.info(
        "period.start grid=%s %dx%d observations=%d",
        grid.grid_hash[:12],
        grid.g10.width,
        grid.g10.height,
        n_obs_total,
    )

    per_index: dict[str, list[IndexObservation]] = defaultdict(list)
    rgb_inputs: list[tuple[Reflectance, Reflectance, Reflectance]] = []
    rgb_quality: list[GridArray] = []
    used: list[str] = []
    usable: list[str] = []
    reason_px: dict[str, int] = dict.fromkeys(masks.REASON_NAMES, 0)
    aoi_px_obs = 0
    aoi10_count = int(in_aoi10.sum())

    pool = ThreadPoolExecutor(max_workers=READ_WORKERS, thread_name_prefix="aether-read")
    try:
        for i, obs in enumerate(observations):
            if cancelled():
                raise Cancelled()
            progress("ingest", i / max(n_obs_total, 1))
            # --- read (C1 §8, C3 §5) ---
            t0 = time.perf_counter()
            item_windows: list[tuple[SceneRecord, dict[str, RawWindow]]] = []
            for scene, res in _read_observation(obs.items, reader, grid, pool):
                if isinstance(res, ItemExcluded):
                    scene.status, scene.reason_code = "excluded", res.reason
                    log.info("scene.excluded item=%s reason=%s", scene.item_id, scene.reason_code)
                    steps.append(
                        {
                            "kind": "source_read_failed",
                            "item_id": scene.item_id,
                            "observation_id": obs.datatake_id,
                            "reason_code": res.reason,
                            "attempts": res.attempts,
                            "policy": "C1 §8: 1 attempt + 3 retries (1 s, 2 s, 4 s), then exclude",
                        }
                    )
                else:
                    item_windows.append((scene, res))
            timings["read"] += time.perf_counter() - t0
            if not item_windows:
                continue
            obs_items = [s.item_id for s, _ in item_windows]
            for scene, wins in item_windows:
                for key, raw in wins.items():
                    steps.append(
                        {
                            "kind": "source_read",
                            "item_id": scene.item_id,
                            "asset_key": key,
                            "href": raw.meta.href,
                            "file_checksum": scene.assets[key].file_checksum,
                            "native_epsg": raw.meta.crs_epsg,
                            "native_transform": list(raw.meta.source_transform),
                            "window": list(raw.meta.window),
                            "dtype": raw.meta.dtype,
                            "from_cache": raw.from_cache,
                        }
                    )

            # --- mosaic + radiometric normalisation (C2 §4–§5) ---
            t0 = time.perf_counter()
            refl: dict[str, GridArray] = {}
            for key in science.REQUIRED_ASSETS:
                spec = science.ASSETS[key]
                g = grid.g10 if spec.native_res_m == 10 else grid.g20
                meta = item_windows[0][0].radiometry[key]
                dn = _mosaic([w[key] for _, w in item_windows], meta.nodata, spec.kind)
                if spec.kind == "scl":
                    refl[key] = GridArray(g, dn, dn != meta.nodata)
                    continue
                norm = normalize(dn, meta)
                refl[key] = GridArray(g, norm.reflectance, norm.valid)
                steps.append(
                    {
                        "kind": "radiometric_normalization",
                        "version": science.RADIOMETRY_VERSION,
                        "observation_id": obs.datatake_id,
                        "item_ids": obs_items,
                        "asset_key": key,
                        "scale": meta.scale,
                        "offset": meta.offset,
                        "nodata": meta.nodata,
                        "metadata_source": meta.source,
                        "invalid_px": {"nodata": norm.n_nodata, "saturated": norm.n_saturated},
                    }
                )
            timings["radiometry"] += time.perf_counter() - t0

            # --- SCL mask (C4 §2) on G20, R1 to G10 ---
            t0 = time.perf_counter()
            m = masks.scl_mask(refl["scl"])
            q20 = m.valid
            q10, r1_step = resample.r1_nearest_20_to_10(q20, grid.g10)
            reason10 = np.repeat(np.repeat(m.reason, 2, axis=0), 2, axis=1)
            steps.append({**masks.masking_step(m), "observation_id": obs.datatake_id})
            steps.append({"kind": "resampling", **r1_step, "observation_id": obs.datatake_id, "purpose": "SCL mask to G10"})
            aoi_px_obs += aoi10_count
            for code, name in enumerate(masks.REASON_NAMES):
                reason_px[name] += int(((reason10 == code) & in_aoi10).sum())
            timings["mask"] += time.perf_counter() - t0

            # --- per-observation indices (C4 §4) ---
            t0 = time.perf_counter()

            def r(key: str, arr: GridArray, obs_id: str = obs.datatake_id) -> Reflectance:
                return Reflectance(key, obs_id, arr)

            green20, s_g = resample.r2_block_mean_10_to_20(refl["green"], grid.g20)
            nir20, s_n = resample.r2_block_mean_10_to_20(refl["nir"], grid.g20)
            for s, band in ((s_g, "green"), (s_n, "nir")):
                steps.append(
                    {
                        "kind": "resampling",
                        **s,
                        "observation_id": obs.datatake_id,
                        "asset_key": band,
                        "purpose": "MNDWI/NDBI on G20",
                    }
                )
            inputs = {
                "ndvi": (r("nir", refl["nir"]), r("red", refl["red"]), q10),
                "ndwi": (r("green", refl["green"]), r("nir", refl["nir"]), q10),
                "mndwi": (r("green", green20), r("swir16", refl["swir16"]), q20),
                "ndbi": (r("swir16", refl["swir16"]), r("nir", nir20), q20),
            }
            for name, (a, b, q) in inputs.items():
                spec = science.INDICES[name]
                io = compute_index(spec, a, b, q)
                per_index[name].append(io)
                steps.append(index_step(spec, obs.datatake_id, io.undefined_counts))
            rgb_inputs.append((r("red", refl["red"]), r("green", refl["green"]), r("blue", refl["blue"])))
            rgb_quality.append(q10)
            used.append(obs.datatake_id)
            if (per_index["ndvi"][-1].array.valid & in_aoi10).any():
                usable.append(obs.datatake_id)
            timings["indices"] += time.perf_counter() - t0
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    # --- composites (C4 §3) ---
    progress("composite", 0.0)
    if cancelled():
        raise Cancelled()
    t0 = time.perf_counter()
    composites: dict[str, IndexComposite] = {}
    for name in science.INDICES:
        if per_index[name]:
            c = median_index_composite(per_index[name], n_min)
            composites[name] = c
            steps.append(compositing_step(c))
    display = display_rgb_composite(rgb_inputs, rgb_quality) if rgb_inputs else None
    timings["composite"] += time.perf_counter() - t0

    return PeriodResult(
        in_aoi10=in_aoi10,
        in_aoi20=in_aoi20,
        observations_total=n_obs_total,
        observations_used=used,
        observations_usable=usable,
        composites=composites,
        display=display,
        mask_reason_px=reason_px,
        aoi_pixel_observations=aoi_px_obs,
        steps=steps,
        timings_s=dict(timings),
    )
