"""Shared fixtures.

Integration tests run against a real PostgreSQL database because the
behaviour under test (partial unique index, FOR UPDATE SKIP LOCKED, JSONB)
is PostgreSQL behaviour. The local docker-compose database is used by default;
override with TEST_DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text

from app.adapters import MockInfrastructureAdapter, MockTestExecutionAdapter
from app.adapters.base import ExecutionContext, TestResult
from app.config import Settings
from app.domain.config import RunConfig
from app.domain.states import FailureSimulation
from app.orchestration.worker import Worker
from app.persistence.db import Database
from app.persistence.models import Base
from app.persistence.seed import seed_reference_data

ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL", "postgresql://orchestrator:orchestrator@localhost:5432/orchestrator"
)
TEST_DB_NAME = "orchestrator_test"
TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    f"postgresql+asyncpg://orchestrator:orchestrator@localhost:5432/{TEST_DB_NAME}",
)

TABLES = ["run_events", "run_transitions", "run_tests", "runs", "test_definitions", "environments"]


async def _ensure_test_database() -> None:
    conn = await asyncpg.connect(ADMIN_URL)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB_NAME)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    return Settings(
        database_url=TEST_URL,
        worker_enabled=True,
        worker_id="test-worker",
        worker_poll_interval_ms=50,
        worker_concurrency=4,
        worker_lease_seconds=15,
        worker_heartbeat_seconds=1,
        mock_prepare_delay_ms=0,
        mock_deploy_delay_ms=0,
        mock_ready_delay_ms=0,
        mock_test_delay_scale=0.0,
        cors_origins=[],
    )


@pytest.fixture(scope="session")
def _schema(test_settings: Settings) -> None:
    async def prepare() -> None:
        await _ensure_test_database()
        db = Database(test_settings.database_url)
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await db.dispose()

    asyncio.run(prepare())


@pytest.fixture
async def db(test_settings: Settings, _schema: None) -> AsyncIterator[Database]:
    database = Database(test_settings.database_url)
    async with database.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    async with database.session() as session:
        await seed_reference_data(session)
    yield database
    await database.dispose()


# ---------------------------------------------------------------- adapters


class SpyTestRunner(MockTestExecutionAdapter):
    """Mock runner that records which tests it was asked to execute."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(delay_scale=0.0, **kwargs)
        self.calls: list[str] = []

    async def execute_test(self, test_id: str, context: ExecutionContext) -> TestResult:
        self.calls.append(test_id)
        return await super().execute_test(test_id, context)


class GateInfrastructure(MockInfrastructureAdapter):
    """Mock infrastructure whose prepare step blocks until released.

    Lets a test hold a run in PREPARING for as long as it needs.
    """

    def __init__(self) -> None:
        super().__init__(prepare_delay_ms=0, deploy_delay_ms=0, ready_delay_ms=0)
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.prepare_calls = 0

    async def prepare_environment(self, environment: str, config: RunConfig) -> None:
        self.prepare_calls += 1
        self.entered.set()
        await self.gate.wait()


@pytest.fixture
def infrastructure() -> MockInfrastructureAdapter:
    return MockInfrastructureAdapter(prepare_delay_ms=0, deploy_delay_ms=0, ready_delay_ms=0)


@pytest.fixture
def runner() -> SpyTestRunner:
    return SpyTestRunner()


@pytest.fixture
def make_worker(db: Database, test_settings: Settings) -> Callable[..., Worker]:
    def factory(
        infrastructure: MockInfrastructureAdapter | None = None,
        runner: MockTestExecutionAdapter | None = None,
        worker_id: str = "test-worker",
    ) -> Worker:
        settings = test_settings.model_copy(update={"worker_id": worker_id})
        return Worker(
            db,
            settings,
            infrastructure
            or MockInfrastructureAdapter(prepare_delay_ms=0, deploy_delay_ms=0, ready_delay_ms=0),
            runner or SpyTestRunner(),
        )

    return factory


# ------------------------------------------------------------------ helpers


def make_config(**overrides: Any) -> RunConfig:
    base: dict[str, Any] = {
        "environment": "test",
        "deployment_version": "latest",
        "worker_instances": 2,
        "data_generators": 1,
        "timeout_seconds": 60,
        "selected_test_ids": ("connectivity_check", "data_ingestion", "processing_validation"),
        "failure_simulation": FailureSimulation.NONE,
    }
    base.update(overrides)
    return RunConfig.model_validate(base)


async def eventually(
    predicate: Callable[[], Awaitable[bool]], *, timeout: float = 5.0, interval: float = 0.02
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")
