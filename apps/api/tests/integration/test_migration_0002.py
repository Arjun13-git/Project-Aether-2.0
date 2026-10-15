"""Migration 0002 (change_events): upgrade from clean and from Phase 1 data, downgrade, upgrade again.

The test leaves the database at ``head`` so the rest of the session is unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text

from tests.integration.conftest import TEST_URL

pytestmark = pytest.mark.db


def _cfg() -> Config:
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    cfg.attributes["url"] = TEST_URL
    return cfg


def _tables(engine: Engine) -> set[str]:
    engine.dispose()
    return set(inspect(engine).get_table_names())


def _version(engine: Engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_migration_0002_upgrade_downgrade_upgrade(engine: Engine):
    cfg = _cfg()
    try:
        # From a clean database to head.
        command.downgrade(cfg, "base")
        assert "change_events" not in _tables(engine) and "analysis_runs" not in _tables(engine)
        command.upgrade(cfg, "head")
        assert _version(engine) == "0002" and "change_events" in _tables(engine)

        # From an existing Phase 1 database (0001 with data) to 0002: Phase 1 data is untouched.
        command.downgrade(cfg, "0001")
        assert _version(engine) == "0001" and "change_events" not in _tables(engine)
        with engine.begin() as conn:
            pid = conn.execute(text("INSERT INTO projects (name) VALUES ('phase1 data') RETURNING id")).scalar_one()
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT name FROM projects WHERE id=:i"), {"i": pid}).scalar_one() == "phase1 data"

        # Schema of change_events matches the frozen plan §9.
        engine.dispose()
        insp = inspect(engine)
        cols = {c["name"]: str(c["type"]) for c in insp.get_columns("change_events")}
        assert cols == {
            "id": "UUID",
            "analysis_run_id": "UUID",
            "evidence_id": "TEXT",
            "category": "TEXT",
            "touches_aoi_boundary": "BOOLEAN",
        }
        fks = {(f["referred_table"], tuple(f["constrained_columns"])) for f in insp.get_foreign_keys("change_events")}
        assert fks == {("analysis_runs", ("analysis_run_id",)), ("evidence", ("evidence_id",))}
        assert "change_events_run_cat_ix" in {i["name"] for i in insp.get_indexes("change_events")}
        with engine.connect() as conn:
            trig = conn.execute(
                text("SELECT tgname FROM pg_trigger WHERE tgrelid = 'change_events'::regclass AND NOT tgisinternal")
            )
            assert [r[0] for r in trig] == ["change_events_immutable"]
            check = conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'change_events'::regclass AND contype = 'c'"
                )
            ).scalar_one()
            assert "other_significant" in check and "veg_loss" in check

        # Downgrade removes only change_events; upgrade again.
        before = _tables(engine)
        command.downgrade(cfg, "0001")
        assert _tables(engine) == before - {"change_events"}
        command.upgrade(cfg, "head")
        assert _version(engine) == "0002" and "change_events" in _tables(engine)
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL aether.project_delete = 'on'"))
            conn.execute(text("TRUNCATE projects, scenes, assets CASCADE"))
