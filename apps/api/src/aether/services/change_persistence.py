"""Submit and persist two-period change runs (implementation plan §8–§11; C5 §10; C6 §5.1; C-03/C-16/C-17/C-18).

``submit_change`` creates the ``analysis_runs`` row (type ``change``) and its job, keyed by the Stage 4
change identity. ``execute`` runs the self-contained change pipeline
(:func:`aether.services.change_run.execute_change`: discover A and B, process each with the shared
per-period engine, compare), writes the artifacts to the content-addressed store, and persists
everything in **one** transaction:

- one processing run with ``CHANGE_ALGORITHM_VERSIONS`` and every period-tagged step (C-16);
- ``analysis_run_inputs`` for period ``a`` and ``b`` (no reference to any snapshot run, C-07);
- artifacts: per period index / n_valid / RGB / unfilled NIR QA composite; for completed runs also
  ``delta_<I>`` (native grid), ``z_<I>`` (robust mode only), the pre-morphology ``category`` raster,
  the 6-band ``event_label`` raster (C-17), ``joint_valid`` and the authoritative ``change_events.gpkg``;
- evidence: ``coverage_statistic``, 4 + 4 per-period ``index_statistic``, ``coregistration_check``
  (always, C-03), and for completed runs 3 delta ``index_statistic`` (C-06), one ``change_event``
  per retained event (+ a ``change_events`` row) and one ``change_summary``.

Early stops (C-03 steps 1–2) and ``coregistration_failed`` persist coverage, per-period index evidence
and the co-registration check, and no change artifacts or change evidence.

**Zero-event completed runs** write and register an *empty* ``change_events.gpkg``. C5 §10 lists the
GeoPackage among the outputs of ``change.index_diff.v1`` without an event-count condition, and the
outcome ``completed`` explicitly includes zero events; the artifact set of a completed run is therefore
the same whether or not events were found.

The co-registration estimator's median-filled copy never leaves ``aether.geo.coreg``; only scalars
and the unfilled QA composites are persisted.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np
from shapely.geometry import mapping, shape
from sqlalchemy import Connection, Engine, text

from aether import science
from aether.db import repo
from aether.domain.errors import AetherError, InvariantViolation
from aether.domain.periods import Period, validate_pair
from aether.domain.registries import MASK_REASONS
from aether.evidence.builders import EvidenceDraft, MeasurementDraft, canonical_hash
from aether.evidence.provenance import code_version, environment
from aether.geo.change.events import ChangeEvent
from aether.geo.change.rules import CATEGORIES
from aether.geo.grid import Grid, build_analysis_grid
from aether.geo.io.cog import write_cog
from aether.geo.io.vector import MEDIA_TYPE as GPKG_MEDIA_TYPE
from aether.geo.io.vector import VectorArtifact, features_from_events, write_change_events_gpkg
from aether.geo.io.windows import AssetReader
from aether.geo.period import Cancelled
from aether.geo.stac.client import StacSearcher
from aether.jobs import queue
from aether.services.change_run import ChangeRunRequest, ChangeRunResult, change_idempotency_key, execute_change
from aether.services.snapshot_run import SCL_LIMITATIONS, _aoi_area
from aether.storage.store import ObjectStore

log = logging.getLogger(__name__)

MAX_QUEUED_PER_PROJECT = 10  # Security §3 (same limit as snapshot submissions)
COG_MEDIA_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
PERIODS = ("a", "b")
# Blocking run-level limitations that qualify every change product (C3 §9, C5 §4, C6 §5.1).
CHANGE_BLOCKING = ("partial_coverage", "coregistration_not_verified", "widespread_change", "robust_reference_unavailable")


# --- submission -------------------------------------------------------------------------------------


def submit_change(conn: Connection, *, aoi_id: uuid.UUID, period_a: Period, period_b: Period) -> tuple[dict[str, Any], bool]:
    """Create (or return the existing) change run + job. Identity = the Stage 4 change key (C-10)."""
    validate_pair(period_a, period_b)
    aoi = repo.get_aoi(conn, aoi_id)
    grid = build_analysis_grid(shape(aoi["geometry"]), aoi["utm_epsg"])
    params = science.change_parameters()
    key = change_idempotency_key(aoi, period_a, period_b)

    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
    existing = (
        conn.execute(
            text(
                """SELECT r.id, r.status, j.id AS job_id FROM analysis_runs r LEFT JOIN jobs j ON j.analysis_run_id = r.id
           WHERE r.idempotency_key=:k AND r.status IN ('queued','running','succeeded')
           ORDER BY j.queued_at DESC NULLS LAST LIMIT 1"""
            ),
            {"k": key},
        )
        .mappings()
        .first()
    )
    if existing is not None:
        return {
            "analysis_run_id": existing["id"],
            "job_id": existing["job_id"],
            "status": existing["status"],
            "cached": existing["status"] == "succeeded",
        }, False

    queued = conn.execute(
        text("SELECT count(*) FROM jobs WHERE project_id=:p AND status='queued'"), {"p": aoi["project_id"]}
    ).scalar_one()
    if queued >= MAX_QUEUED_PER_PROJECT:
        raise AetherError("too_many_queued_jobs", "at most 10 queued analyses per project", http_status=429)

    run_id = conn.execute(
        text(
            """INSERT INTO analysis_runs (project_id, aoi_id, type, period_a, period_b, collection, parameters,
             parameters_hash, algorithm_versions, grid_hash, idempotency_key, status)
           VALUES (:p, :a, 'change', CAST(:pa AS daterange), CAST(:pb AS daterange), :c, CAST(:par AS jsonb), :ph,
             CAST(:av AS jsonb), :gh, :k, 'queued') RETURNING id"""
        ),
        {
            "p": aoi["project_id"],
            "a": aoi_id,
            "pa": f"[{period_a.start},{period_a.end}]",
            "pb": f"[{period_b.start},{period_b.end}]",
            "c": science.STAC_COLLECTION,
            "par": _json(params),
            "ph": canonical_hash(params),
            "av": _json(science.CHANGE_ALGORITHM_VERSIONS),
            "gh": grid.grid_hash,
            "k": key,
        },
    ).scalar_one()
    job_id = queue.enqueue(
        conn,
        kind="analysis_run",
        project_id=aoi["project_id"],
        analysis_run_id=run_id,
        idempotency_key=key,
        payload={"analysis_run_id": str(run_id)},
    )
    return {"analysis_run_id": run_id, "job_id": job_id, "status": "queued", "cached": False}, True


def _json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# --- execution ----------------------------------------------------------------------------------------


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
                    """SELECT id, project_id, aoi_id, type, lower(period_a) AS a_s, upper(period_a) - 1 AS a_e,
                      lower(period_b) AS b_s, upper(period_b) - 1 AS b_e, parameters, grid_hash, idempotency_key,
                      algorithm_versions
                   FROM analysis_runs WHERE id=:id"""
                ),
                {"id": run_id},
            )
            .mappings()
            .one()
        )
        aoi = repo.get_aoi(conn, run["aoi_id"])
    if run["type"] != "change":
        raise InvariantViolation("analysis_type_mismatch", "change execution received a non-change run")
    if run["parameters"] != science.change_parameters() or run["algorithm_versions"] != science.CHANGE_ALGORITHM_VERSIONS:
        raise InvariantViolation("change_identity_mismatch", "run was submitted with other parameters or versions")
    request = ChangeRunRequest(aoi, Period(run["a_s"], run["a_e"]), Period(run["b_s"], run["b_e"]))

    def progress(stage: str, frac: float) -> None:
        if heartbeat(stage, frac):
            raise Cancelled()

    heartbeat("ingest", 0.0)
    result = execute_change(request, searcher=searcher, reader=reader, progress=progress)
    if result.idempotency_key != run["idempotency_key"]:
        raise InvariantViolation("change_identity_mismatch", "computed change key differs from the submitted key")
    if result.analysis.grid.grid_hash != run["grid_hash"]:
        raise InvariantViolation("grid_mismatch", "analysis grid differs from the one recorded at submission")
    aoi_area = _aoi_area(engine, aoi)
    heartbeat("persist", 0.9)
    artifacts, vector = _write_artifacts(result, store)
    _persist(engine, job, run, aoi, result, artifacts, vector, aoi_area, started)
    return result.outcome


# --- artifacts ---------------------------------------------------------------------------------------


def _write_artifacts(result: ChangeRunResult, store: ObjectStore) -> tuple[dict[str, dict[str, Any]], VectorArtifact | None]:
    out: dict[str, dict[str, Any]] = {}
    a = result.analysis

    def put(
        key: str, kind: str, grid: Grid, bands: list[np.ndarray], dtype: str, nodata: Any, names: list[str], cat: bool
    ) -> None:
        tmp = store.tmp_path(".tif")
        meta = write_cog(tmp, grid, bands, dtype=dtype, nodata=nodata, band_names=names, categorical=cat)
        ref = store.put_file(Path(tmp), "tif")
        out[key] = {
            "kind": kind,
            "sha256": ref.sha256,
            "uri": ref.uri,
            "bytes": ref.bytes,
            "media_type": COG_MEDIA_TYPE,
            "raster": meta,
        }

    for p, summary in (("a", a.period_a), ("b", a.period_b)):
        pr = summary.result
        for name, c in pr.composites.items():
            put(f"index:{name}.{p}", "index", c.array.grid, [c.array.data], "float32", float("nan"), [name], False)
            put(f"n_valid:{name}.{p}", "n_valid", c.array.grid, [c.n_valid], "uint8", None, [f"n_valid_{name}"], True)
        if pr.display is not None:
            d = pr.display
            put(
                f"rgb.{p}",
                "composite",
                d.arrays[0].grid,
                [x.data for x in d.arrays],
                "float32",
                float("nan"),
                ["red", "green", "blue"],
                False,
            )
        if pr.qa is not None:  # unfilled NIR QA composite: co-registration input, never a measurement
            put(f"qa_nir.{p}", "composite", pr.qa.array.grid, [pr.qa.array.data], "float32", float("nan"), ["nir_qa"], False)

    if a.outcome != "completed" or a.change is None or a.joint is None:
        return out, None
    ch = a.change
    g10 = a.grid.g10
    for name, d in ch.deltas.items():  # Δ on the index's native grid (C5 §3), float64 as measured
        put(f"delta:{name}", "delta", d.native.grid, [d.native.data], "float64", float("nan"), [f"delta_{name}"], False)
    if ch.reference.z is not None:
        for name in science.CHANGE_INDICES:
            put(f"z:{name}", "z", g10, [ch.reference.z[name]], "float64", float("nan"), [f"z_{name}"], False)
    put("category", "category", g10, [ch.categories.data], "uint8", None, ["category"], True)
    put(
        "event_label",
        "event_label",
        g10,
        [b.data for b in ch.events.event_label_bands],
        "int32",
        None,
        [f"event_label_{CATEGORIES[code]}" for code in sorted(CATEGORIES)],
        True,
    )
    put("joint_valid", "joint_valid", g10, [a.joint.mask.astype(np.uint8)], "uint8", None, ["joint_valid"], True)

    tmp = store.tmp_path(".gpkg")
    vector = write_change_events_gpkg(tmp, features_from_events(ch.events.events), epsg=a.grid.epsg)
    ref = store.put_file(Path(tmp), "gpkg")
    if ref.sha256 != vector.sha256 or ref.bytes != vector.bytes:
        raise InvariantViolation("vector_artifact_mismatch", "stored GeoPackage differs from the written file")
    out["vector"] = {
        "kind": "vector_gpkg",
        "sha256": ref.sha256,
        "uri": ref.uri,
        "bytes": ref.bytes,
        "media_type": GPKG_MEDIA_TYPE,
        "raster": {"crs_epsg": vector.crs_epsg},
    }
    return out, vector


# --- evidence ----------------------------------------------------------------------------------------


def _period_tuple(p: Period) -> tuple[str, str]:
    return p.as_tuple()


def _unavail(q: str, stat: str, method: str, reason: str, **kw: Any) -> MeasurementDraft:
    return MeasurementDraft(q, stat, None, method, unavailable_reason=reason, **kw)


def _coverage_evidence(result: ChangeRunResult, aoi: dict[str, Any], area_m2: float) -> EvidenceDraft:
    a = result.analysis
    pa, pb = a.period_a, a.period_b
    px_area = a.pixel_area_true_m2
    pm, cm, im = science.PIXEL_AREA_METHOD, f"{science.COVERAGE_VERSION}@1", f"{science.INGEST_VERSION}@1"
    aoi10 = pa.aoi_grid_px
    aoi20 = int(pa.result.in_aoi20.sum())
    grid_area = aoi10 * px_area
    ms = [
        MeasurementDraft("aoi_area", "value", area_m2, science.GEODESIC_AREA_METHOD),
        MeasurementDraft("aoi_grid_pixels_10m", "count", aoi10, pm, n_samples=aoi10),
        MeasurementDraft("aoi_grid_pixels_20m", "count", aoi20, pm, n_samples=aoi20),
        MeasurementDraft("aoi_grid_area", "value", grid_area, pm, n_samples=aoi10),
        MeasurementDraft("grid_vs_vector_area_ratio", "fraction", grid_area / area_m2, pm, denominator="aoi_area"),
    ]
    for p, s in (("a", pa), ("b", pb)):
        ms += [
            MeasurementDraft(f"observed_area_{p}", "value", s.observed_px * px_area, cm, n_samples=s.observed_px),
            MeasurementDraft(f"valid_area_{p}", "value", s.valid_px * px_area, cm, n_samples=s.valid_px),
            MeasurementDraft(f"coverage_{p}", "fraction", s.coverage, cm, denominator="aoi_grid_area", n_samples=s.valid_px)
            if s.coverage is not None
            else _unavail(f"coverage_{p}", "fraction", cm, "not_applicable", denominator="aoi_grid_area"),
            MeasurementDraft(f"observations_total.{p}", "count", s.result.observations_total, cm),
            MeasurementDraft(f"observations_usable.{p}", "count", s.observations_usable, cm),
        ]
        reasons: dict[str, int] = {}
        for sc in s.scenes:
            if sc.status == "excluded" and sc.reason_code:
                reasons[sc.reason_code] = reasons.get(sc.reason_code, 0) + 1
        for r, n in sorted(reasons.items()):
            ms.append(MeasurementDraft(f"items_excluded.{r}.{p}", "count", n, im))
        total = s.result.aoi_pixel_observations
        ms.append(MeasurementDraft(f"aoi_pixel_observations.{p}", "count", total, cm, n_samples=total))
        for reason in MASK_REASONS:
            n = s.result.mask_reason_px.get(reason, 0)
            ms.append(
                MeasurementDraft(
                    f"masked_fraction.{reason}.{p}",
                    "fraction",
                    n / total if total else None,
                    science.MASK_VERSION + "@1",
                    unavailable_reason=None if total else "insufficient_observations",
                    denominator=f"aoi_pixel_observations.{p}",
                    n_samples=n,
                )
            )
    if a.joint is not None:
        j = a.joint
        ms.append(MeasurementDraft("joint_valid_area", "value", j.joint_valid_px * px_area, cm, n_samples=j.joint_valid_px))
        ms.append(
            MeasurementDraft(
                "joint_coverage", "fraction", j.joint_coverage, cm, denominator="aoi_grid_area", n_samples=j.joint_valid_px
            )
            if j.joint_coverage is not None
            else _unavail("joint_coverage", "fraction", cm, "not_applicable", denominator="aoi_grid_area")
        )
    else:  # a period has no change-index composites: nothing to compare (C-03 step 1)
        ms.append(_unavail("joint_valid_area", "value", cm, "insufficient_observations"))
        ms.append(_unavail("joint_coverage", "fraction", cm, "insufficient_observations", denominator="aoi_grid_area"))

    def used(s: Any) -> list[str]:
        return sorted(x.item_id for x in s.scenes if x.status == "used")

    keys = [f"n_valid:ndvi.{p}" for p, s in (("a", pa), ("b", pb)) if "ndvi" in s.result.composites]
    if a.outcome == "completed":
        keys.append("joint_valid")
    return EvidenceDraft(
        kind="coverage_statistic",
        facts={
            "outcome": a.outcome,
            "run_type": "change",
            "coverage_index": "ndvi",
            "period_a": list(_period_tuple(result.request.period_a)),
            "period_b": list(_period_tuple(result.request.period_b)),
            "observation_ids": {"a": pa.result.observations_used, "b": pb.result.observations_used},
            "usable_observation_ids": {"a": pa.result.observations_usable, "b": pb.result.observations_usable},
            "item_ids_used": {"a": used(pa), "b": used(pb)},
            "mask_profile": science.MASK_VERSION,
            "composite": science.COMPOSITE_VERSION,
            "grid_hash": a.grid.grid_hash,
            "epsg": a.grid.epsg,
            "denominators": {
                "coverage_a": "aoi_grid_area",
                "coverage_b": "aoi_grid_area",
                "joint_coverage": "aoi_grid_area",
                "masked_fraction": "aoi_pixel_observations.<period>",
                "grid_vs_vector_area_ratio": "aoi_area",
            },
        },
        measurements=ms,
        limitations=[*(["partial_coverage"] if "partial_coverage" in a.limitations else []), *SCL_LIMITATIONS],
        geometry=aoi["geometry"],
        observed_period_a=_period_tuple(result.request.period_a),
        observed_period_b=_period_tuple(result.request.period_b),
        artifact_keys=keys,
    )


def _index_limitations(name: str) -> list[str]:
    lims = list(SCL_LIMITATIONS)
    if science.INDICES[name].grid == "G20":
        lims.append("index_resolution_20m")
    if name == "ndbi":
        lims.append("ndbi_not_builtup_detector")
    return lims


def _period_index_evidence(result: ChangeRunResult, name: str, p: str, aoi: dict[str, Any]) -> EvidenceDraft:
    a = result.analysis
    summary = a.period_a if p == "a" else a.period_b
    period = result.request.period_a if p == "a" else result.request.period_b
    spec = science.INDICES[name]
    s = summary.stats[name]
    method = science.ZONAL_STATS_METHOD
    has = name in summary.result.composites
    ms = [
        MeasurementDraft(f"index_region_pixels.{name}", "count", s.region_pixels, method, n_samples=s.region_pixels),
        MeasurementDraft(
            f"index_pixels.{name}",
            "count",
            s.n_pixels if has else None,
            method,
            unavailable_reason=None if has else s.unavailable_reason,
            n_samples=s.n_pixels,
        ),
        MeasurementDraft(
            f"index_support.{name}",
            "fraction",
            s.support_fraction if has else None,
            method,
            unavailable_reason=None if (has and s.support_fraction is not None) else (s.unavailable_reason or "not_applicable"),
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
    return EvidenceDraft(
        kind="index_statistic",
        facts={
            "index": name,
            "period": p,
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
            "observation_ids": list(summary.result.composites[name].observation_ids) if has else [],
            "outcome": a.outcome,
            "grid_hash": a.grid.grid_hash,
            "epsg": a.grid.epsg,
            "percentile_method": "linear",
        },
        measurements=ms,
        limitations=_index_limitations(name),
        geometry=aoi["geometry"],
        observed_period_a=_period_tuple(period) if p == "a" else None,
        observed_period_b=_period_tuple(period) if p == "b" else None,
        artifact_keys=[f"index:{name}.{p}", f"n_valid:{name}.{p}"] if has else [],
    )


def _run_blocking(result: ChangeRunResult, codes: tuple[str, ...]) -> list[str]:
    return [c for c in codes if c in result.analysis.limitations]


def _delta_evidence(result: ChangeRunResult, name: str, aoi: dict[str, Any]) -> EvidenceDraft:
    a = result.analysis
    if a.change is None or a.joint is None:
        raise InvariantViolation("change_evidence_incomplete", "delta evidence exists only for completed runs")
    s = a.change.delta_stats[name]
    method = science.ZONAL_STATS_METHOD
    aoi10 = a.joint.aoi_grid_px
    ms = [
        MeasurementDraft("aoi_grid_pixels_10m", "count", aoi10, science.PIXEL_AREA_METHOD, n_samples=aoi10),
        MeasurementDraft(f"index_delta_pixels.{name}", "count", s.n_pixels, method, n_samples=s.n_pixels),
        MeasurementDraft(
            f"index_delta_support.{name}",
            "fraction",
            s.support_fraction,
            method,
            unavailable_reason=None if s.support_fraction is not None else "not_applicable",
            denominator="aoi_grid_pixels_10m",
            n_samples=s.n_pixels,
        ),
    ]
    for stat in ("mean", "median", "p10", "p90"):
        v = getattr(s, stat)
        ms.append(
            MeasurementDraft(
                f"index_delta.{name}",
                stat,
                v,
                method,
                unavailable_reason=None if v is not None else s.unavailable_reason,
                n_samples=s.n_pixels if v is not None else None,
            )
        )
    spec = science.INDICES[name]
    return EvidenceDraft(
        kind="index_statistic",
        facts={
            "index": name,
            "period": "delta",
            "delta": "B - A",
            "sample_set": "joint_valid",
            "grid": spec.grid,
            "sample_grid": "G10",
            "replication": "R3 nearest 20 m → 10 m" if spec.grid == "G20" else "none",
            "formula": spec.formula,
            "change_version": science.CHANGE_VERSION,
            "outcome": a.outcome,
            "grid_hash": a.grid.grid_hash,
            "epsg": a.grid.epsg,
            "percentile_method": "linear",
            "denominators": {f"index_delta_support.{name}": "aoi_grid_pixels_10m"},
        },
        measurements=ms,
        limitations=[*_index_limitations(name), *_run_blocking(result, ("partial_coverage", "coregistration_not_verified"))],
        geometry=aoi["geometry"],
        observed_period_a=_period_tuple(result.request.period_a),
        observed_period_b=_period_tuple(result.request.period_b),
        artifact_keys=[f"delta:{name}", "joint_valid"],
    )


def _coreg_evidence(result: ChangeRunResult, aoi: dict[str, Any], artifacts: dict[str, dict[str, Any]]) -> EvidenceDraft:
    """C3 §9 / C-01–C-03: always produced. Unavailable reason = the run outcome when not run (early stop),
    ``insufficient_support`` when not assessable."""
    a = result.analysis
    c = a.coregistration
    method = f"{science.COREG_VERSION}@1"
    reason = a.coregistration_unavailable_reason
    g10 = a.grid.g10
    grid_px = g10.width * g10.height

    def scalar(q: str, v: float | None, stat: str = "value") -> MeasurementDraft:
        if v is None:
            return _unavail(q, stat, method, reason or "not_applicable")
        return MeasurementDraft(q, stat, float(v), method)

    ms = [
        MeasurementDraft("grid_pixels_10m", "count", grid_px, science.PIXEL_AREA_METHOD, n_samples=grid_px),
        scalar("coreg_residual", c.residual_px if c else None),
        scalar("coreg_shift_x", c.shift_dx_px if c else None),
        scalar("coreg_shift_y", c.shift_dy_px if c else None),
        scalar("coreg_pcc_error", c.pcc_error if c else None),
        scalar("coreg_qa_nir_std.a", c.nir_qa_std_a if c else None),
        scalar("coreg_qa_nir_std.b", c.nir_qa_std_b if c else None),
    ]
    support = c.support if c else None
    shape_ = support.window_shape if support else None
    ms.append(scalar("coreg_window_width", shape_[1] if shape_ else None))
    ms.append(scalar("coreg_window_height", shape_[0] if shape_ else None))
    ms.append(
        MeasurementDraft(
            "coreg_grid_joint_valid_fraction",
            "fraction",
            support.grid_fraction,
            method,
            denominator="grid_pixels_10m",
            n_samples=support.joint_valid_px,
        )
        if support
        else _unavail(
            "coreg_grid_joint_valid_fraction", "fraction", method, reason or "not_applicable", denominator="grid_pixels_10m"
        )
    )
    window = support.window if support else None
    return EvidenceDraft(
        kind="coregistration_check",
        facts={
            "method": science.COREG_VERSION,
            "estimator": "skimage.registration.phase_cross_correlation (upsampled)",
            "status": a.coregistration_status,
            "support_reason": support.reason if support and support.reason else "none",
            "window": f"rows {window[0]}:{window[1]}, cols {window[2]}:{window[3]} (G10)" if window else "none",
            "shift_convention": "shift that registers B onto A; x = columns, y = rows, in G10 pixels",
            "qa_composite": "unfilled per-band NIR median (C4 §3); the estimator fill is never persisted (C-01)",
            "gate": "residual > max_residual_px → coregistration_failed; not assessable → coregistration_not_verified",
            "outcome": a.outcome,
            "grid_hash": a.grid.grid_hash,
            "epsg": a.grid.epsg,
        },
        measurements=ms,
        limitations=[],
        geometry=aoi["geometry"],
        observed_period_a=_period_tuple(result.request.period_a),
        observed_period_b=_period_tuple(result.request.period_b),
        artifact_keys=[k for k in ("qa_nir.a", "qa_nir.b") if k in artifacts],
    )


def _event_evidence(result: ChangeRunResult, e: ChangeEvent) -> EvidenceDraft:
    a = result.analysis
    if a.change is None:
        raise InvariantViolation("change_evidence_incomplete", "event evidence exists only for completed runs")
    method = f"{science.CHANGE_VERSION}@1"
    ms = [
        MeasurementDraft("event_area", "value", e.area_m2, method, region="event", n_samples=e.pixel_count),
        MeasurementDraft("event_pixel_count", "count", e.pixel_count, method, region="event", n_samples=e.pixel_count),
        *(
            MeasurementDraft(
                f"event_median_delta.{i}", "median", e.median_delta[i], method, region="event", n_samples=e.pixel_count
            )
            for i in science.CHANGE_INDICES
        ),
        MeasurementDraft("event_strength", "median", e.strength, method, region="event", n_samples=e.pixel_count)
        if e.strength is not None
        else _unavail(
            "event_strength", "median", method, e.strength_unavailable_reason or "robust_reference_unavailable", region="event"
        ),
        MeasurementDraft("event_support_a", "median", e.support_a, method, region="event", n_samples=e.pixel_count),
        MeasurementDraft("event_support_b", "median", e.support_b, method, region="event", n_samples=e.pixel_count),
    ]
    for i in science.CHANGE_INDICES:  # C-18: positive/negative counts; zero = event_pixel_count − both (derived)
        for sign in ("positive", "negative"):
            ms.append(MeasurementDraft(f"event_flag_pixels.{i}.{sign}", "count", e.flag_pixels[i][sign], method, region="event"))
    limitations = list(dict.fromkeys([*e.limitations, *_run_blocking(result, CHANGE_BLOCKING)]))
    return EvidenceDraft(
        kind="change_event",
        facts={
            "event_label": e.event_label,
            "category": e.category,
            "index_flags": {i: list(e.index_flags[i]) for i in science.CHANGE_INDICES},
            "mode": a.change.reference.mode,
            "touches_aoi_boundary": e.touches_aoi_boundary,
            "category_note": "typical-correlate label of an index change, not a land-cover or object claim",
            "confidence_band_note": "rule-based data-quality indicator (C5 §8), not a calibrated probability",
            "geometry_note": "EPSG:4326 copy; authoritative pixel-edge UTM geometry is change_events.gpkg (FID = event_label)",
            "change_version": science.CHANGE_VERSION,
            "grid_hash": a.grid.grid_hash,
            "epsg": a.grid.epsg,
        },
        measurements=ms,
        limitations=limitations,
        geometry=mapping(e.geometry.geom_4326),
        observed_period_a=_period_tuple(result.request.period_a),
        observed_period_b=_period_tuple(result.request.period_b),
        artifact_keys=["event_label", "category"],
        confidence_band=e.confidence_band,
        geometry_artifact_key="vector",
    )


def _summary_evidence(result: ChangeRunResult, aoi: dict[str, Any], artifacts: dict[str, dict[str, Any]]) -> EvidenceDraft:
    a = result.analysis
    if a.change is None or a.joint is None:
        raise InvariantViolation("change_evidence_incomplete", "summary evidence exists only for completed runs")
    ev = a.change.events
    method = f"{science.CHANGE_VERSION}@1"
    cm = f"{science.COVERAGE_VERSION}@1"
    px = a.pixel_area_true_m2
    jv = a.joint.joint_valid_px
    ms = [
        MeasurementDraft("joint_valid_area", "value", jv * px, cm, n_samples=jv),
        MeasurementDraft("eligible_pixels", "count", jv, method, n_samples=jv),
        MeasurementDraft("change_event_count", "count", len(ev.events), method),
        MeasurementDraft("changed_area", "value", ev.changed_area_m2, method, n_samples=ev.changed_px),
        MeasurementDraft(
            "changed_fraction",
            "fraction",
            ev.changed_px / jv if jv else None,
            method,
            unavailable_reason=None if jv else "not_applicable",
            denominator="joint_valid_area",
            n_samples=ev.changed_px,
        ),
        MeasurementDraft("observations_usable.a", "count", a.period_a.observations_usable, cm),
        MeasurementDraft("observations_usable.b", "count", a.period_b.observations_usable, cm),
    ]
    for code, cat in CATEGORIES.items():
        n_px = ev.changed_px_by_category[cat]
        ms += [
            MeasurementDraft(f"change_event_count.{cat}", "count", sum(e.category_code == code for e in ev.events), method),
            MeasurementDraft(f"changed_area.{cat}", "value", ev.changed_area_category_m2(cat), method, n_samples=n_px),
            MeasurementDraft(
                f"changed_fraction.{cat}",
                "fraction",
                n_px / jv if jv else None,
                method,
                unavailable_reason=None if jv else "not_applicable",
                denominator="joint_valid_area",
                n_samples=n_px,
            ),
        ]
    keys = ["category", "event_label", "joint_valid", "vector", *(k for k in artifacts if k.startswith(("delta:", "z:")))]
    return EvidenceDraft(
        kind="change_summary",
        facts={
            "mode": a.change.reference.mode,
            "coregistration_status": a.coregistration_status,
            "outcome": a.outcome,
            "change_version": science.CHANGE_VERSION,
            "algorithm_versions": dict(result.algorithm_versions),
            "parameters": {k: json.dumps(v) for k, v in sorted(result.parameters.items())},
            "parameters_calibration": "untuned engineering defaults (C5 §9), not calibrated values",
            "changed_area_semantics": "union of retained event pixels; per-category values are unions within the "
            "category and may sum to more than the total where categories overlap (C-17)",
            "vector_artifact": "change_events.gpkg (layer change_events, UTM, FID = event_label)",
            "grid_hash": a.grid.grid_hash,
            "epsg": a.grid.epsg,
        },
        measurements=ms,
        limitations=["thresholds_untuned", *_run_blocking(result, CHANGE_BLOCKING)],
        geometry=aoi["geometry"],
        observed_period_a=_period_tuple(result.request.period_a),
        observed_period_b=_period_tuple(result.request.period_b),
        artifact_keys=keys,
    )


def build_evidence(
    result: ChangeRunResult, aoi: dict[str, Any], aoi_area_m2: float, artifacts: dict[str, dict[str, Any]]
) -> list[EvidenceDraft]:
    """All evidence of a change run, in a fixed order (C-03 early stops included)."""
    drafts = [_coverage_evidence(result, aoi, aoi_area_m2)]
    drafts += [_period_index_evidence(result, n, p, aoi) for p in PERIODS for n in science.INDICES]
    drafts.append(_coreg_evidence(result, aoi, artifacts))
    a = result.analysis
    if a.outcome == "completed" and a.change is not None:
        drafts += [_delta_evidence(result, n, aoi) for n in science.CHANGE_INDICES]
        drafts += [_event_evidence(result, e) for e in a.change.events.events]
        drafts.append(_summary_evidence(result, aoi, artifacts))
    return drafts


# --- transaction -------------------------------------------------------------------------------------


def _environment(vector: VectorArtifact | None) -> dict[str, str]:
    env = dict(environment())
    env["scikit-image"] = metadata.version("scikit-image")
    if vector is not None:
        env["pyogrio"] = vector.pyogrio_version
        env["pyogrio_gdal"] = vector.gdal_version
        env["gpkg_sqlite"] = str(vector.sqlite_version_number)
    return env


def _persist(
    engine: Engine,
    job: dict[str, Any],
    run: dict[str, Any],
    aoi: dict[str, Any],
    result: ChangeRunResult,
    artifacts: dict[str, dict[str, Any]],
    vector: VectorArtifact | None,
    aoi_area_m2: float,
    started: datetime,
) -> None:
    a = result.analysis
    params = result.parameters
    ph = canonical_hash(params)
    steps = list(result.steps)
    for p in PERIODS:
        if f"rgb.{p}" in artifacts:
            steps.append(
                {
                    "kind": "display_composite",
                    "purpose": "display_rgb",
                    "bands": ["red", "green", "blue"],
                    "statistic": "median",
                    "artifact_sha256": artifacts[f"rgb.{p}"]["sha256"],
                    "period": p,
                }
            )
    drafts = build_evidence(result, aoi, aoi_area_m2, artifacts)
    with engine.begin() as conn:
        pr = repo.insert_processing_run(
            conn,
            project_id=run["project_id"],
            analysis_run_id=run["id"],
            kind="analysis",
            parameters=params,
            parameters_hash=ph,
            grid_hash=a.grid.grid_hash,
            code_version=code_version(),
            environment=_environment(vector),
            steps=steps,
            timings={k: round(v, 3) for k, v in a.timings_s.items()},
            started_at=started,
            algorithm_versions=result.algorithm_versions,
        )
        for p, summary in (("a", a.period_a), ("b", a.period_b)):
            for s in summary.scenes:
                sid, _ = repo.upsert_scene(conn, s)
                conn.execute(
                    text(
                        """INSERT INTO analysis_run_inputs (analysis_run_id, scene_id, period, datatake_id, status, reason_code)
                       VALUES (:r, :s, :p, :dt, :st, :rc)"""
                    ),
                    {"r": run["id"], "s": sid, "p": p, "dt": s.datatake_id, "st": s.status, "rc": s.reason_code},
                )
        art_ids = {
            key: repo.insert_artifact(
                conn,
                run_id=pr,
                sha256=meta["sha256"],
                uri=meta["uri"],
                media_type=meta["media_type"],
                nbytes=meta["bytes"],
                kind=meta["kind"],
                raster=meta["raster"],
            )
            for key, meta in artifacts.items()
        }
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
            if ev.kind == "change_event":
                repo.insert_change_event(
                    conn,
                    analysis_run_id=run["id"],
                    evidence_id=ev.evidence_id,
                    category=ev.facts["category"],
                    touches_aoi_boundary=ev.facts["touches_aoi_boundary"],
                )
        conn.execute(
            text("UPDATE analysis_runs SET status='succeeded', outcome=:o, completed_at=now() WHERE id=:id"),
            {"o": a.outcome, "id": run["id"]},
        )
        conn.execute(
            text("UPDATE jobs SET status='succeeded', stage='done', progress=1, finished_at=now(), error=NULL WHERE id=:id"),
            {"id": job["id"]},
        )
        conn.execute(
            text("SELECT pg_notify('aether_events', :m)"),
            {"m": json.dumps({"job_id": str(job["id"]), "status": "succeeded", "outcome": a.outcome})},
        )
    log.info("change.persisted run=%s outcome=%s evidence=%d artifacts=%d", run["id"], a.outcome, len(drafts), len(artifacts))
