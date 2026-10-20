"""The execution engine driven directly, one run at a time."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from app.adapters import MockInfrastructureAdapter
from app.domain.states import FailureSimulation, RunState, TestStatus
from app.orchestration.executor import RunExecutor
from app.persistence.db import Database
from app.persistence.models import Run
from app.persistence.repository import RunRepository, utcnow
from tests.conftest import SpyTestRunner, make_config

HAPPY = [
    "PENDING",
    "PREPARING",
    "DEPLOYING",
    "WAITING_FOR_READY",
    "RUNNING_TESTS",
    "COLLECTING_RESULTS",
    "COMPLETED",
]


async def create(db: Database, **overrides: object) -> int:
    async with db.session() as session:
        run = await RunRepository(session).create_run(make_config(**overrides))
        return run.id


async def load(db: Database, run_id: int) -> Run:
    async with db.session() as session:
        return await RunRepository(session).get_run_or_raise(run_id)


TOKEN = "token-test-worker"


async def execute(
    db: Database, run_id: int, infrastructure: MockInfrastructureAdapter, runner: SpyTestRunner
) -> Run:
    """Drive the executor as the worker would: the run is leased to us first."""
    async with db.session() as session:
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(worker_id="test-worker", lease_token=TOKEN, lease_expires_at=utcnow())
        )
        await session.commit()
    await RunExecutor(db, infrastructure, runner, "test-worker", TOKEN).execute(run_id)
    return await load(db, run_id)


async def test_happy_path_walks_every_state_in_order(
    db: Database, infrastructure: MockInfrastructureAdapter, runner: SpyTestRunner
) -> None:
    run_id = await create(db)
    run = await execute(db, run_id, infrastructure, runner)

    assert run.state == RunState.COMPLETED
    assert [t.to_state for t in run.transitions] == HAPPY
    assert run.transitions[0].from_state is None
    assert [t.from_state for t in run.transitions[1:]] == HAPPY[:-1]
    assert all(t.created_at is not None for t in run.transitions)
    assert run.started_at is not None and run.finished_at is not None
    assert run.deployment_id and run.deployment_id.startswith("dep-")
    assert run.error_message is None
    assert run.current_step is None
    assert run.worker_id is None and run.lease_expires_at is None

    assert [t.status for t in run.tests] == [TestStatus.PASSED] * 3
    assert all(t.duration_ms is not None and t.message for t in run.tests)
    assert runner.calls == ["connectivity_check", "data_ingestion", "processing_validation"]

    messages = [e.message for e in run.events]
    assert messages[0] == "Run created"
    assert "Deployment started: latest" in messages
    assert "Test execution started" in messages
    assert messages[-1].startswith("Run completed successfully: 3/3")


async def test_config_snapshot_is_stored_verbatim(
    db: Database, infrastructure: MockInfrastructureAdapter, runner: SpyTestRunner
) -> None:
    run_id = await create(db, deployment_version="v1.2.3", worker_instances=5)
    run = await load(db, run_id)
    assert run.config_snapshot["deployment_version"] == "v1.2.3"
    assert run.config_snapshot["worker_instances"] == 5
    assert run.config_snapshot["selected_test_ids"] == [
        "connectivity_check",
        "data_ingestion",
        "processing_validation",
    ]
    assert run.failure_simulation == "none"


async def test_deployment_failure_stops_before_tests(
    db: Database, infrastructure: MockInfrastructureAdapter, runner: SpyTestRunner
) -> None:
    run_id = await create(
        db, environment="stage", failure_simulation=FailureSimulation.DEPLOYMENT_FAILURE
    )
    run = await execute(db, run_id, infrastructure, runner)

    assert run.state == RunState.FAILED
    assert [t.to_state for t in run.transitions] == ["PENDING", "PREPARING", "DEPLOYING", "FAILED"]
    failed = run.transitions[-1]
    assert failed.from_state == "DEPLOYING"
    assert failed.reason and "Simulated deployment failure" in failed.reason
    assert run.error_message == failed.reason
    assert run.finished_at is not None

    assert runner.calls == []
    assert [t.status for t in run.tests] == [TestStatus.SKIPPED] * 3
    assert all(t.message == "Skipped: run failed before execution" for t in run.tests)
    assert any(
        e.level == "error" and "Run failed during DEPLOYING" in e.message for e in run.events
    )


async def test_test_failure_is_reflected_in_final_result(
    db: Database, infrastructure: MockInfrastructureAdapter, runner: SpyTestRunner
) -> None:
    run_id = await create(db, failure_simulation=FailureSimulation.TEST_FAILURE)
    run = await execute(db, run_id, infrastructure, runner)

    assert run.state == RunState.FAILED
    statuses = [t.status for t in run.tests]
    assert statuses == [TestStatus.FAILED, TestStatus.PASSED, TestStatus.PASSED]
    assert run.tests[0].error_message and "Simulated test failure" in run.tests[0].error_message
    # Remaining tests still ran: a failing test does not abort the suite.
    assert runner.calls == ["connectivity_check", "data_ingestion", "processing_validation"]
    assert [t.to_state for t in run.transitions][-2:] == ["COLLECTING_RESULTS", "FAILED"]
    assert run.error_message == "1 of 3 tests did not pass (connectivity_check: FAILED)"


async def test_test_timeout_marks_test_timed_out_and_run_failed(
    db: Database, infrastructure: MockInfrastructureAdapter, runner: SpyTestRunner
) -> None:
    run_id = await create(
        db,
        timeout_seconds=1,
        selected_test_ids=("connectivity_check", "data_ingestion"),
        failure_simulation=FailureSimulation.TEST_TIMEOUT,
    )
    run = await execute(db, run_id, infrastructure, runner)

    assert run.state == RunState.FAILED
    first, second = run.tests
    assert first.status == TestStatus.TIMED_OUT
    assert first.duration_ms == 1000
    assert first.error_message == "Test exceeded the run timeout of 1 s"
    assert second.status == TestStatus.PASSED
    assert any("timed out" in e.message for e in run.events)
    assert run.error_message == "1 of 2 tests did not pass (connectivity_check: TIMED_OUT)"


async def test_readiness_timeout_fails_the_run(db: Database, runner: SpyTestRunner) -> None:
    slow = MockInfrastructureAdapter(prepare_delay_ms=0, deploy_delay_ms=0, ready_delay_ms=5000)
    run_id = await create(db, timeout_seconds=1)
    run = await execute(db, run_id, slow, runner)

    assert run.state == RunState.FAILED
    assert run.transitions[-1].from_state == "WAITING_FOR_READY"
    assert "did not become ready" in (run.error_message or "")
    assert runner.calls == []


async def test_unexpected_adapter_exception_is_recorded_not_leaked(
    db: Database, runner: SpyTestRunner
) -> None:
    class Exploding(MockInfrastructureAdapter):
        async def deploy(  # type: ignore[override]
            self, environment: str, config: object, *, idempotency_key: str
        ) -> None:
            raise RuntimeError("boom")

    run_id = await create(db)
    run = await execute(db, run_id, Exploding(prepare_delay_ms=0), runner)
    assert run.state == RunState.FAILED
    assert "Unexpected error during DEPLOYING" in (run.error_message or "")
    assert "boom" in (run.error_message or "")


async def test_terminal_run_is_left_untouched(
    db: Database, infrastructure: MockInfrastructureAdapter, runner: SpyTestRunner
) -> None:
    run_id = await create(db)
    first = await execute(db, run_id, infrastructure, runner)
    again = await execute(db, run_id, infrastructure, runner)
    assert len(again.transitions) == len(first.transitions)
    assert len(again.events) == len(first.events)
    assert runner.calls == ["connectivity_check", "data_ingestion", "processing_validation"]


@pytest.mark.parametrize("simulation", list(FailureSimulation))
async def test_every_simulation_reaches_a_terminal_state(
    db: Database,
    infrastructure: MockInfrastructureAdapter,
    runner: SpyTestRunner,
    simulation: FailureSimulation,
) -> None:
    run_id = await create(db, timeout_seconds=1, failure_simulation=simulation)
    run = await execute(db, run_id, infrastructure, runner)
    expected = RunState.COMPLETED if simulation is FailureSimulation.NONE else RunState.FAILED
    assert run.state == expected
    # A finished run never has tests still waiting to be executed.
    assert all(t.status not in (TestStatus.PENDING, TestStatus.RUNNING) for t in run.tests)
