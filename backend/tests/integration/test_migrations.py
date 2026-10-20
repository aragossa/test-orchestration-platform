"""Alembic migrations describe the same schema as the ORM models.

The other integration tests build the schema with `Base.metadata.create_all`;
production builds it with `alembic upgrade head`. Nothing else checks that the
two agree, so a model change without a migration would pass every test and
break the deployment. This test migrates an empty database and diffs it
against the models.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.persistence.models import Base
from tests.conftest import ADMIN_URL, TEST_URL

BACKEND_DIR = Path(__file__).resolve().parents[2]
MIGRATIONS_DB = "orchestrator_migrations"
MIGRATIONS_URL = TEST_URL.rsplit("/", 1)[0] + f"/{MIGRATIONS_DB}"


async def _recreate_database() -> None:
    conn = await asyncpg.connect(ADMIN_URL)
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            MIGRATIONS_DB,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{MIGRATIONS_DB}"')
        await conn.execute(f'CREATE DATABASE "{MIGRATIONS_DB}"')
    finally:
        await conn.close()


async def _alembic(*args: str) -> None:
    env = {**os.environ, "DATABASE_URL": MIGRATIONS_URL}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        *args,
        cwd=BACKEND_DIR,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    assert proc.returncode == 0, f"alembic {' '.join(args)} failed:\n{out.decode()}"


def _diff_against_models(connection: Connection) -> list[object]:
    return compare_metadata(MigrationContext.configure(connection), Base.metadata)


def _table_names(connection: Connection) -> list[str]:
    return sorted(inspect(connection).get_table_names())


async def test_migrations_head_matches_the_models_and_downgrade_is_clean() -> None:
    await _recreate_database()
    engine = create_async_engine(MIGRATIONS_URL)
    try:
        await _alembic("upgrade", "head")
        async with engine.connect() as conn:
            diff = await conn.run_sync(_diff_against_models)
        assert diff == [], f"models and migrations disagree: {diff}"

        await _alembic("downgrade", "base")
        async with engine.connect() as conn:
            tables = await conn.run_sync(_table_names)
        assert tables == ["alembic_version"]

        # Migrations are replayable from scratch.
        await _alembic("upgrade", "head")
        async with engine.connect() as conn:
            diff = await conn.run_sync(_diff_against_models)
        assert diff == []
    finally:
        await engine.dispose()
