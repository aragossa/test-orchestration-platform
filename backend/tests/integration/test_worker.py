"""The worker: claiming, concurrency, queueing, cancellation and recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.config import Settings
from app.domain.states import RunState, TestStatus
from app.orchestration.worker import Worker
from app.persistence.db import Database
from app.persistence.models import Run, RunTest
from app.persistence.repository import RunRepository, WorkerRepository
from app.services import RunService
from app.worker_service import check, serve
from tests.conftest import (
    DeployGateInfrastructure,
    GateInfrastructure,
    SpyTestRunner,
    eventually,
    make_config,
)


async def create(db: Database, **overrides: object) -> int:
    async with db.session() as session:
        return (await RunRepository(session).create_run(make_config(**overrides))).id


async def state_of(db: Database, run_id: int) -> str:
    async with db.session() as session:
        return (await RunRepository(session).get_run_or_raise(run_id)).state


async def test_two_runs_in_different_environments_execute_concurrently(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    worker = make_worker()
    a = await create(db, environment="test")
    b = await create(db, environment="stage")

    launched = await worker.tick()
    assert sorted(launched) == [a, b]
    await worker.wait_idle()

    async with db.session() as session:
        repo = RunRepository(session)
        run_a, run_b = await repo.get_run_or_raise(a), await repo.get_run_or_raise(b)
    assert run_a.state == run_b.state == RunState.COMPLETED
    # Each run owns its own history; nothing bled across.
    assert len(run_a.transitions) == len(run_b.transitions) == 7
    assert {t.run_id for t in run_a.transitions} == {a}
    assert {t.run_id for t in run_b.transitions} == {b}
    assert all(t.status == TestStatus.PASSED for t in run_a.tests + run_b.tests)


async def test_second_run_waits_until_environment_is_free(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    gate = GateInfrastructure()
    worker = make_worker(infrastructure=gate)
    first = await create(db, environment="stage")
    second = await create(db, environment="stage")

    assert await worker.tick() == [first]
    await gate.entered.wait()
    assert await state_of(db, first) == RunState.PREPARING
    # While the first run holds the environment, the second is not picked up.
    assert await worker.tick() == []
    assert await state_of(db, second) == RunState.PENDING

    gate.gate.set()
    await worker.wait_idle()
    assert await state_of(db, first) == RunState.COMPLETED

    assert await worker.tick() == [second]
    await worker.wait_idle()
    assert await state_of(db, second) == RunState.COMPLETED


async def test_cancel_takes_effect_mid_run(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    gate = GateInfrastructure()
    runner = SpyTestRunner()
    worker = make_worker(infrastructure=gate, runner=runner)
    run_id = await create(db)

    await worker.tick()
    await gate.entered.wait()

    async with db.session() as session:
        await RunService(session).cancel_run(run_id)
    gate.gate.set()
    await worker.wait_idle()

    async with db.session() as session:
        run = await RunRepository(session).get_run_or_raise(run_id)
    assert run.state == RunState.CANCELLED
    assert run.transitions[-1].reason == "Cancelled by user"
    assert [t.status for t in run.tests] == [TestStatus.SKIPPED] * 3
    assert runner.calls == []
    assert run.finished_at is not None


async def test_cancel_while_a_test_is_running_interrupts_it(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    class SlowRunner(SpyTestRunner):
        async def execute_test(self, test_id, context):  # type: ignore[no-untyped-def]
            self.calls.append(test_id)
            await asyncio.sleep(30)
            raise AssertionError("should have been cancelled")

    runner = SlowRunner()
    worker = make_worker(runner=runner)
    run_id = await create(db, selected_test_ids=("connectivity_check", "data_ingestion"))
    await worker.tick()

    async def first_test_started() -> bool:
        return runner.calls == ["connectivity_check"]

    await eventually(first_test_started)
    assert await state_of(db, run_id) == RunState.RUNNING_TESTS

    async with db.session() as session:
        await RunService(session).cancel_run(run_id)
    await worker.wait_idle(timeout=10)

    async with db.session() as session:
        run = await RunRepository(session).get_run_or_raise(run_id)
    assert run.state == RunState.CANCELLED
    assert runner.calls == ["connectivity_check"]
    assert [t.status for t in run.tests] == [TestStatus.SKIPPED, TestStatus.SKIPPED]


async def _status_is(db: Database, run_id: int, state: RunState) -> bool:
    return await state_of(db, run_id) == state


async def test_interrupted_run_is_resumed_from_its_last_step(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    """Simulate a crash in the middle of RUNNING_TESTS and restart the worker."""
    run_id = await create(db)
    async with db.session() as session:
        repo = RunRepository(session)
        await repo.transition(run_id, RunState.PENDING, RunState.PREPARING)
        await repo.transition(run_id, RunState.PREPARING, RunState.DEPLOYING)
        await repo.transition(
            run_id, RunState.DEPLOYING, RunState.WAITING_FOR_READY, deployment_id="dep-old"
        )
        await repo.transition(run_id, RunState.WAITING_FOR_READY, RunState.RUNNING_TESTS)
        run = await repo.get_run_or_raise(run_id)
        first, second, _ = run.tests
        await repo.update_test(first.id, status=TestStatus.PASSED, duration_ms=5, message="ok")
        await repo.update_test(second.id, status=TestStatus.RUNNING)  # was mid-flight
        # The dead worker's lease lapsed a minute ago.
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(worker_id="dead-worker", lease_expires_at=datetime.now(UTC) - timedelta(60))
        )
        await session.commit()

    runner = SpyTestRunner()
    worker = make_worker(runner=runner, worker_id="fresh-worker")
    await worker.start()
    try:
        await eventually(lambda: _status_is(db, run_id, RunState.COMPLETED), timeout=5)
    finally:
        await worker.stop()

    async with db.session() as session:
        run = await RunRepository(session).get_run_or_raise(run_id)
    assert worker.recovered_run_ids == [run_id]
    assert run.deployment_id == "dep-old"
    # The already-passed test was not executed again; the interrupted one was.
    assert runner.calls == ["data_ingestion", "processing_validation"]
    assert [t.status for t in run.tests] == [TestStatus.PASSED] * 3
    assert any(
        e.level == "warning" and "resumed from RUNNING_TESTS" in e.message for e in run.events
    )
    assert [t.to_state for t in run.transitions][-3:] == [
        "RUNNING_TESTS",
        "COLLECTING_RESULTS",
        "COMPLETED",
    ]


async def test_startup_reclaims_runs_this_worker_owned_before_restart(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    run_id = await create(db)
    async with db.session() as session:
        repo = RunRepository(session)
        await repo.transition(run_id, RunState.PENDING, RunState.PREPARING)
        # Lease still valid: a *different* worker would have to wait for it.
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(worker_id="node-1", lease_expires_at=datetime.now(UTC) + timedelta(60))
        )
        await session.commit()

    stranger = make_worker(worker_id="node-2")
    assert await stranger.tick() == []

    same = make_worker(worker_id="node-1")
    await same.start()
    try:
        await eventually(lambda: _status_is(db, run_id, RunState.COMPLETED))
    finally:
        await same.stop()
    assert same.recovered_run_ids == [run_id]


async def test_startup_repairs_finished_runs_with_unfinished_tests(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    """A FAILED run whose tests were never marked (the inconsistent state a
    crash between two commits used to leave) is repaired before anything else."""
    run_id = await create(db)
    async with db.session() as session:
        repo = RunRepository(session)
        await repo.transition(run_id, RunState.PENDING, RunState.PREPARING)
        await repo.transition(run_id, RunState.PREPARING, RunState.FAILED, reason="crashed")
    assert [t.status for t in (await _load(db, run_id)).tests] == [TestStatus.PENDING] * 3

    worker = make_worker()
    await worker.start()
    try:
        await eventually(lambda: _reconciled(worker, run_id))
    finally:
        await worker.stop()

    run = await _load(db, run_id)
    assert run.state == RunState.FAILED
    assert [t.status for t in run.tests] == [TestStatus.SKIPPED] * 3
    assert worker.status()["reconciled_on_startup"] == [run_id]
    assert worker.recovered_run_ids == []  # a finished run is never resumed


async def _reconciled(worker: Worker, run_id: int) -> bool:
    return run_id in worker.reconciled_run_ids


async def _load(db: Database, run_id: int) -> Run:
    async with db.session() as session:
        return await RunRepository(session).get_run_or_raise(run_id)


async def _abandon_in_deploying(db: Database, run_id: int, *, deploy_requested: bool) -> None:
    """Put a run in DEPLOYING with an expired lease held by a dead worker."""
    async with db.session() as session:
        repo = RunRepository(session)
        await repo.transition(run_id, RunState.PENDING, RunState.PREPARING)
        await repo.transition(run_id, RunState.PREPARING, RunState.DEPLOYING)
        values: dict[str, object] = {
            "worker_id": "dead",
            "lease_token": "dead-token",
            "lease_expires_at": datetime.now(UTC) - timedelta(5),
        }
        if deploy_requested:
            values |= {
                "deploy_requested_at": datetime.now(UTC) - timedelta(10),
                "deploy_requested_by": "dead",
            }
        await session.execute(update(Run).where(Run.id == run_id).values(**values))
        await session.commit()


async def test_run_interrupted_before_requesting_the_deployment_deploys_once(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    """The dead worker reached DEPLOYING but never asked the infrastructure to
    deploy (no marker): the new owner deploys, exactly once."""
    run_id = await create(db)
    await _abandon_in_deploying(db, run_id, deploy_requested=False)

    infrastructure = DeployGateInfrastructure()
    infrastructure.gate.set()
    worker = make_worker(infrastructure=infrastructure, worker_id="alive")
    assert await worker.tick() == [run_id]
    await worker.wait_idle()

    run = await _load(db, run_id)
    assert run.state == RunState.COMPLETED
    assert run.deployment_id is not None
    assert infrastructure.deploy_calls == [f"run-{run_id}"]
    assert run.deploy_requested_by == "alive"
    assert [t.to_state for t in run.transitions] == [
        "PENDING",
        "PREPARING",
        "DEPLOYING",
        "WAITING_FOR_READY",
        "RUNNING_TESTS",
        "COLLECTING_RESULTS",
        "COMPLETED",
    ]


async def test_run_interrupted_after_requesting_the_deployment_is_not_redeployed(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    """The dead worker had already asked the infrastructure to deploy; whether
    that deployment went out is unknown. Deploying again could start it twice,
    so the run is failed with the reason recorded, and the infrastructure is
    not called."""
    run_id = await create(db)
    await _abandon_in_deploying(db, run_id, deploy_requested=True)

    infrastructure = DeployGateInfrastructure()
    infrastructure.gate.set()
    runner = SpyTestRunner()
    worker = make_worker(infrastructure=infrastructure, runner=runner, worker_id="alive")
    assert await worker.tick() == [run_id]
    await worker.wait_idle()

    run = await _load(db, run_id)
    assert run.state == RunState.FAILED
    assert infrastructure.deploy_calls == []
    assert runner.calls == []
    assert "already requested by worker dead" in (run.error_message or "")
    assert "not requested again" in (run.error_message or "")
    assert [t.status for t in run.tests] == [TestStatus.SKIPPED] * 3
    assert run.transitions[-1].from_state == RunState.DEPLOYING
    assert run.deploy_requested_by == "dead"  # the marker is history, not overwritten


async def test_takeover_while_deploying_never_starts_a_second_deployment(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    """Worker A is inside infrastructure.deploy() when its lease lapses and
    worker B takes the run over. B must not deploy again, and A's late result
    must not be recorded over B's outcome."""
    infra_a = DeployGateInfrastructure()
    worker_a = make_worker(infrastructure=infra_a, worker_id="worker-a")
    run_id = await create(db)
    assert await worker_a.tick() == [run_id]
    await infra_a.entered.wait()  # A has requested the deployment and is waiting on it

    # A's lease lapses (it stalled); B reclaims the run on its next tick.
    async with db.session() as session:
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(1))
        )
        await session.commit()
    infra_b = DeployGateInfrastructure()
    infra_b.gate.set()
    worker_b = make_worker(infrastructure=infra_b, worker_id="worker-b")
    assert await worker_b.tick() == [run_id]
    await worker_b.wait_idle()

    run = await _load(db, run_id)
    assert run.state == RunState.FAILED
    assert infra_b.deploy_calls == []  # B saw A's marker and did not deploy
    assert "already requested by worker worker-a" in (run.error_message or "")

    # A's deployment finally "completes"; its attempt to record it is fenced out.
    infra_a.gate.set()
    await worker_a.wait_idle()
    await worker_a.stop()
    again = await _load(db, run_id)
    assert again.state == RunState.FAILED
    assert again.deployment_id is None
    assert infra_a.deploy_calls == [f"run-{run_id}"]
    assert len(again.transitions) == len(run.transitions)
    assert not any("Deployment created" in e.message for e in again.events)


