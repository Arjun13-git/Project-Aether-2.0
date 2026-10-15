"""Own ``jobs`` table queue with ``SELECT … FOR UPDATE SKIP LOCKED`` (ADR-017, JOB-SYSTEM.md)."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import Connection, Engine, text

TIMEOUTS_S = {"analysis_run": 20 * 60, "analyst_turn": 120, "report_build": 300}
HEARTBEAT_STALE_S = 120
RETRY_BASE_S = 10
WORKER_LOST = json.dumps({"code": "worker_lost", "message": "worker heartbeat lost", "retryable": False})


def enqueue(
    conn: Connection,
    *,
    kind: str,
    project_id: uuid.UUID,
    analysis_run_id: uuid.UUID | None,
    idempotency_key: str,
    payload: dict[str, Any],
) -> uuid.UUID:
    job_id = conn.execute(
        text(
            """INSERT INTO jobs (kind, analysis_run_id, project_id, status, stage, timeout_s, idempotency_key, payload)
           VALUES (:k, :a, :p, 'queued', 'queued', :t, :i, CAST(:pl AS jsonb)) RETURNING id"""
        ),
        {
            "k": kind,
            "a": analysis_run_id,
            "p": project_id,
            "t": TIMEOUTS_S[kind],
            "i": idempotency_key,
            "pl": json.dumps(payload),
        },
    ).scalar_one()
    conn.execute(text("SELECT pg_notify('aether_events', :m)"), {"m": json.dumps({"job_id": str(job_id), "status": "queued"})})
    return job_id


def claim(engine: Engine, worker_id: str) -> dict[str, Any] | None:
    """Claim the oldest runnable job; concurrent workers never claim the same row."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT id FROM jobs WHERE status = 'queued' AND not_before <= now()
               ORDER BY queued_at FOR UPDATE SKIP LOCKED LIMIT 1"""
            )
        ).first()
        if row is None:
            return None
        job = (
            conn.execute(
                text(
                    """UPDATE jobs SET status='running', worker_id=:w, started_at=now(), heartbeat_at=now(),
                 attempts = attempts + 1, stage='starting'
               WHERE id=:id RETURNING *"""
                ),
                {"w": worker_id, "id": row.id},
            )
            .mappings()
            .one()
        )
        if job["analysis_run_id"] is not None:
            conn.execute(text("UPDATE analysis_runs SET status='running' WHERE id=:a"), {"a": job["analysis_run_id"]})
        return dict(job)


def heartbeat(engine: Engine, job_id: uuid.UUID, stage: str | None = None, progress: float | None = None) -> bool:
    """Update heartbeat/progress; returns True if cancellation was requested."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """UPDATE jobs SET heartbeat_at=now(), stage=COALESCE(:s, stage), progress=COALESCE(:p, progress)
               WHERE id=:id AND status='running' RETURNING cancel_requested_at"""
            ),
            {"s": stage, "p": None if progress is None else max(0.0, min(1.0, progress)), "id": job_id},
        ).first()
    return row is not None and row.cancel_requested_at is not None


def fail(engine: Engine, job: dict[str, Any], error: dict[str, Any]) -> str:
    """Record a failure. Retryable errors requeue with backoff until max_attempts."""
    retry = bool(error.get("retryable")) and job["attempts"] < job["max_attempts"]
    status = "queued" if retry else "failed"
    with engine.begin() as conn:
        conn.execute(
            text(
                """UPDATE jobs SET status=:st, error=CAST(:e AS jsonb), finished_at=CASE WHEN :st='failed' THEN now() END,
                 not_before = now() + make_interval(secs => :delay), worker_id=NULL
               WHERE id=:id"""
            ),
            {
                "st": status,
                "e": json.dumps(error),
                "id": job["id"],
                "delay": RETRY_BASE_S * 2 ** job["attempts"] if retry else 0,
            },
        )
        if job["analysis_run_id"] is not None:
            conn.execute(
                text("UPDATE analysis_runs SET status=:st, completed_at=CASE WHEN :st='failed' THEN now() END WHERE id=:a"),
                {"st": status, "a": job["analysis_run_id"]},
            )
        conn.execute(
            text("SELECT pg_notify('aether_events', :m)"),
            {"m": json.dumps({"job_id": str(job["id"]), "status": status})},
        )
    return status


def mark_cancelled(engine: Engine, job: dict[str, Any]) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status='cancelled', finished_at=now() WHERE id=:id"), {"id": job["id"]})
        if job["analysis_run_id"] is not None:
            conn.execute(
                text("UPDATE analysis_runs SET status='cancelled', completed_at=now() WHERE id=:a"),
                {"a": job["analysis_run_id"]},
            )


def request_cancel(conn: Connection, job_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                """UPDATE jobs SET cancel_requested_at = COALESCE(cancel_requested_at, now()),
             status = CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
             finished_at = CASE WHEN status='queued' THEN now() ELSE finished_at END
           WHERE id=:id AND status IN ('queued','running') RETURNING id, status, analysis_run_id"""
            ),
            {"id": job_id},
        )
        .mappings()
        .first()
    )
    if row is not None and row["status"] == "cancelled" and row["analysis_run_id"] is not None:
        conn.execute(
            text("UPDATE analysis_runs SET status='cancelled', completed_at=now() WHERE id=:a"),
            {"a": row["analysis_run_id"]},
        )
    return dict(row) if row else None


def reap_stale(engine: Engine) -> int:
    """Requeue (or fail with worker_lost) running jobs whose heartbeat is older than 120 s."""
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    """UPDATE jobs SET
                 status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                 error = CASE WHEN attempts >= max_attempts
                   THEN CAST(:lost AS jsonb)
                   ELSE error END,
                 finished_at = CASE WHEN attempts >= max_attempts THEN now() END,
                 worker_id = NULL
               WHERE status='running' AND heartbeat_at < now() - make_interval(secs => :s)
               RETURNING id, status, analysis_run_id"""
                ),
                {"s": HEARTBEAT_STALE_S, "lost": WORKER_LOST},
            )
            .mappings()
            .all()
        )
        for r in rows:
            if r["analysis_run_id"] is not None:
                conn.execute(
                    text("UPDATE analysis_runs SET status=:st WHERE id=:a"),
                    {"st": r["status"], "a": r["analysis_run_id"]},
                )
    return len(rows)


def get_job(conn: Connection, job_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                """SELECT id, kind, analysis_run_id, project_id, status, stage, progress, attempts, max_attempts, error,
                  cancel_requested_at, queued_at, started_at, finished_at, heartbeat_at
           FROM jobs WHERE id=:id"""
            ),
            {"id": job_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None
