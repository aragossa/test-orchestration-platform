"""Background worker: claims runs from the database and executes them.

The worker is a polling loop in its own process (`python -m app.worker_service`);
it shares nothing with the API but the database. The database is the queue;
`worker_id` and `lease_expires_at` on the run row record who is working on it
and until when. A run whose lease lapses is considered abandoned and is picked
up again, which is how execution survives a worker restart. Each pass also
upserts the worker's own heartbeat row so the API can report who is alive.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from app.adapters.base import InfrastructureAdapter, TestExecutionAdapter
from app.config import Settings
from app.orchestration.executor import RunExecutor
from app.persistence.db import Database
from app.persistence.repository import (
    RunRepository,
    WorkerRepository,
    default_worker_id,
    utcnow,
)

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        infrastructure: InfrastructureAdapter,
        test_runner: TestExecutionAdapter,
    ) -> None:
        self.db = db
        self.settings = settings
        self.worker_id = settings.worker_id or default_worker_id()
        # Identity vs fencing: worker_id is stable across restarts (hostname) and
        # is what heartbeats and startup recovery key on; lease_token is unique
        # to this process start and is what every write to a run is fenced on.
        self.lease_token = uuid.uuid4().hex
        self.executor = RunExecutor(
            db, infrastructure, test_runner, self.worker_id, self.lease_token
        )
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()
        self._main: asyncio.Task[None] | None = None
        self.started_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.recovered_run_ids: list[int] = []
        self.reconciled_run_ids: list[int] = []

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self.started_at = utcnow()
        self._stop.clear()
        self._main = asyncio.create_task(self._loop(), name="orchestration-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._main:
            self._main.cancel()
            await asyncio.gather(self._main, return_exceptions=True)
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        try:
            async with self.db.session() as session:
                await WorkerRepository(session).mark_stopped(self.worker_id)
        except Exception:  # noqa: BLE001
            log.exception("worker %s: could not record shutdown", self.worker_id)

    def status(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "lease_token": self.lease_token,
            "running": self._main is not None and not self._main.done(),
            "active_runs": sorted(self._tasks),
            "concurrency": self.settings.worker_concurrency,
            "started_at": self.started_at,
            "last_tick_at": self.last_tick_at,
            "recovered_on_startup": self.recovered_run_ids,
            "reconciled_on_startup": self.reconciled_run_ids,
        }

    # ------------------------------------------------------------------ loop

    async def _loop(self) -> None:
        try:
            await self._recover_on_startup()
        except Exception:  # noqa: BLE001
            log.exception("worker %s: startup recovery failed", self.worker_id)
        interval = self.settings.worker_poll_interval_ms / 1000
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("worker %s: tick failed", self.worker_id)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _recover_on_startup(self) -> None:
        """Repair finished runs that still have unfinished tests, then take back
        runs this worker owned before it restarted, plus any whose lease already
        lapsed, and resume them."""
        async with self.db.session() as session:
            repo = RunRepository(session)
            self.reconciled_run_ids = await repo.reconcile_terminal_runs()
            if self.reconciled_run_ids:
                log.warning(
                    "worker %s: marked leftover tests SKIPPED on finished run(s) %s",
                    self.worker_id,
                    self.reconciled_run_ids,
                )
            # Taking back "my own" runs assumes the previous holder of this id is
            # gone. If a process with the same id is still heartbeating, the id is
            # shared by mistake (WORKER_ID set with several replicas): do not rob
            # it; fall back to ordinary lease expiry.
            include_own = not await WorkerRepository(session).is_alive(
                self.worker_id, self.settings.worker_lease_seconds
            )
            if not include_own:
                log.warning(
                    "worker %s: another live worker uses the same id; not reclaiming its runs "
                    "(set a unique WORKER_ID per process or leave it empty)",
                    self.worker_id,
                )
            ids = await repo.reclaim_abandoned_runs(
                self.worker_id,
                self.lease_token,
                self.settings.worker_lease_seconds,
                self.settings.worker_concurrency,
                include_own=include_own,
            )
        self.recovered_run_ids = ids
        if ids:
            log.warning(
                "worker %s: resuming %d interrupted run(s): %s", self.worker_id, len(ids), ids
            )
        for run_id in ids:
            self._launch(run_id)

    async def tick(self) -> list[int]:
        """One scheduling pass. Returns the ids launched in this pass."""
        self.last_tick_at = utcnow()
        self._reap_finished()
        async with self.db.session() as session:
            await WorkerRepository(session).heartbeat(
                self.worker_id, self.settings.worker_concurrency, self.started_at or utcnow()
            )
        free = self.settings.worker_concurrency - len(self._tasks)
        if free <= 0:
            return []

        launched: list[int] = []
        async with self.db.session() as session:
            repo = RunRepository(session)
            abandoned = await repo.reclaim_abandoned_runs(
                self.worker_id, self.lease_token, self.settings.worker_lease_seconds, free
            )
            launched.extend(abandoned)
            pending = await repo.claim_pending_runs(
                self.worker_id,
                self.lease_token,
                self.settings.worker_lease_seconds,
                free - len(abandoned),
            )
            launched.extend(pending)
        if pending:
            log.info("worker %s: claimed run(s) %s", self.worker_id, pending)
        if abandoned:
            log.warning("worker %s: took over abandoned run(s) %s", self.worker_id, abandoned)

        for run_id in launched:
            self._launch(run_id)
        return launched

    def _reap_finished(self) -> None:
        for run_id, task in list(self._tasks.items()):
            if task.done():
                del self._tasks[run_id]
                if not task.cancelled() and task.exception():
                    log.error("run %s task crashed: %r", run_id, task.exception())

    def _launch(self, run_id: int) -> None:
        if run_id in self._tasks and not self._tasks[run_id].done():
            return
        self._tasks[run_id] = asyncio.create_task(self._run(run_id), name=f"run-{run_id}")

    async def _run(self, run_id: int) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(run_id), name=f"lease-{run_id}")
        try:
            await self.executor.execute(run_id)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, run_id: int) -> None:
        interval = self.settings.worker_heartbeat_seconds
        while True:
            await asyncio.sleep(interval)
            async with self.db.session() as session:
                renewed = await RunRepository(session).extend_lease(
                    run_id, self.lease_token, self.settings.worker_lease_seconds
                )
            if not renewed:
                # Someone else owns the run now (or it finished). Stop working on it.
                log.warning(
                    "worker %s lost the lease on run %s; stopping it", self.worker_id, run_id
                )
                task = self._tasks.get(run_id)
                if task and not task.done():
                    task.cancel()
                return

    async def wait_idle(self, timeout: float = 30.0) -> None:
        """Test helper: wait until no run tasks are in flight."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            self._reap_finished()
            if not self._tasks:
                return
            await asyncio.sleep(0.02)
        raise TimeoutError(f"worker still busy with {sorted(self._tasks)}")
