"""Execute one ``analysis_run`` job of type ``snapshot`` and persist it in one transaction.

Stages write artifacts to the object store first (content-addressed); then a single DB transaction
inserts the processing run, inputs, artifacts, evidence and measurements and marks the run and
job succeeded (ARCHITECTURE §6). A failure inserts nothing.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from shapely.geometry import shape
from sqlalchemy import Engine, text

from aether import science
from aether.db import repo
from aether.domain.errors import AetherError, InvariantViolation
from aether.domain.periods import Period
from aether.evidence.builders import EvidenceDraft, MeasurementDraft, canonical_hash
from aether.evidence.provenance import code_version, environment
from aether.geo.grid import AnalysisGrid, build_analysis_grid
from aether.geo.io.cog import write_cog
from aether.geo.io.windows import AssetReader
from aether.geo.snapshot import Cancelled, SnapshotResult, run_snapshot
from aether.geo.stac.client import SearchResult, StacSearcher
from aether.services.catalog import discover
from aether.storage.store import ObjectStore

log = logging.getLogger(__name__)

SCL_LIMITATIONS = ["scl_bright_surface_confusion", "scl_thin_cloud_omission"]


def execute(
    engine: Engine,
    job: dict[str, Any],
    *,
    searcher: StacSearcher,
    reader: AssetReader,
    store: ObjectStore,
    heartbeat: Any,
) -> str:
    run_id = uuid.UUID(job["payload"]["analysis_run_id"])
    started = datetime.now(UTC)
    with engine.connect() as conn:
        run = dict(
            conn.execute(
                text(
                    "SELECT id, project_id, aoi_id, type, lower(period_a) AS s, upper(period_a) - 1 AS e, parameters, grid_hash "
                    "FROM analysis_runs WHERE id=:id"
                ),
                {"id": run_id},
            )
            .mappings()
            .one()
        )
        aoi = repo.get_aoi(conn, run["aoi_id"])
    if run["type"] != "snapshot":
        raise AetherError("analysis_type_not_available", "only snapshot runs exist in Phase 1")
    period = Period(run["s"], run["e"])
    grid = build_analysis_grid(shape(aoi["geometry"]), aoi["utm_epsg"])
    if grid.grid_hash != run["grid_hash"]:
        raise InvariantViolation("grid_mismatch", "analysis grid differs from the one recorded at submission")
    aoi_ev = _aoi_area(engine, aoi)

    heartbeat("ingest", 0.0)
    params = run["parameters"]
    search, scenes = discover(searcher, aoi, period, float(params["max_tile_cloud"]))
    log.info("analysis.start run=%s items=%d period=%s", run_id, len(scenes), period.as_tuple())

    def progress(stage: str, frac: float) -> None:
        if heartbeat(stage, frac):
            raise Cancelled()

    result = run_snapshot(
        grid=grid, aoi_area_m2=aoi_ev, scenes=scenes, reader=reader, n_min=int(params["n_min"]), progress=progress
    )
    heartbeat("persist", 0.9)
    artifacts = _write_artifacts(result, store)
    _persist(engine, job, run, aoi, period, grid, search, result, artifacts, started)
    return result.coverage.outcome


def _aoi_area(engine: Engine, aoi: dict[str, Any]) -> float:
    with engine.connect() as conn:
        v = conn.execute(
            text(
                """SELECT m.value FROM measurements m JOIN evidence e ON e.evidence_id = m.evidence_id
               WHERE e.kind='aoi_measurement' AND e.facts->>'aoi_id' = :a AND m.quantity='aoi_area'
               ORDER BY e.created_at LIMIT 1"""
            ),
            {"a": str(aoi["id"])},
        ).scalar()
    if v is None:
        raise InvariantViolation("aoi_area_missing", "AOI has no aoi_measurement evidence")
    return float(v)


def _write_artifacts(result: SnapshotResult, store: ObjectStore) -> dict[str, dict[str, Any]]:
    """Write composite, n_valid and display rasters. Keys: ``index:<name>``, ``n_valid:<name>``, ``rgb``."""
    out: dict[str, dict[str, Any]] = {}

    def put(
        key: str,
        kind: str,
        grid: Any,
        bands: list[np.ndarray],
        dtype: str,
        nodata: Any,
        names: list[str],
        categorical: bool,
    ) -> None:
        tmp = store.tmp_path(".tif")
        meta = write_cog(tmp, grid, bands, dtype=dtype, nodata=nodata, band_names=names, categorical=categorical)
        ref = store.put_file(Path(tmp), "tif")
        out[key] = {"kind": kind, "sha256": ref.sha256, "uri": ref.uri, "bytes": ref.bytes, "raster": meta}

    for name, c in result.composites.items():
        put(f"index:{name}", "index", c.array.grid, [c.array.data], "float32", float("nan"), [name], False)
        put(f"n_valid:{name}", "n_valid", c.array.grid, [c.n_valid], "uint8", None, [f"n_valid_{name}"], True)
    if result.display is not None:
        d = result.display
        put(
            "rgb",
            "composite",
            d.arrays[0].grid,
            [a.data for a in d.arrays],
            "float32",
            float("nan"),
            ["red", "green", "blue"],
            False,
        )
    return out


def _mask_measurements(result: SnapshotResult, method: str) -> list[MeasurementDraft]:
    total = result.aoi_pixel_observations
    ms = [MeasurementDraft("aoi_pixel_observations", "count", total, method, n_samples=total)]
    for reason, n in result.mask_reason_px.items():
        ms.append(
            MeasurementDraft(
                f"masked_fraction.{reason}",
                "fraction",
                n / total if total else None,
                science.MASK_VERSION + "@1",
                unavailable_reason=None if total else "insufficient_observations",
                denominator="aoi_pixel_observations",
                n_samples=n,
            )
        )
    return ms


def _coverage_evidence(result: SnapshotResult, aoi: dict[str, Any], period: Period, area_m2: float) -> EvidenceDraft:
    c = result.coverage
    pm = science.PIXEL_AREA_METHOD
    cm = f"{science.COVERAGE_VERSION}@1"
    used = [s for s in result.scenes if s.status == "used"]
    ms = [
        MeasurementDraft("aoi_area", "value", area_m2, science.GEODESIC_AREA_METHOD),
        MeasurementDraft("aoi_grid_pixels_10m", "count", c.aoi_grid_px_10m, pm, n_samples=c.aoi_grid_px_10m),
        MeasurementDraft("aoi_grid_pixels_20m", "count", c.aoi_grid_px_20m, pm, n_samples=c.aoi_grid_px_20m),
        MeasurementDraft("aoi_grid_area", "value", c.aoi_grid_area_m2, pm, n_samples=c.aoi_grid_px_10m),
        MeasurementDraft("grid_vs_vector_area_ratio", "fraction", c.aoi_grid_area_m2 / area_m2, pm, denominator="aoi_area"),
        MeasurementDraft("observed_area_a", "value", c.observed_area_m2, cm, n_samples=c.observed_px),
        MeasurementDraft("valid_area_a", "value", c.valid_area_m2, cm, n_samples=c.valid_px),
        MeasurementDraft(
            "coverage_a",
            "fraction",
            c.coverage_a,
            cm,
            denominator="aoi_grid_area",
            unavailable_reason=None if c.coverage_a is not None else "not_applicable",
            n_samples=c.valid_px,
        ),
        MeasurementDraft("observations_total", "count", c.observations_total, cm),
        MeasurementDraft("observations_usable", "count", c.observations_usable, cm),
        MeasurementDraft("items_considered", "count", len(result.scenes), f"{science.INGEST_VERSION}@1"),
        MeasurementDraft("items_used", "count", len(used), f"{science.INGEST_VERSION}@1"),
    ]
    reasons: dict[str, int] = {}
    for s in result.scenes:
        if s.status == "excluded" and s.reason_code:
            reasons[s.reason_code] = reasons.get(s.reason_code, 0) + 1
    for r, n in sorted(reasons.items()):
        ms.append(MeasurementDraft(f"items_excluded.{r}", "count", n, f"{science.INGEST_VERSION}@1"))
    ms.extend(_mask_measurements(result, cm))
    return EvidenceDraft(
        kind="coverage_statistic",
        facts={
            "outcome": c.outcome,
            "run_type": "snapshot",
            "coverage_index": "ndvi",
            "period_a": list(period.as_tuple()),
            "observation_ids": result.observations_used,
            "usable_observation_ids": result.observations_usable,
            "item_ids_used": sorted(s.item_id for s in used),
            "platforms": sorted({s.platform for s in used}),
            "processing_baselines": sorted({s.processing_baseline or "" for s in used}),
            "mask_profile": science.MASK_VERSION,
            "composite": science.COMPOSITE_VERSION,
            "grid_hash": result.grid.grid_hash,
            "epsg": result.grid.epsg,
            "denominators": {
                "coverage_a": "aoi_grid_area",
                "masked_fraction": "aoi_pixel_observations",
                "grid_vs_vector_area_ratio": "aoi_area",
            },
        },
        measurements=ms,
        limitations=[*c.limitations, *SCL_LIMITATIONS],
        geometry=aoi["geometry"],
        observed_period_a=period.as_tuple(),
        artifact_keys=["n_valid:ndvi"] if "ndvi" in result.composites else [],
    )


def _index_evidence(result: SnapshotResult, name: str, aoi: dict[str, Any], period: Period) -> EvidenceDraft:
    spec = science.INDICES[name]
    s = result.stats[name]
    method = science.ZONAL_STATS_METHOD
    has_comp = name in result.composites
    ms = [
        MeasurementDraft(f"index_region_pixels.{name}", "count", s.region_pixels, method, n_samples=s.region_pixels),
        MeasurementDraft(
            f"index_pixels.{name}",
            "count",
            s.n_pixels if has_comp else None,
            method,
            unavailable_reason=None if has_comp else s.unavailable_reason,
            n_samples=s.n_pixels,
        ),
        MeasurementDraft(
            f"index_support.{name}",
            "fraction",
            s.support_fraction if has_comp else None,
            method,
            unavailable_reason=None
            if (has_comp and s.support_fraction is not None)
            else (s.unavailable_reason or "not_applicable"),
            denominator=f"index_region_pixels.{name}",
            n_samples=s.n_pixels,
        ),
    ]
    for stat in ("mean", "median", "p10", "p90"):
        v = getattr(s, stat)
        ms.append(
            MeasurementDraft(
                f"index.{name}",
                stat,
                v,
                method,
                unavailable_reason=None if v is not None else s.unavailable_reason,
                n_samples=s.n_pixels if v is not None else None,
            )
        )
    lims = [*result.coverage.limitations, *SCL_LIMITATIONS]
    if spec.grid == "G20":
        lims.append("index_resolution_20m")
    if name == "ndbi":
        lims.append("ndbi_not_builtup_detector")
    return EvidenceDraft(
        kind="index_statistic",
        facts={
            "index": name,
            "formula": spec.formula,
            "bands": {"a": spec.a, "b": spec.b},
            "grid": spec.grid,
            "typical_correlate": spec.correlate,
            "indices_version": science.INDICES_VERSION,
            "composite": science.COMPOSITE_VERSION,
            "composite_strategy": "per_observation_index_then_temporal_median",
            "mask_profile": science.MASK_VERSION,
            "radiometry": science.RADIOMETRY_VERSION,
            "region": "aoi (pixel-centre rule)",
            "period_a": list(period.as_tuple()),
            "observation_ids": list(result.composites[name].observation_ids) if has_comp else [],
            "outcome": result.coverage.outcome,
            "grid_hash": result.grid.grid_hash,
            "epsg": result.grid.epsg,
            "percentile_method": "linear",
        },
        measurements=ms,
        limitations=lims,
        geometry=aoi["geometry"],
        observed_period_a=period.as_tuple(),
        artifact_keys=[f"index:{name}", f"n_valid:{name}"] if has_comp else [],
    )


def _persist(
    engine: Engine,
    job: dict[str, Any],
    run: dict[str, Any],
    aoi: dict[str, Any],
    period: Period,
    grid: AnalysisGrid,
    search: SearchResult,
    result: SnapshotResult,
    artifacts: dict[str, dict[str, Any]],
    started: datetime,
) -> None:
    params = run["parameters"]
    ph = canonical_hash(params)
    steps = [
        {
            "kind": "catalog_search",
            "api_url": search.api_url,
            "collection": search.collection,
            "request": search.request,
            "item_ids": [s.item_id for s in result.scenes],
        },
        *result.steps,
    ]
    if "rgb" in artifacts:
        # Content addressing deduplicates identical rasters across runs, so a run's display layer is
        # located by this step, never by "first producer" (artifacts.produced_by_run_id).
        steps.append(
            {
                "kind": "display_composite",
                "purpose": "display_rgb",
                "bands": ["red", "green", "blue"],
                "statistic": "median",
                "artifact_sha256": artifacts["rgb"]["sha256"],
            }
        )
    with engine.begin() as conn:
        pr = repo.insert_processing_run(
            conn,
            project_id=run["project_id"],
            analysis_run_id=run["id"],
            kind="analysis",
            parameters=params,
            parameters_hash=ph,
            grid_hash=grid.grid_hash,
            code_version=code_version(),
            environment=environment(),
            steps=steps,
            timings={k: round(v, 3) for k, v in result.timings_s.items()},
            started_at=started,
            algorithm_versions=science.SNAPSHOT_ALGORITHM_VERSIONS,
        )
        for s in result.scenes:
            sid, _ = repo.upsert_scene(conn, s)
            conn.execute(
                text(
                    """INSERT INTO analysis_run_inputs (analysis_run_id, scene_id, period, datatake_id, status, reason_code)
                   VALUES (:r, :s, 'a', :dt, :st, :rc)"""
                ),
                {"r": run["id"], "s": sid, "dt": s.datatake_id, "st": s.status, "rc": s.reason_code},
            )
        art_ids = {
            key: repo.insert_artifact(
                conn,
                run_id=pr,
                sha256=a["sha256"],
                uri=a["uri"],
                media_type="image/tiff; application=geotiff; profile=cloud-optimized",
                nbytes=a["bytes"],
                kind=a["kind"],
                raster=a["raster"],
            )
            for key, a in artifacts.items()
        }
        drafts = [_coverage_evidence(result, aoi, period, result.coverage.aoi_area_m2)]
        drafts += [_index_evidence(result, n, aoi, period) for n in science.INDICES]
        for ev in drafts:
            repo.insert_evidence(
                conn,
                ev,
                project_id=run["project_id"],
                analysis_run_id=run["id"],
                processing_run_id=pr,
                parameters_hash=ph,
                artifact_ids=art_ids,
            )
        conn.execute(
            text("UPDATE analysis_runs SET status='succeeded', outcome=:o, completed_at=now() WHERE id=:id"),
            {"o": result.coverage.outcome, "id": run["id"]},
        )
        conn.execute(
            text("UPDATE jobs SET status='succeeded', stage='done', progress=1, finished_at=now(), error=NULL WHERE id=:id"),
            {"id": job["id"]},
        )
        conn.execute(
            text("SELECT pg_notify('aether_events', :m)"),
            {"m": json.dumps({"job_id": str(job["id"]), "status": "succeeded", "outcome": result.coverage.outcome})},
        )
