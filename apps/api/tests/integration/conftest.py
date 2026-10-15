"""Integration fixtures: a freshly migrated PostGIS test database and an isolated object store.

Requires ``AETHER_TEST_DATABASE_URL`` (e.g. the ``aether_test`` DB of infra/compose.yaml). Tests are
skipped when it is not set, so the unit suite never depends on a database or the network.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

TEST_URL = os.environ.get("AETHER_TEST_DATABASE_URL", "")


@pytest.fixture(scope="session")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    if not TEST_URL:
        pytest.skip("AETHER_TEST_DATABASE_URL not set")
    storage = tmp_path_factory.mktemp("storage")
    os.environ["AETHER_DATABASE_URL"] = TEST_URL
    os.environ["AETHER_STORAGE_ROOT"] = str(storage)
    from aether.db.engine import get_engine
    from aether.settings import get_settings

    get_settings.cache_clear()
    get_engine.cache_clear()

    eng = create_engine(TEST_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"))
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    cfg.attributes["url"] = TEST_URL
    command.upgrade(cfg, "head")
    yield get_engine()
    eng.dispose()


@pytest.fixture
def clean(engine: Engine) -> Engine:
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL aether.project_delete = 'on'"))
        conn.execute(text("TRUNCATE projects, scenes, assets CASCADE"))
    return engine


@pytest.fixture
def storage_root() -> Path:
    return Path(os.environ["AETHER_STORAGE_ROOT"])