async def test_same_worker_id_alive_disables_reclaiming_its_runs_on_startup(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    """Two processes sharing a WORKER_ID (misconfiguration) must not rob each
    other: a starting worker only takes back "its own" runs if no process with
    that id is heartbeating."""
    gate = GateInfrastructure()
    first = make_worker(infrastructure=gate, worker_id="shared")
    run_id = await create(db)
    assert await first.tick() == [run_id]  # heartbeat written, run leased
    await gate.entered.wait()

    twin = make_worker(worker_id="shared")
    await twin.start()
    try:
        await twin.wait_idle()
        assert twin.recovered_run_ids == []
        assert (await _load(db, run_id)).lease_token == first.lease_token
    finally:
        await twin.stop()

    gate.gate.set()
    await first.wait_idle()
    await first.stop()
    assert await state_of(db, run_id) == RunState.COMPLETED

    # Once the first process has stopped, a restart with the same id does take over.
    restarted = make_worker(worker_id="shared")
    other = await create(db)
    async with db.session() as session:
        repo = RunRepository(session)
        await repo.transition(other, RunState.PENDING, RunState.PREPARING)
        await repo.update_run(
            other,
            worker_id="shared",
            lease_token="gone",
            lease_expires_at=datetime.now(UTC) + timedelta(60),
        )
    await restarted.start()
    try:
        await eventually(lambda: _status_is(db, other, RunState.COMPLETED))
    finally:
        await restarted.stop()
    assert restarted.recovered_run_ids == [other]


async def test_lost_lease_stops_the_run_task(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    gate = GateInfrastructure()
    worker = make_worker(infrastructure=gate, worker_id="w-slow")
    run_id = await create(db)
    await worker.tick()
    await gate.entered.wait()

    # Another worker steals the run (as it would after the lease lapsed).
    async with db.session() as session:
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(worker_id="w-fast", lease_token="token-of-w-fast")
        )
        await session.commit()

    # Heartbeat (1s in tests) notices and cancels the local task.
    await worker.wait_idle(timeout=5)
    # The row was left in PREPARING for the new owner; we did not clobber it.
    assert await state_of(db, run_id) == RunState.PREPARING
    gate.gate.set()
    await worker.stop()


async def test_run_tests_rows_are_isolated_per_run(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    worker = make_worker()
    a = await create(db, environment="test", selected_test_ids=("connectivity_check",))
    b = await create(db, environment="stage", selected_test_ids=("high_load_processing",))
    await worker.tick()
    await worker.wait_idle()
    async with db.session() as session:
        rows = (await session.execute(RunTest.__table__.select())).all()
    by_run = {row.run_id: row.test_id for row in rows}
    assert by_run == {a: "connectivity_check", b: "high_load_processing"}


async def alive_ids(db: Database, stale_after: int = 15) -> list[str]:
    async with db.session() as session:
        return [w.worker_id for w in await WorkerRepository(session).list_alive(stale_after)]


async def test_heartbeat_marks_worker_alive_until_it_stops_or_goes_stale(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    worker = make_worker(worker_id="w-1")
    assert await alive_ids(db) == []

    await worker.tick()
    assert await alive_ids(db) == ["w-1"]
    # A heartbeat older than the lease window means the process is gone.
    assert await alive_ids(db, stale_after=0) == []

    await worker.stop()
    assert await alive_ids(db) == []

    # Coming back after a crash or restart revives the same row.
    await worker.tick()
    assert await alive_ids(db) == ["w-1"]


async def test_live_workers_report_the_runs_they_own(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    infrastructure = GateInfrastructure()
    worker = make_worker(infrastructure=infrastructure, worker_id="w-busy")
    run_id = await create(db)
    await worker.tick()
    await infrastructure.entered.wait()

    async with db.session() as session:
        (status,) = await WorkerRepository(session).list_alive(15)
    assert status.worker_id == "w-busy"
    assert status.active_runs == 1
    assert status.concurrency == 4

    infrastructure.gate.set()
    await worker.wait_idle()
    assert await state_of(db, run_id) == RunState.COMPLETED


async def test_worker_service_runs_until_stopped(db: Database, test_settings: Settings) -> None:
    """The standalone process: starts a worker, executes queued runs, records
    its shutdown when asked to stop."""
    settings = test_settings.model_copy(update={"worker_id": "svc-1"})
    stop = asyncio.Event()
    task = asyncio.create_task(serve(settings, stop, database=db))
    run_id = await create(db)
    try:
        await eventually(lambda: _is(db, run_id, RunState.COMPLETED))
        assert await alive_ids(db) == ["svc-1"]
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    assert await alive_ids(db) == []


async def _is(db: Database, run_id: int, state: str) -> bool:
    return await state_of(db, run_id) == state


async def test_two_workers_polling_at_once_never_claim_the_same_run(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    """FOR UPDATE SKIP LOCKED under real contention: both workers tick in the
    same instant, several rounds; every run is claimed exactly once."""
    a = make_worker(worker_id="w-a")
    b = make_worker(worker_id="w-b")
    claimed_by: dict[int, list[str]] = {}
    created: list[int] = []
    for _ in range(5):
        batch = [await create(db, environment="test"), await create(db, environment="stage")]
        created.extend(batch)
        launched_a, launched_b = await asyncio.gather(a.tick(), b.tick())
        for run_id in launched_a:
            claimed_by.setdefault(run_id, []).append("w-a")
        for run_id in launched_b:
            claimed_by.setdefault(run_id, []).append("w-b")
        await a.wait_idle()
        await b.wait_idle()

    assert sorted(claimed_by) == sorted(created)
    assert all(len(owners) == 1 for owners in claimed_by.values()), claimed_by
    for run_id in created:
        assert await state_of(db, run_id) == RunState.COMPLETED
    await a.stop()
    await b.stop()


async def test_heartbeat_keeps_extending_the_lease_while_a_run_executes(
    db: Database, make_worker: Callable[..., Worker]
) -> None:
    gate = GateInfrastructure()
    worker = make_worker(infrastructure=gate, worker_id="w-beat")
    run_id = await create(db)
    await worker.start()
    await gate.entered.wait()

    first = await _load(db, run_id)
    assert first.worker_id == "w-beat" and first.lease_token == worker.lease_token
    assert first.lease_expires_at is not None

    async def lease_moved_forward() -> bool:
        current = (await _load(db, run_id)).lease_expires_at
        return current is not None and current > first.lease_expires_at  # type: ignore[operator]

    # Heartbeat interval is 1 s in tests.
    await eventually(lease_moved_forward, timeout=5)
    renewed = await _load(db, run_id)
    assert renewed.lease_token == worker.lease_token  # same holder, just a longer lease
    assert renewed.state == RunState.PREPARING

    gate.gate.set()
    await eventually(lambda: _status_is(db, run_id, RunState.COMPLETED))
    await worker.stop()


async def test_container_health_check_follows_this_workers_heartbeat(
    db: Database, test_settings: Settings, make_worker: Callable[..., Worker]
) -> None:
    """`python -m app.worker_service --check` is the container health check:
    it passes only while *this* worker's heartbeat row is fresh."""
    settings = test_settings.model_copy(update={"worker_id": "w-probe"})
    assert await check(settings) is False  # never heartbeat

    worker = make_worker(worker_id="w-probe")
    await worker.tick()
    assert await check(settings) is True
    assert await check(settings.model_copy(update={"worker_id": "someone-else"})) is False

    await worker.stop()
    assert await check(settings) is False
