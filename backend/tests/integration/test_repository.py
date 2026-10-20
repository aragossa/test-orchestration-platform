"""Persistence guarantees: legal transitions only, optimistic locking, env lock."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.states import InvalidTransitionError, RunState, TestStatus
from app.persistence.db import Database
from app.persistence.repository import (
    EnvironmentBusyError,
    RunRepository,
    StaleRunError,
    utcnow,
)
from tests.conftest import make_config


async def test_illegal_transition_is_rejected_before_touching_the_database(db: Database) -> None:
    async with db.session() as session:
        repo = RunRepository(session)
        run_id = (await repo.create_run(make_config())).id
        with pytest.raises(InvalidTransitionError):
            await repo.transition(run_id, RunState.PENDING, RunState.COMPLETED)
    async with db.session() as session:
        reloaded = await RunRepository(session).get_run_or_raise(run_id)
        assert reloaded.state == RunState.PENDING
        assert [t.to_state for t in reloaded.transitions] == ["PENDING"]


async def test_stale_expectation_is_rejected(db: Database) -> None:
    async with db.session() as session:
        repo = RunRepository(session)
        run_id = (await repo.create_run(make_config())).id
        await repo.transition(run_id, RunState.PENDING, RunState.PREPARING)
        # Someone else still believes the run is PENDING.
        with pytest.raises(StaleRunError):
            await repo.transition(run_id, RunState.PENDING, RunState.PREPARING)
    async with db.session() as session:
        reloaded = await RunRepository(session).get_run_or_raise(run_id)
        assert reloaded.state == RunState.PREPARING
        assert [t.to_state for t in reloaded.transitions] == ["PENDING", "PREPARING"]


async def test_transition_records_history_with_reason(db: Database) -> None:
    async with db.session() as session:
        repo = RunRepository(session)
        run = await repo.create_run(make_config())
        await repo.transition(run.id, RunState.PENDING, RunState.PREPARING)
        await repo.transition(
            run.id, RunState.PREPARING, RunState.FAILED, reason="disk on fire", error_message="x"
        )
    async with db.session() as session:
        reloaded = await RunRepository(session).get_run_or_raise(run.id)
        assert [(t.from_state, t.to_state, t.reason) for t in reloaded.transitions] == [
            (None, "PENDING", None),
            ("PENDING", "PREPARING", None),
            ("PREPARING", "FAILED", "disk on fire"),
        ]
        assert reloaded.started_at is not None
        assert reloaded.finished_at is not None
        assert reloaded.transitions[1].created_at <= reloaded.transitions[2].created_at


async def test_database_forbids_two_active_runs_in_one_environment(db: Database) -> None:
    async with db.session() as session:
        repo = RunRepository(session)
        first_id = (await repo.create_run(make_config(environment="stage"))).id
        second_id = (await repo.create_run(make_config(environment="stage"))).id
        await repo.transition(first_id, RunState.PENDING, RunState.PREPARING)
        with pytest.raises(EnvironmentBusyError) as exc_info:
            await repo.transition(second_id, RunState.PENDING, RunState.PREPARING)
        assert exc_info.value.environment == "stage"

    async with db.session() as session:
        repo = RunRepository(session)
        reloaded = await repo.get_run_or_raise(second_id)
        assert reloaded.state == RunState.PENDING
        # A different environment is not affected.
        other = await repo.create_run(make_config(environment="test"))
        await repo.transition(other.id, RunState.PENDING, RunState.PREPARING)


async def test_claim_skips_runs_whose_environment_is_busy(db: Database) -> None:
    async with db.session() as session:
        repo = RunRepository(session)
        busy = await repo.create_run(make_config(environment="stage"))
        waiting = await repo.create_run(make_config(environment="stage"))
        free = await repo.create_run(make_config(environment="test"))
        await repo.transition(busy.id, RunState.PENDING, RunState.PREPARING)

        claimed = await repo.claim_pending_runs("w1", lease_seconds=10, limit=10)
        assert claimed == [free.id]
        # Claimed rows are not handed out twice while the lease holds.
        assert await repo.claim_pending_runs("w2", lease_seconds=10, limit=10) == []

        await repo.transition(busy.id, RunState.PREPARING, RunState.FAILED, reason="x")
        assert await repo.claim_pending_runs("w1", lease_seconds=10, limit=10) == [waiting.id]


async def test_reclaim_takes_only_expired_or_own_leases(db: Database) -> None:
    async with db.session() as session:
        repo = RunRepository(session)
        mine = (await repo.create_run(make_config(environment="test"))).id
        theirs = (await repo.create_run(make_config(environment="stage"))).id
        await repo.transition(mine, RunState.PENDING, RunState.PREPARING)
        await repo.transition(theirs, RunState.PENDING, RunState.PREPARING)
        now = utcnow()
        await repo.update_run(mine, worker_id="worker-a", lease_expires_at=now + timedelta(60))
        # worker-b stopped heartbeating long ago.
        await repo.update_run(theirs, worker_id="worker-b", lease_expires_at=now - timedelta(60))

        # Regular tick: only lapsed leases are taken.
        assert await repo.reclaim_abandoned_runs("worker-a", 60, 10) == [theirs]
        # Startup of worker-a: it also takes back what it owned before restart,
        # even though those leases are still valid (both runs are its own now).
        assert await repo.reclaim_abandoned_runs("worker-a", 60, 10, include_own=True) == [
            mine,
            theirs,
        ]

        # extend_lease refuses to renew a lease the worker no longer owns.
        assert await repo.extend_lease(theirs, "worker-b", 60) is False
        assert await repo.extend_lease(theirs, "worker-a", 60) is True


async def test_transitions_and_events_are_logged(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="app.persistence.repository")
    async with db.session() as session:
        repo = RunRepository(session)
        run = await repo.create_run(make_config())
        await repo.transition(run.id, RunState.PENDING, RunState.PREPARING)
        await repo.add_event(run.id, "Preparing environment")
        await repo.add_event(run.id, "Lease lost", level="warning")
        await repo.transition(
            run.id, RunState.PREPARING, RunState.FAILED, reason="disk on fire", error_message="x"
        )
    messages = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert messages == [
        ("INFO", f"run {run.id}: PENDING -> PREPARING"),
        ("INFO", f"run {run.id}: Preparing environment"),
        ("WARNING", f"run {run.id}: Lease lost"),
        ("INFO", f"run {run.id}: PREPARING -> FAILED (disk on fire)"),
    ]


async def _statuses(db: Database, run_id: int) -> list[str]:
    async with db.session() as session:
        return [t.status for t in (await RunRepository(session).get_run_or_raise(run_id)).tests]


async def test_finish_run_commits_state_tests_and_event_together(db: Database) -> None:
    """One commit for the whole outcome: there is no moment at which the run is
    finished but its tests are still pending."""
    async with db.session() as session:
        repo = RunRepository(session)
        run_id = (await repo.create_run(make_config())).id
        await repo.transition(run_id, RunState.PENDING, RunState.PREPARING)
        await repo.transition(run_id, RunState.PREPARING, RunState.DEPLOYING)

        commits = 0
        real_commit = session.commit

        async def counting_commit() -> None:
            nonlocal commits
            commits += 1
            await real_commit()

        session.commit = counting_commit  # type: ignore[method-assign]
        skipped = await repo.finish_run(
            run_id,
            RunState.DEPLOYING,
            RunState.FAILED,
            reason="rollout stuck",
            skip_message="Skipped: run failed before execution",
            event_message="Run failed during DEPLOYING: rollout stuck",
            event_payload={"failed_in": "DEPLOYING"},
        )
    assert commits == 1
    assert skipped == 3

    async with db.session() as session:
        run = await RunRepository(session).get_run_or_raise(run_id)
    assert run.state == RunState.FAILED
    assert run.error_message == "rollout stuck"
    assert run.finished_at is not None and run.worker_id is None
    assert [t.status for t in run.tests] == [TestStatus.SKIPPED] * 3
    assert all(t.message == "Skipped: run failed before execution" for t in run.tests)
    assert run.transitions[-1].to_state == RunState.FAILED
    assert run.transitions[-1].reason == "rollout stuck"
    assert run.events[-1].level == "error"
    assert run.events[-1].payload == {"failed_in": "DEPLOYING", "skipped_tests": 3}


async def test_finish_run_leaves_everything_untouched_when_stale(db: Database) -> None:
    async with db.session() as session:
        repo = RunRepository(session)
        run_id = (await repo.create_run(make_config())).id
        await repo.transition(run_id, RunState.PENDING, RunState.PREPARING)
        with pytest.raises(StaleRunError):
            await repo.finish_run(
                run_id,
                RunState.PENDING,  # someone else already moved it on
                RunState.CANCELLED,
                reason="Cancelled by user",
                skip_message="Skipped: run cancelled",
                event_message="Run cancelled while queued",
            )
    async with db.session() as session:
        run = await RunRepository(session).get_run_or_raise(run_id)
    assert run.state == RunState.PREPARING
    assert [t.status for t in run.tests] == [TestStatus.PENDING] * 3
    assert [t.to_state for t in run.transitions] == ["PENDING", "PREPARING"]
    assert [e.message for e in run.events] == ["Run created"]


async def test_finish_run_requires_a_terminal_target(db: Database) -> None:
    async with db.session() as session:
        repo = RunRepository(session)
        run_id = (await repo.create_run(make_config())).id
        with pytest.raises(ValueError):
            await repo.finish_run(
                run_id,
                RunState.PENDING,
                RunState.PREPARING,
                reason="x",
                skip_message="x",
                event_message="x",
            )


async def test_reconcile_skips_leftover_tests_of_finished_runs_only(db: Database) -> None:
    async with db.session() as session:
        repo = RunRepository(session)
        broken = (await repo.create_run(make_config(environment="test"))).id
        active = (await repo.create_run(make_config(environment="stage"))).id
        await repo.transition(broken, RunState.PENDING, RunState.PREPARING)
        await repo.transition(active, RunState.PENDING, RunState.PREPARING)
        # The inconsistent state the old two-step code could leave behind:
        # the run is FAILED but its tests were never marked.
        await repo.transition(broken, RunState.PREPARING, RunState.FAILED, reason="crash")
        first = (await repo.get_run_or_raise(broken)).tests[0]
        await repo.update_test(first.id, status=TestStatus.PASSED)

        assert await repo.reconcile_terminal_runs() == [broken]
        assert await repo.reconcile_terminal_runs() == []  # idempotent

    assert await _statuses(db, broken) == [
        TestStatus.PASSED,
        TestStatus.SKIPPED,
        TestStatus.SKIPPED,
    ]
    assert await _statuses(db, active) == [TestStatus.PENDING] * 3
    async with db.session() as session:
        run = await RunRepository(session).get_run_or_raise(broken)
    assert run.events[-1].level == "warning"
    assert "marked SKIPPED on startup" in run.events[-1].message
