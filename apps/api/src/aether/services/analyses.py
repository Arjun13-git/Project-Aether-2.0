"""Analysis requests: preflight, idempotent submission and read models.

``snapshot`` runs (Phase 1) keep their exact behaviour and identity. ``change`` runs (Phase 2,
implementation plan §12) are routed to :mod:`aether.services.change_persistence` for submission and get
a STAC-level, non-persisting preflight here. No request carries scientific parameters (C-09).
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

from shapely.geometry import shape
from sqlalchemy import Connection, text

from aether import science
from aether.db import repo
from aether.domain.errors import AetherError, NotFound
from aether.domain.periods import Period, validate_pair, validate_period
from aether.evidence.builders import canonical_hash
from aether.geo.grid import build_analysis_grid
from aether.geo.stac.client import StacSearcher
from aether.geo.stac.eligibility import group_observations
from aether.jobs import queue
from aether.services import change_persistence
from aether.services.catalog import discover, scene_summary

MAX_QUEUED_PER_PROJECT = 10  # Security §3


def _require_known_type(analysis_type: str) -> None:
    if analysis_type not in ("snapshot", "change"):
        raise AetherError("analysis_type_invalid", f"unknown analysis type {analysis_type!r}")


def _require_period_b(period_b: dict[str, Any] | None) -> Period:
    if period_b is None:
        raise AetherError("periods_invalid", "change runs take period_a and period_b")
    return _period(period_b)


def _period(p: dict[str, Any]) -> Period:
    try:
        return Period(date.fromisoformat(p["start"]), date.fromisoformat(p["end"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise AetherError("periods_invalid", "period must be {start, end} ISO dates") from exc


def idempotency_key(aoi: dict[str, Any], analysis_type: str, period: Period, params: dict[str, Any]) -> str:
    return canonical_hash(
        {
            "aoi_id": str(aoi["id"]),
            "geometry_hash": aoi["geometry_hash"],
            "type": analysis_type,
            "period_a": period.as_tuple(),
            "collection": science.STAC_COLLECTION,
            "parameters": params,
            "algorithm_versions": science.SNAPSHOT_ALGORITHM_VERSIONS,
        }
    )


def preflight(
    conn: Connection,
    searcher: StacSearcher,
    *,
    aoi_id: uuid.UUID,
    analysis_type: str,
    period_a: dict[str, Any],
    period_b: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_known_type(analysis_type)
    if analysis_type == "change":
        return _preflight_change(conn, searcher, aoi_id=aoi_id, period_a=_period(period_a), period_b=_require_period_b(period_b))
    if period_b is not None:
        raise AetherError("periods_invalid", "snapshot runs take period_a only")
    period = _period(period_a)
    validate_period(period)
    aoi = repo.get_aoi(conn, aoi_id)
    grid = build_analysis_grid(shape(aoi["geometry"]), aoi["utm_epsg"])
    _, scenes = discover(searcher, aoi, period)
    obs = group_observations(scenes)
    return {
        "type": analysis_type,
        "period_a": {"start": period.start.isoformat(), "end": period.end.isoformat(), "days": period.days},
        "grid": {"G10": grid.g10.describe(), "G20": grid.g20.describe()},
        "items_considered": len(scenes),
        "observations_eligible": len(obs),
        "n_min": science.N_MIN.value,
        "sufficient_observations_possible": len(obs) >= science.N_MIN.value,
        "note": "eligibility is STAC-level; read-time checks (lattice, file headers, readability) run in the job",
        "scenes": [scene_summary(s) for s in scenes],
    }


def _preflight_change(
    conn: Connection, searcher: StacSearcher, *, aoi_id: uuid.UUID, period_a: Period, period_b: Period
) -> dict[str, Any]:
    """Change preflight (plan §12, C-15): STAC-level counts per period, pair rules and the extent-based
    co-registration assessability. Synchronous; nothing is persisted; reads no pixels.

    This is a disclosure, not a gate: AOI joint coverage and the registration support are pixel
    quantities decided in the job, in the frozen order observations → coverage → co-registration (C-03).
    """
    validate_pair(period_a, period_b)  # C4 §1 codes → 422
    aoi = repo.get_aoi(conn, aoi_id)
    grid = build_analysis_grid(shape(aoi["geometry"]), aoi["utm_epsg"])
    n_min = int(science.N_MIN.value)
    periods: dict[str, Any] = {}
    scenes: dict[str, Any] = {}
    for label, period in (("a", period_a), ("b", period_b)):
        _, found = discover(searcher, aoi, period)
        eligible = len(group_observations(found))
        periods[label] = {
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "days": period.days,
            "items_considered": len(found),
            "observations_eligible": eligible,
            "sufficient_observations_possible": eligible >= n_min,
        }
        scenes[label] = [scene_summary(s) for s in found]
    min_px = science.COREG_MIN_WINDOW_PX
    assessable = grid.g10.width >= min_px and grid.g10.height >= min_px
    return {
        "type": "change",
        "pair_rules": "passed",
        "periods": periods,
        "grid": {"G10": grid.g10.describe(), "G20": grid.g20.describe()},
        "n_min": n_min,
        "sufficient_observations_possible": all(p["sufficient_observations_possible"] for p in periods.values()),
        "coregistration_assessable_by_extent": assessable,
        "coregistration_note": (
            "The analysis grid supports the 256 × 256 px co-registration window; whether registration is "
            "verified also depends on jointly valid pixels, decided in the job (C3 §9)."
            if assessable
            else "The analysis grid is smaller than 256 × 256 px, so co-registration cannot be assessed: the run "
            "would carry the blocking limitation coregistration_not_verified and confidence capped at lower (C-15)."
        ),
        "outcome_order": ["insufficient_observations", "insufficient_coverage", "coregistration_failed"],
        "note": "eligibility is STAC-level; read-time checks, AOI joint coverage and registration support are "
        "determined from pixels in the job",
        "scenes": scenes,
    }


def submit(
    conn: Connection,
    *,
    aoi_id: uuid.UUID,
    analysis_type: str,
    period_a: dict[str, Any],
    period_b: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create (or return the existing) analysis run + job. Returns (body, created)."""
    _require_known_type(analysis_type)
    if analysis_type == "change":
        # Change identity, idempotency and job creation live in the change persistence service (C-10).
        return change_persistence.submit_change(
            conn, aoi_id=aoi_id, period_a=_period(period_a), period_b=_require_period_b(period_b)
        )
    if period_b is not None:
        raise AetherError("periods_invalid", "snapshot runs take period_a only")
    period = _period(period_a)
    validate_period(period)
    aoi = repo.get_aoi(conn, aoi_id)
    grid = build_analysis_grid(shape(aoi["geometry"]), aoi["utm_epsg"])
    params = science.snapshot_parameters()
    key = idempotency_key(aoi, analysis_type, period, params)

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
           VALUES (:p, :a, 'snapshot', CAST(:pa AS daterange), NULL, :c, CAST(:par AS jsonb), :ph,
             CAST(:av AS jsonb), :gh, :k, 'queued') RETURNING id"""
        ),
        {
            "p": aoi["project_id"],
            "a": aoi_id,
            "pa": f"[{period.start},{period.end}]",
            "c": science.STAC_COLLECTION,
            "par": _json(params),
            "ph": canonical_hash(params),
            "av": _json(science.SNAPSHOT_ALGORITHM_VERSIONS),
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
    return json.dumps(obj, sort_keys=True)


def get_analysis(conn: Connection, run_id: uuid.UUID) -> dict[str, Any]:
    row = (
        conn.execute(
            text(
                """SELECT r.id, r.project_id, r.aoi_id, r.type, lower(r.period_a) AS period_start,
                  upper(r.period_a) - 1 AS period_end, lower(r.period_b) AS period_b_start,
                  upper(r.period_b) - 1 AS period_b_end, r.collection, r.parameters, r.algorithm_versions, r.grid_hash,
                  r.status, r.outcome, r.created_at, r.completed_at,
                  (SELECT j.id FROM jobs j WHERE j.analysis_run_id = r.id ORDER BY j.queued_at DESC LIMIT 1) AS job_id
           FROM analysis_runs r WHERE r.id=:id"""
            ),
            {"id": run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("analysis", str(run_id))
    out = dict(row)
    out["evidence"] = [
        dict(r)
        for r in conn.execute(
            text(
                """SELECT evidence_id, kind, facts->>'index' AS index, facts->>'period' AS period,
                  (facts->>'event_label')::int AS event_label, limitations FROM evidence
           WHERE analysis_run_id=:id
           ORDER BY kind, facts->>'index', facts->>'period', (facts->>'event_label')::int, evidence_id"""
            ),
            {"id": run_id},
        ).mappings()
    ]
    out["inputs"] = [
        dict(r)
        for r in conn.execute(
            text(
                """SELECT s.item_id, i.period, i.datatake_id, i.status, i.reason_code, s.acquired_at, s.platform,
                  s.processing_baseline
           FROM analysis_run_inputs i JOIN scenes s ON s.id = i.scene_id
           WHERE i.analysis_run_id=:id ORDER BY s.acquired_at, s.item_id"""
            ),
            {"id": run_id},
        ).mappings()
    ]
    out["calibration"] = (
        science.change_parameter_calibration_table() if out["type"] == "change" else science.parameter_calibration_table()
    )
    if out["job_id"] is not None:
        out["job"] = queue.get_job(conn, out["job_id"])
    return out


def list_analyses(conn: Connection, project_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            text(
                """SELECT id, aoi_id, type, lower(period_a) AS period_start, upper(period_a) - 1 AS period_end,
                  lower(period_b) AS period_b_start, upper(period_b) - 1 AS period_b_end, status,
                  outcome, created_at, completed_at
           FROM analysis_runs WHERE project_id=:p ORDER BY created_at DESC"""
            ),
            {"p": project_id},
        ).mappings()
    ]


def compare_check(conn: Connection, run_a: uuid.UUID, run_b: uuid.UUID) -> dict[str, Any]:
    """Phase 1 visual A/B check (C4 §1): same AOI and grid, both completed, pair rules pass.

    Produces no measurement: the response only says whether a side-by-side view is allowed.
    """
    a, b = get_analysis(conn, run_a), get_analysis(conn, run_b)
    if a["aoi_id"] != b["aoi_id"] or a["grid_hash"] != b["grid_hash"]:
        raise AetherError("grid_mismatch", "visual comparison requires the same AOI and analysis grid")
    pa = Period(a["period_start"], a["period_end"])
    pb = Period(b["period_start"], b["period_end"])
    if pa.start > pb.start:
        pa, pb = pb, pa
    validate_pair(pa, pb)
    return {
        "allowed": True,
        "label": "visual comparison — not a change analysis",
        "period_a": pa.as_tuple(),
        "period_b": pb.as_tuple(),
    }
