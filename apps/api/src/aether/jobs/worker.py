"""Worker process: claims jobs, heartbeats, runs stages, records structured failures (ADR-017).

Run with ``uv run python -m aether.jobs.worker``.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Any

from sqlalchemy import Engine

from aether.db.engine import get_engine
from aether.domain.errors import AetherError
from aether.geo.io.windows import CachedAssetReader, RasterioAssetReader
from aether.geo.snapshot import Cancelled
from aether.geo.stac.client import EarthSearchClient
from aether.jobs import queue
from aether.services import snapshot_run
from aether.settings import get_settings
from aether.storage.store import LocalFilesystemStore

log = logging.getLogger("aether.worker")
HEARTBEAT_S = 10
REAP_EVERY_S = 30


class _Heartbeat:
    """Background heartbeat + stage/progress reporting + cancellation and timeout checks."""

    def __init__(self, engine: Engine, job: dict[str, Any]) -> None:
        self.engine, self.job = engine, job
        self.stage, self.progress = "starting", 0.0
        self.cancel = False
        self.deadline = time.monotonic() + job["timeout_s"]
        self._stop = threading.Event()
        self._last_beat = 0.0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_S):
            try:
                self.cancel |= queue.heartbeat(self.engine, self.job["id"], self.stage, self.progress)
            except Exception:  # never kill the job because a heartbeat failed
                log.exception("heartbeat failed")

    def __call__(self, stage: str, progress: float) -> bool:
        """Progress callback for stages. Returns True when cancellation was requested."""
        if time.monotonic() > self.deadline:
            raise AetherError("job_timeout", f"job exceeded {self.job['timeout_s']} s", retryable=False)
        changed = stage != self.stage
        self.stage, self.progress = stage, progress
        now = time.monotonic()
        if changed or now - self._last_beat > 2.0:  # NOTIFY throttle ≤ 2/s (JOB-SYSTEM.md)
            self._last_beat = now
            self.cancel |= queue.heartbeat(self.engine, self.job["id"], stage, progress)
        return self.cancel


def run_job(engine: Engine, job: dict[str, Any], **deps: Any) -> None:
    log.info("job.start id=%s kind=%s attempt=%d", job["id"], job["kind"], job["attempts"])
    t0 = time.perf_counter()
    with _Heartbeat(engine, job) as hb:
        try:
            if job["kind"] != "analysis_run":
                raise AetherError("job_kind_not_available", f"{job['kind']} jobs do not exist in Phase 1")
            outcome = snapshot_run.execute(engine, job, heartbeat=hb, **deps)
            log.info("job.succeeded id=%s outcome=%s %.1fs", job["id"], outcome, time.perf_counter() - t0)
        except Cancelled:
            queue.mark_cancelled(engine, job)
            log.info("job.cancelled id=%s", job["id"])
        except AetherError as exc:
            status = queue.fail(engine, job, {**exc.to_dict(), "retryable": exc.retryable, "stage": hb.stage})
            log.warning("job.%s id=%s code=%s stage=%s: %s", status, job["id"], exc.code, hb.stage, exc.message)
        except Exception as exc:
            log.exception("job.failed id=%s unexpected error", job["id"])
            queue.fail(
                engine,
                job,
                {"code": "internal_error", "message": type(exc).__name__, "retryable": False, "stage": hb.stage},
            )


def default_deps() -> dict[str, Any]:
    s = get_settings()
    store = LocalFilesystemStore(s.storage_root)
    return {
        "searcher": EarthSearchClient(s.stac_api_url, s.stac_collection),
        "reader": CachedAssetReader(RasterioAssetReader(), store.root),
        "store": store,
    }


def main() -> None:
    s = get_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = get_engine()
    deps = default_deps()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    log.info("worker.start id=%s storage=%s", worker_id, s.storage_root)
    last_reap = 0.0
    while True:
        if time.monotonic() - last_reap > REAP_EVERY_S:
            n = queue.reap_stale(engine)
            if n:
                log.warning("reaper requeued/failed %d stale jobs", n)
            last_reap = time.monotonic()
        job = queue.claim(engine, worker_id)
        if job is None:
            time.sleep(s.worker_poll_s)
            continue
        run_job(engine, job, **deps)


if __name__ == "__main__":
    main()
