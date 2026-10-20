"""Data access for runs.

Every state change goes through `transition()`, which combines the domain
check (is this transition legal?) with an optimistic concurrency check (is the
row still in the state we think it is?). Both must pass or nothing is written.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import TestSpec
from app.domain.config import RunConfig
from app.domain.states import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    RunState,
    TestStatus,
    assert_transition,
)
from app.persistence.models import (
    Environment,
    Run,
    RunEvent,
    RunTest,
    RunTransition,
    TestDefinition,
    WorkerRecord,
)

log = logging.getLogger(__name__)


class StaleRunError(Exception):
    """The run changed under us: its state no longer matches what we expected."""

    def __init__(self, run_id: int, expected: RunState, target: RunState) -> None:
        self.run_id = run_id
        self.expected = expected
        self.target = target
        super().__init__(f"Run {run_id} is no longer in {expected}; refused transition to {target}")


class EnvironmentBusyError(Exception):
    """Another run is already active in the target environment."""

    def __init__(self, run_id: int, environment: str) -> None:
        self.run_id = run_id
        self.environment = environment
        super().__init__(f"Environment '{environment}' is busy; run {run_id} must wait")


@dataclass(frozen=True)
class WorkerStatus:
    worker_id: str
    concurrency: int
    active_runs: int
    started_at: datetime
    last_heartbeat_at: datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def default_worker_id() -> str:
    return socket.gethostname()


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads

    async def get_run(self, run_id: int) -> Run | None:
        return await self.session.get(Run, run_id)

    async def get_run_or_raise(self, run_id: int) -> Run:
        run = await self.get_run(run_id)
        if run is None:
            raise LookupError(f"Run {run_id} not found")
        return run

    async def list_runs(
        self,
        *,
        environment: str | None = None,
        state: RunState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Run], int]:
        stmt = select(Run)
        count_stmt = select(func.count()).select_from(Run)
        if environment:
            stmt = stmt.where(Run.environment_id == environment)
            count_stmt = count_stmt.where(Run.environment_id == environment)
        if state:
            stmt = stmt.where(Run.state == state)
            count_stmt = count_stmt.where(Run.state == state)
        stmt = stmt.order_by(Run.id.desc()).limit(limit).offset(offset)
        runs = list((await self.session.scalars(stmt)).all())
        total = (await self.session.scalar(count_stmt)) or 0
        return runs, total

    async def get_environment(self, environment_id: str) -> Environment | None:
        return await self.session.get(Environment, environment_id)

    async def list_environments(self) -> list[Environment]:
        stmt = select(Environment).order_by(Environment.id)
        return list((await self.session.scalars(stmt)).all())

    async def list_test_definitions(self) -> list[TestDefinition]:
        stmt = select(TestDefinition).order_by(TestDefinition.sort_order, TestDefinition.id)
        return list((await self.session.scalars(stmt)).all())

    async def get_test_specs(self, test_ids: list[str]) -> dict[str, TestSpec]:
        stmt = select(TestDefinition).where(TestDefinition.id.in_(test_ids))
        rows = (await self.session.scalars(stmt)).all()
        return {
            row.id: TestSpec(
                test_id=row.id,
                name=row.name,
                category=row.category,
                base_duration_ms=row.base_duration_ms,
            )
            for row in rows
        }

    async def is_cancel_requested(self, run_id: int) -> bool:
        stmt = select(Run.cancel_requested_at).where(Run.id == run_id)
        return (await self.session.scalar(stmt)) is not None

    # ---------------------------------------------------------------- writes

    async def create_run(self, config: RunConfig) -> Run:
        run = Run(
            environment_id=config.environment,
            state=RunState.PENDING,
            config_snapshot=config.model_dump(mode="json"),
            failure_simulation=config.failure_simulation,
            current_step="Queued",
        )
        for position, test_id in enumerate(config.selected_test_ids):
            run.tests.append(RunTest(test_id=test_id, position=position, status=TestStatus.PENDING))
        run.transitions.append(RunTransition(from_state=None, to_state=RunState.PENDING))
        run.events.append(
            RunEvent(
                level="info",
                message="Run created",
                payload={"environment": config.environment, "tests": len(config.selected_test_ids)},
            )
        )
        self.session.add(run)
        await self.session.commit()
        await self.session.refresh(run)
        return run

    async def transition(
        self,
        run_id: int,
        expected: RunState,
        target: RunState,
        *,
        reason: str | None = None,
        **fields: Any,
    ) -> None:
        """Move a run from `expected` to `target`, atomically.

        Raises InvalidTransitionError if the domain forbids it, StaleRunError if
        the row is not in `expected` any more, and EnvironmentBusyError if the
        move would make a second run active in the same environment.
        """
        await self._apply_transition(run_id, expected, target, reason, fields)
        await self.session.commit()
        log.info("run %s: %s -> %s%s", run_id, expected, target, f" ({reason})" if reason else "")

    async def finish_run(
        self,
        run_id: int,
        expected: RunState,
        target: RunState,
        *,
        reason: str,
        skip_message: str,
        event_message: str,
        event_level: str = "error",
        event_payload: dict[str, Any] | None = None,
        **fields: Any,
    ) -> int:
        """Move a run to a terminal state, skip its unfinished tests and record
        the event, all in ONE transaction.

        A run must never be observed as finished while some of its tests are
        still PENDING or RUNNING, so the three writes are committed together:
        either all of them land or none does. Returns the number of tests that
        were marked SKIPPED. Raises the same errors as `transition()`.
        """
        if target not in TERMINAL_STATES:
            raise ValueError(f"finish_run() needs a terminal target, got {target}")
        fields.setdefault("error_message", reason)
        await self._apply_transition(run_id, expected, target, reason, fields)
        skipped = await self._skip_unfinished_tests(run_id, skip_message)
        self.session.add(
            RunEvent(
                run_id=run_id,
                level=event_level,
                message=event_message,
                payload={**(event_payload or {}), "skipped_tests": skipped},
            )
        )
        await self.session.commit()
        log.log(
            logging.WARNING if event_level in ("warning", "error") else logging.INFO,
            "run %s: %s -> %s (%s); %d test(s) skipped",
            run_id,
            expected,
            target,
            reason,
            skipped,
        )
        return skipped

    async def _apply_transition(
        self,
        run_id: int,
        expected: RunState,
        target: RunState,
        reason: str | None,
        fields: dict[str, Any],
    ) -> None:
        """Stage the state change and its history row without committing."""
        assert_transition(expected, target)
        now = utcnow()
        values: dict[str, Any] = {"state": target, "updated_at": now, **fields}
        if target is RunState.PREPARING and "started_at" not in fields:
            values["started_at"] = now
        if target in TERMINAL_STATES:
            values.setdefault("finished_at", now)
            values.setdefault("current_step", None)
            values.setdefault("worker_id", None)
            values.setdefault("lease_expires_at", None)

        stmt = (
            update(Run)
            .where(Run.id == run_id, Run.state == expected)
            .values(**values)
            .returning(Run.environment_id)
        )
        try:
            environment_id = (await self.session.execute(stmt)).scalar_one_or_none()
        except IntegrityError as exc:
            await self.session.rollback()
            if "uq_runs_one_active_per_environment" in str(exc.orig):
                env = await self.session.scalar(select(Run.environment_id).where(Run.id == run_id))
                raise EnvironmentBusyError(run_id, env or "?") from exc
            raise
        if environment_id is None:
            await self.session.rollback()
            raise StaleRunError(run_id, expected, target)

        self.session.add(
            RunTransition(run_id=run_id, from_state=expected, to_state=target, reason=reason)
        )

    async def update_run(self, run_id: int, **fields: Any) -> None:
        stmt = update(Run).where(Run.id == run_id).values(updated_at=utcnow(), **fields)
        await self.session.execute(stmt)
        await self.session.commit()

    async def add_event(
        self,
        run_id: int,
        message: str,
        *,
        level: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(RunEvent(run_id=run_id, level=level, message=message, payload=payload))
        await self.session.commit()
        log.log(
            logging.WARNING if level in ("warning", "error") else logging.INFO,
            "run %s: %s",
            run_id,
            message,
        )

    async def update_test(self, run_test_id: int, **fields: Any) -> None:
        stmt = update(RunTest).where(RunTest.id == run_test_id).values(**fields)
        await self.session.execute(stmt)
        await self.session.commit()

    async def _skip_unfinished_tests(self, run_id: int, message: str) -> int:
        """Stage SKIPPED for the run's unfinished tests without committing.

        Private on purpose: skipping tests is only ever part of finishing a
        run, and must commit together with the state change (see finish_run).
        """
        stmt = (
            update(RunTest)
            .where(
                RunTest.run_id == run_id,
                RunTest.status.in_([TestStatus.PENDING, TestStatus.RUNNING]),
            )
            .values(status=TestStatus.SKIPPED, message=message, finished_at=utcnow())
        )
        result = await self.session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def reconcile_terminal_runs(self) -> list[int]:
        """Repair runs that finished but still have PENDING/RUNNING tests.

        `finish_run` makes this impossible for new runs; this sweep is the
        safety net for rows written before it existed. Returns the repaired
        run ids. Idempotent.
        """
        terminal_runs = select(Run.id).where(Run.state.in_(list(TERMINAL_STATES)))
        stmt = (
            update(RunTest)
            .where(
                RunTest.run_id.in_(terminal_runs),
                RunTest.status.in_([TestStatus.PENDING, TestStatus.RUNNING]),
            )
            .values(
                status=TestStatus.SKIPPED,
                message="Skipped: run had already finished (repaired on worker startup)",
                finished_at=utcnow(),
            )
            .returning(RunTest.run_id)
        )
        run_ids = sorted(set((await self.session.scalars(stmt)).all()))
        for run_id in run_ids:
            self.session.add(
                RunEvent(
                    run_id=run_id,
                    level="warning",
                    message="Unfinished tests of a finished run were marked SKIPPED on startup",
                )
            )
        await self.session.commit()
        return run_ids

    async def request_cancel(self, run_id: int) -> None:
        await self.update_run(run_id, cancel_requested_at=utcnow())

    # ------------------------------------------------------- worker leasing

    async def claim_pending_runs(self, worker_id: str, lease_seconds: int, limit: int) -> list[int]:
        """Take ownership of queued runs whose environment is free.

        FOR UPDATE SKIP LOCKED lets several workers poll concurrently without
        ever handing the same run to two of them. Only the oldest queued run
        per environment is taken in one pass, so two runs targeting the same
        environment are never started together.
        """
        if limit <= 0:
            return []
        now = utcnow()
        other = Run.__table__.alias("other")
        env_busy = exists(
            select(1).where(
                other.c.environment_id == Run.environment_id,
                other.c.state.in_(list(ACTIVE_STATES)),
            )
        )
        candidates = (
            select(Run.id, Run.environment_id)
            .where(
                Run.state == RunState.PENDING,
                or_(Run.worker_id.is_(None), Run.lease_expires_at < now),
                ~env_busy,
            )
            .order_by(Run.id)
            .with_for_update(skip_locked=True)
        )
        chosen: list[int] = []
        seen_environments: set[str] = set()
        for run_id, environment_id in (await self.session.execute(candidates)).all():
            if environment_id in seen_environments:
                continue
            seen_environments.add(environment_id)
            chosen.append(run_id)
            if len(chosen) == limit:
                break
        if chosen:
            await self.session.execute(
                update(Run)
                .where(Run.id.in_(chosen))
                .values(
                    worker_id=worker_id,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    updated_at=now,
                )
            )
        await self.session.commit()
        return chosen

    async def reclaim_abandoned_runs(
        self, worker_id: str, lease_seconds: int, limit: int, *, include_own: bool = False
    ) -> list[int]:
        """Take over active runs whose owner stopped renewing its lease.

        With include_own=True (used at startup) runs that this very worker owned
        before a restart are taken back immediately, without waiting for the
        lease to expire.
        """
        if limit <= 0:
            return []
        now = utcnow()
        ownership = Run.lease_expires_at < now
        if include_own:
            ownership = or_(ownership, Run.worker_id == worker_id)
        candidates = (
            select(Run.id)
            .where(Run.state.in_(list(ACTIVE_STATES)), ownership)
            .order_by(Run.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(Run)
            .where(Run.id.in_(candidates))
            .values(
                worker_id=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
            .returning(Run.id)
        )
        ids = sorted((await self.session.scalars(stmt)).all())
        await self.session.commit()
        return ids

    async def extend_lease(self, run_id: int, worker_id: str, lease_seconds: int) -> bool:
        now = utcnow()
        stmt = (
            update(Run)
            .where(Run.id == run_id, Run.worker_id == worker_id)
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            .returning(Run.id)
        )
        renewed = (await self.session.execute(stmt)).scalar_one_or_none() is not None
        await self.session.commit()
        return renewed

    async def release_claim(self, run_id: int, worker_id: str) -> None:
        stmt = (
            update(Run)
            .where(Run.id == run_id, Run.worker_id == worker_id)
            .values(worker_id=None, lease_expires_at=None, updated_at=utcnow())
        )
        await self.session.execute(stmt)
        await self.session.commit()


class WorkerRepository:
    """Liveness records in the `workers` table. Separate from runs on purpose:
    a heartbeat says nothing about any run, it only says the process is up."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def heartbeat(self, worker_id: str, concurrency: int, started_at: datetime) -> None:
        now = utcnow()
        stmt = pg_insert(WorkerRecord).values(
            id=worker_id,
            concurrency=concurrency,
            started_at=started_at,
            last_heartbeat_at=now,
            stopped_at=None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[WorkerRecord.id],
            set_={
                "concurrency": concurrency,
                "started_at": started_at,
                "last_heartbeat_at": now,
                "stopped_at": None,
            },
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def mark_stopped(self, worker_id: str) -> None:
        stmt = update(WorkerRecord).where(WorkerRecord.id == worker_id).values(stopped_at=utcnow())
        await self.session.execute(stmt)
        await self.session.commit()

    async def list_alive(self, stale_after_seconds: int) -> list[WorkerStatus]:
        """Workers that heartbeat within the window and did not stop gracefully,
        with the number of active runs each one currently owns."""
        cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
        active = (
            select(Run.worker_id, func.count().label("n"))
            .where(Run.state.in_(list(ACTIVE_STATES)))
            .group_by(Run.worker_id)
            .subquery()
        )
        stmt = (
            select(WorkerRecord, func.coalesce(active.c.n, 0))
            .outerjoin(active, active.c.worker_id == WorkerRecord.id)
            .where(WorkerRecord.stopped_at.is_(None), WorkerRecord.last_heartbeat_at >= cutoff)
            .order_by(WorkerRecord.id)
        )
        return [
            WorkerStatus(
                worker_id=record.id,
                concurrency=record.concurrency,
                active_runs=int(count),
                started_at=record.started_at,
                last_heartbeat_at=record.last_heartbeat_at,
            )
            for record, count in (await self.session.execute(stmt)).all()
        ]
