"""scripts/repro_check.py selects snapshot runs only: a completed change run must not be re-executed as a snapshot."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import Engine, text

from aether.db import repo
from aether.jobs import queue
from aether.jobs.worker import run_job
from aether.services import analyses, aois
from aether.storage.store import LocalFilesystemStore
from tests.change_fixtures import PERIOD_A, PERIOD_B, aoi_record, world

pytestmark = pytest.mark.db

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "repro_check.py"


def _load_script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.chdir(Path.cwd())  # the script chdirs on import; monkeypatch restores the cwd afterwards
    spec = importlib.util.spec_from_file_location("repro_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed_run(engine: Engine, store: LocalFilesystemStore, name: str, analysis_type: str) -> str:
    searcher, reader = world()
    period = {"start": PERIOD_A.start.isoformat(), "end": PERIOD_A.end.isoformat()}
    period_b = {"start": PERIOD_B.start.isoformat(), "end": PERIOD_B.end.isoformat()} if analysis_type == "change" else None
    with engine.begin() as conn:
        project = repo.insert_project(conn, name, "")
        aoi = aois.create_aoi(conn, project_id=project["id"], name=name, geometry=aoi_record(2300)["geometry"], source="geojson")
        body, created = analyses.submit(conn, aoi_id=aoi["id"], analysis_type=analysis_type, period_a=period, period_b=period_b)
    assert created
    job = queue.claim(engine, "test-worker")
    assert job is not None
    run_job(engine, job, searcher=searcher, reader=reader, store=store)
    with engine.connect() as conn:
        run = conn.execute(
            text("SELECT id, type, status, outcome FROM analysis_runs WHERE id=:r"), {"r": body["analysis_run_id"]}
        ).one()
    assert (run.type, run.status, run.outcome) == (analysis_type, "succeeded", "completed")
    return str(run.id)


def test_completed_change_run_does_not_contaminate_snapshot_selection(
    clean: Engine, storage_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = LocalFilesystemStore(storage_root)
    snapshot_id = _completed_run(clean, store, "repro snapshot", "snapshot")
    change_id = _completed_run(clean, store, "repro change", "change")
    script = _load_script(monkeypatch)

    with clean.connect() as conn:
        assert [str(i) for i in script.snapshot_run_ids(conn)] == [snapshot_id]
        # Named explicitly, a change run is refused (FAIL) before any snapshot re-execution.
        assert script.check(conn, change_id) is False
    assert "change run: not a snapshot run" in capsys.readouterr().out
