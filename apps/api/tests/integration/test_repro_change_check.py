"""scripts/repro_change_check.py: re-executes a persisted change run read-only and detects any difference."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import Engine, text

from aether import science
from aether.db import repo
from aether.jobs import queue
from aether.jobs.worker import run_job
from aether.services import analyses, aois
from aether.storage.store import LocalFilesystemStore
from tests.change_fixtures import PERIOD_A, PERIOD_B, ItemWorld, aoi_record, centred_box, world

pytestmark = pytest.mark.db

SCRIPTS = Path(__file__).resolve().parents[4] / "scripts"
EVENT = (centred_box(200),)
TABLES = (
    "analysis_runs",
    "analysis_run_inputs",
    "artifacts",
    "change_events",
    "evidence",
    "jobs",
    "measurements",
    "processing_runs",
)


def _load(monkeypatch: pytest.MonkeyPatch, name: str) -> ModuleType:
    monkeypatch.chdir(Path.cwd())  # the scripts chdir on import; monkeypatch restores the cwd afterwards
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine: Engine, store: LocalFilesystemStore, name: str, analysis_type: str, **world_kw: Any) -> str:
    searcher, reader = world(**world_kw)
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
        outcome = conn.execute(text("SELECT outcome FROM analysis_runs WHERE id=:r"), {"r": body["analysis_run_id"]}).scalar_one()
    assert outcome == "completed"
    return str(body["analysis_run_id"])


def _db_state(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        state: dict[str, Any] = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in TABLES}
        state["runs"] = conn.execute(
            text(
                """SELECT string_agg(id::text || status || coalesce(outcome, '') || coalesce(completed_at::text, ''), ','
                   ORDER BY id) FROM analysis_runs"""
            )
        ).scalar_one()
    return state


def _check(engine: Engine, script: ModuleType, run_id: str, reader: Any) -> bool:
    with engine.connect() as conn:  # a fresh connection: the check opens its own READ ONLY transaction
        return script.check(conn, run_id, reader=reader)


def _files(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in (root / "artifacts").rglob("*") if p.is_file()}


class _NoReads:
    def read(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("the run must not be re-executed")


@pytest.fixture
def change_run(clean: Engine, storage_root: Path) -> str:
    return _run(clean, LocalFilesystemStore(storage_root), "repro change", "change", b=ItemWorld(event_boxes=EVENT))


def test_completed_change_run_reproduces_without_mutating_anything(
    clean: Engine, storage_root: Path, change_run: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load(monkeypatch, "repro_change_check")
    before, files_before = _db_state(clean), _files(storage_root)
    _, reader = world(b=ItemWorld(event_boxes=EVENT))

    assert _check(clean, script, change_run, reader) is True
    out = capsys.readouterr().out
    assert "DIFF" not in out and out.rstrip().endswith("PASS")
    assert "change events (label, category, boundary)  (1 recorded)" in out
    assert _db_state(clean) == before  # no database row added, changed or removed
    assert _files(storage_root) == files_before  # no persisted artifact written or touched
    with clean.connect() as conn:
        assert [str(i) for i in script.change_run_ids(conn)] == [change_run]


def test_different_reproduced_pixels_fail(
    clean: Engine, change_run: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load(monkeypatch, "repro_change_check")
    _, reader = world(b=ItemWorld(event_boxes=(centred_box(300),)))  # same recorded items, different pixels

    assert _check(clean, script, change_run, reader) is False
    out = capsys.readouterr().out
    assert "DIFF  artifacts (kind, bands, sha256)" in out
    assert "DIFF  measurements" in out
    assert out.rstrip().endswith("FAIL")


def test_corrupted_persisted_artifact_fails(
    clean: Engine, change_run: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load(monkeypatch, "repro_change_check")
    with clean.connect() as conn:
        uri = conn.execute(
            text(
                """SELECT a.uri FROM evidence e JOIN artifacts a ON a.id = ANY(e.artifact_ids)
                   WHERE e.analysis_run_id=:r AND a.kind='delta' LIMIT 1"""
            ),
            {"r": change_run},
        ).scalar_one()
    path = Path(uri.removeprefix("file://"))
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\0")  # bytes no longer match the recorded sha256
        _, reader = world(b=ItemWorld(event_boxes=EVENT))
        assert _check(clean, script, change_run, reader) is False
    finally:
        path.write_bytes(original)  # content-addressed files may be shared with other tests
    out = capsys.readouterr().out
    assert "DIFF  persisted artifact bytes intact" in out
    assert "ok    artifacts (kind, bands, sha256)" in out  # the re-execution itself still matches the record


@pytest.mark.parametrize("change", ["versions", "parameters"])
def test_changed_identity_fails_without_re_execution(
    clean: Engine, change_run: str, change: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load(monkeypatch, "repro_change_check")
    if change == "versions":
        bumped = {**science.CHANGE_ALGORITHM_VERSIONS, "change": "change.index_diff.v2"}
        monkeypatch.setattr(science, "CHANGE_ALGORITHM_VERSIONS", bumped)
    else:
        params = science.change_parameters()
        monkeypatch.setattr(science, "change_parameters", lambda: {**params, "k": 2.5})

    assert _check(clean, script, change_run, _NoReads()) is False
    out = capsys.readouterr().out
    assert "DIFF  change idempotency key" in out
    assert f"DIFF  {'algorithm versions' if change == 'versions' else 'parameters (frozen defaults)'}" in out
    assert "not re-executed" in out


def test_snapshot_run_is_rejected_and_snapshot_repro_selection_is_unchanged(
    clean: Engine, storage_root: Path, change_run: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = _run(clean, LocalFilesystemStore(storage_root), "repro snapshot", "snapshot")
    script = _load(monkeypatch, "repro_change_check")
    snapshot_script = _load(monkeypatch, "repro_check")

    assert _check(clean, script, snapshot, _NoReads()) is False
    assert "snapshot run: not a change run; use scripts/repro_check.py  FAIL" in capsys.readouterr().out
    with clean.connect() as conn:
        assert [str(i) for i in script.change_run_ids(conn)] == [change_run]
        assert [str(i) for i in snapshot_script.snapshot_run_ids(conn)] == [snapshot]
