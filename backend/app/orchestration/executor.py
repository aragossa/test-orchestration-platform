"""Executes one run through the workflow.

The executor is deliberately stateless between steps: every step reloads what
it needs from the database and writes its outcome back before moving on. That
is what makes a run resumable after a crash; the state column says exactly
which step to redo.
"""

from __future__ import annotations

import asyncio
import logging

from app.adapters.base import (
    ExecutionContext,
    InfrastructureAdapter,
    InfrastructureError,
    TestExecutionAdapter,
    TestResult,
)
from app.domain.config import RunConfig
from app.domain.states import (
    ACTIVE_STATES,
    CANCELLABLE_STATES,
    FailureSimulation,
    RunState,
    TestStatus,
    is_terminal,
)
from app.persistence.db import Database
from app.persistence.models import Run, RunTest
from app.persistence.repository import (
    EnvironmentBusyError,
    RunRepository,
    StaleRunError,
    utcnow,
)

log = logging.getLogger(__name__)

CANCEL_POLL_SECONDS = 1.0


class RunCancelledError(Exception):
    pass


class RunExecutor:
    def __init__(
        self,
        db: Database,
        infrastructure: InfrastructureAdapter,
        test_runner: TestExecutionAdapter,
        worker_id: str,
    ) -> None:
        self.db = db
        self.infrastructure = infrastructure
        self.test_runner = test_runner
        self.worker_id = worker_id

    # ------------------------------------------------------------ entry point

    async def execute(self, run_id: int) -> None:
        async with self.db.session() as session:
            repo = RunRepository(session)
            run = await repo.get_run_or_raise(run_id)
            state = RunState(run.state)
            config = RunConfig.model_validate(run.config_snapshot)

            if is_terminal(state):
                return

            if state is RunState.PENDING:
                try:
                    await repo.transition(
                        run_id,
                        RunState.PENDING,
                        RunState.PREPARING,
                        current_step="Preparing environment",
                    )
                except EnvironmentBusyError:
                    await self._park_for_busy_environment(repo, run)
                    return
                await repo.add_event(
                    run_id,
                    f"Run started on worker {self.worker_id}",
                    payload={"worker": self.worker_id},
                )
                state = RunState.PREPARING
            elif state in ACTIVE_STATES:
                await repo.add_event(
                    run_id,
                    f"Run resumed from {state} after backend restart",
                    level="warning",
                    payload={"resumed_from": state, "worker": self.worker_id},
                )

        try:
            while not is_terminal(state):
                if await self._cancel_requested(run_id) and state in CANCELLABLE_STATES:
                    await self._cancel(run_id, state)
                    return
                state = await self._step(run_id, state, config)
        except StaleRunError as exc:
            log.info("run %s: %s", run_id, exc)
        except RunCancelledError:
            await self._cancel(run_id, state)
        except asyncio.CancelledError:
            # Worker shutdown. Leave the row as it is: the lease will expire and
            # the next worker (or this one after restart) resumes the step.
            log.info("run %s: execution interrupted in %s", run_id, state)
            raise
        except InfrastructureError as exc:
            await self._fail(run_id, state, str(exc))
        except Exception as exc:  # noqa: BLE001 - last line of defence, must not leak
            log.exception("run %s: unexpected error in %s", run_id, state)
            await self._fail(run_id, state, f"Unexpected error during {state}: {exc!r}")

    # ------------------------------------------------------------------ steps

    async def _step(self, run_id: int, state: RunState, config: RunConfig) -> RunState:
        match state:
            case RunState.PREPARING:
                return await self._prepare(run_id, config)
            case RunState.DEPLOYING:
                return await self._deploy(run_id, config)
            case RunState.WAITING_FOR_READY:
                return await self._wait_ready(run_id, config)
            case RunState.RUNNING_TESTS:
                return await self._run_tests(run_id, config)
            case RunState.COLLECTING_RESULTS:
                return await self._collect(run_id)
            case _:
                raise RuntimeError(f"No step handler for state {state}")

    async def _prepare(self, run_id: int, config: RunConfig) -> RunState:
        async with self.db.session() as session:
            repo = RunRepository(session)
            await repo.add_event(run_id, f"Preparing environment '{config.environment}'")
        await self.infrastructure.prepare_environment(config.environment, config)
        async with self.db.session() as session:
            repo = RunRepository(session)
            await repo.add_event(run_id, f"Environment '{config.environment}' prepared")
            await repo.transition(
                run_id,
                RunState.PREPARING,
                RunState.DEPLOYING,
                current_step=f"Deploying {config.deployment_version}",
            )
        return RunState.DEPLOYING

    async def _deploy(self, run_id: int, config: RunConfig) -> RunState:
        async with self.db.session() as session:
            repo = RunRepository(session)
            await repo.add_event(
                run_id,
                f"Deployment started: {config.deployment_version}",
                payload={
                    "worker_instances": config.worker_instances,
                    "data_generators": config.data_generators,
                },
            )
        result = await self.infrastructure.deploy(config.environment, config)
        async with self.db.session() as session:
            repo = RunRepository(session)
            await repo.add_event(
                run_id,
                f"Deployment created: {result.deployment_id}"
                + (f" ({result.details})" if result.details else ""),
                payload={"deployment_id": result.deployment_id, "version": result.version},
            )
            await repo.transition(
                run_id,
                RunState.DEPLOYING,
                RunState.WAITING_FOR_READY,
                deployment_id=result.deployment_id,
                current_step="Waiting for deployment readiness",
            )
        return RunState.WAITING_FOR_READY

    async def _wait_ready(self, run_id: int, config: RunConfig) -> RunState:
        async with self.db.session() as session:
            run = await RunRepository(session).get_run_or_raise(run_id)
            deployment_id = run.deployment_id
        if not deployment_id:
            raise InfrastructureError("Deployment id missing; cannot wait for readiness")
        await self.infrastructure.wait_until_ready(deployment_id, config.timeout_seconds * 1000)
        async with self.db.session() as session:
            repo = RunRepository(session)
            await repo.add_event(run_id, f"Deployment {deployment_id} is ready")
            await repo.transition(
                run_id,
                RunState.WAITING_FOR_READY,
                RunState.RUNNING_TESTS,
                current_step="Running tests",
            )
            await repo.add_event(run_id, "Test execution started")
        return RunState.RUNNING_TESTS

    async def _run_tests(self, run_id: int, config: RunConfig) -> RunState:
        async with self.db.session() as session:
            repo = RunRepository(session)
            run = await repo.get_run_or_raise(run_id)
            specs = await repo.get_test_specs(list(config.selected_test_ids))
            tests: list[RunTest] = list(run.tests)
            deployment_id = run.deployment_id or "unknown"

        context = ExecutionContext(
            run_id=run_id,
            environment=config.environment,
            deployment_id=deployment_id,
            config=config,
            timeout_seconds=config.timeout_seconds,
            tests=specs,
            failure_target_test_id=self._failure_target(config),
        )

        total = len(tests)
        for index, run_test in enumerate(tests, start=1):
            if run_test.status not in (TestStatus.PENDING, TestStatus.RUNNING):
                continue  # already finished before a restart
            if await self._cancel_requested(run_id):
                raise RunCancelledError
            await self._run_one_test(run_id, run_test, index, total, context)

        async with self.db.session() as session:
            repo = RunRepository(session)
            await repo.transition(
                run_id,
                RunState.RUNNING_TESTS,
                RunState.COLLECTING_RESULTS,
                current_step="Collecting results",
            )
        return RunState.COLLECTING_RESULTS

    async def _run_one_test(
        self, run_id: int, run_test: RunTest, index: int, total: int, context: ExecutionContext
    ) -> None:
        spec = context.tests.get(run_test.test_id)
        name = spec.name if spec else run_test.test_id
        async with self.db.session() as session:
            repo = RunRepository(session)
            await repo.update_test(
                run_test.id, status=TestStatus.RUNNING, started_at=utcnow(), finished_at=None
            )
            await repo.update_run(run_id, current_step=f"Running test {index} of {total}: {name}")
            await repo.add_event(run_id, f"{name} started", payload={"test_id": run_test.test_id})

        try:
            result = await self._await_with_cancel_watch(
                self.test_runner.execute_test(run_test.test_id, context),
                run_id,
                timeout=context.timeout_seconds,
            )
        except TimeoutError:
            result = TestResult(
                test_id=run_test.test_id,
                status=TestStatus.TIMED_OUT,
                duration_ms=context.timeout_seconds * 1000,
                error_message=f"Test exceeded the run timeout of {context.timeout_seconds} s",
            )

        async with self.db.session() as session:
            repo = RunRepository(session)
            await repo.update_test(
                run_test.id,
                status=result.status,
                duration_ms=result.duration_ms,
                message=result.message,
                error_message=result.error_message,
                finished_at=utcnow(),
            )
            verb = {
                TestStatus.PASSED: "passed",
                TestStatus.FAILED: "failed",
                TestStatus.TIMED_OUT: "timed out",
            }.get(result.status, result.status.lower())
            level = "info" if result.status is TestStatus.PASSED else "error"
            await repo.add_event(
                run_id,
                f"{name} {verb} ({result.duration_ms / 1000:.1f}s)"
                + (f": {result.error_message}" if result.error_message else ""),
                level=level,
                payload={"test_id": run_test.test_id, "status": result.status},
            )

    async def _collect(self, run_id: int) -> RunState:
        async with self.db.session() as session:
            repo = RunRepository(session)
            run = await repo.get_run_or_raise(run_id)
            tests = list(run.tests)
            passed = [t for t in tests if t.status == TestStatus.PASSED]
            not_passed = [t for t in tests if t.status != TestStatus.PASSED]
            summary = f"{len(passed)}/{len(tests)} tests passed"

            if not not_passed:
                await repo.transition(run_id, RunState.COLLECTING_RESULTS, RunState.COMPLETED)
                await repo.add_event(run_id, f"Run completed successfully: {summary}")
                return RunState.COMPLETED

            details = ", ".join(f"{t.test_id}: {t.status}" for t in not_passed)
            reason = f"{len(not_passed)} of {len(tests)} tests did not pass ({details})"
            await repo.finish_run(
                run_id,
                RunState.COLLECTING_RESULTS,
                RunState.FAILED,
                reason=reason,
                skip_message="Skipped: run failed before execution",
                event_message=f"Run failed: {reason}",
            )
            return RunState.FAILED

    # -------------------------------------------------------------- outcomes
    #
    # Finishing a run is a single transaction (RunRepository.finish_run): the
    # state change, the SKIPPED marks on unfinished tests and the event land
    # together, so a crash can never leave a finished run with pending tests.

    async def _fail(self, run_id: int, state: RunState, reason: str) -> None:
        async with self.db.session() as session:
            try:
                await RunRepository(session).finish_run(
                    run_id,
                    state,
                    RunState.FAILED,
                    reason=reason,
                    skip_message="Skipped: run failed before execution",
                    event_message=f"Run failed during {state}: {reason}",
                    event_payload={"failed_in": state},
                )
            except StaleRunError as exc:
                log.info("run %s: could not mark failed: %s", run_id, exc)

    async def _cancel(self, run_id: int, state: RunState) -> None:
        async with self.db.session() as session:
            try:
                await RunRepository(session).finish_run(
                    run_id,
                    state,
                    RunState.CANCELLED,
                    reason="Cancelled by user",
                    skip_message="Skipped: run cancelled",
                    event_message=f"Run cancelled during {state}",
                    event_level="warning",
                )
            except StaleRunError as exc:
                log.info("run %s: could not cancel: %s", run_id, exc)

    async def _park_for_busy_environment(self, repo: RunRepository, run: Run) -> None:
        """Leave a queued run in PENDING until its environment frees up."""
        waiting_step = f"Waiting for environment '{run.environment_id}' to become free"
        if run.current_step != waiting_step:
            await repo.add_event(
                run.id,
                f"Environment '{run.environment_id}' is busy with another run; waiting",
                level="warning",
            )
        await repo.update_run(run.id, current_step=waiting_step)
        await repo.release_claim(run.id, self.worker_id)

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _failure_target(config: RunConfig) -> str | None:
        if config.failure_simulation in (
            FailureSimulation.TEST_FAILURE,
            FailureSimulation.TEST_TIMEOUT,
        ):
            return config.selected_test_ids[0]
        return None

    async def _cancel_requested(self, run_id: int) -> bool:
        async with self.db.session() as session:
            return await RunRepository(session).is_cancel_requested(run_id)

    async def _watch_cancel(self, run_id: int) -> None:
        while True:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
            if await self._cancel_requested(run_id):
                return

    async def _await_with_cancel_watch(
        self, awaitable: asyncio.Future[TestResult] | object, run_id: int, timeout: float
    ) -> TestResult:
        """Await a test while honouring both the run timeout and cancellation."""
        task: asyncio.Task[TestResult] = asyncio.ensure_future(awaitable)  # type: ignore[arg-type]
        watcher = asyncio.create_task(self._watch_cancel(run_id))
        try:
            done, _ = await asyncio.wait(
                {task, watcher}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                return task.result()
            if watcher in done:
                raise RunCancelledError
            raise TimeoutError
        finally:
            for pending in (task, watcher):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)
