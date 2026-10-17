"""Persistence guarantees: legal transitions only, optimistic locking, env lock."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.states import InvalidTransitionError, RunState
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
