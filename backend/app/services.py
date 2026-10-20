"""Use cases exposed by the API. Keeps request handling out of the routes."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.config import RunConfig
from app.domain.states import CANCELLABLE_STATES, RunState
from app.persistence.models import Environment, Run
from app.persistence.repository import RunRepository, StaleRunError, utcnow


class ValidationFailed(Exception):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


@dataclass(frozen=True)
class EnvironmentDefaults:
    worker_instances: int | None = None
    data_generators: int | None = None
    deployment_version: str | None = None
    timeout_seconds: int | None = None


class RunService:
    def __init__(self, session: AsyncSession) -> None:
        self.repo = RunRepository(session)

    async def create_run(self, config: RunConfig) -> Run:
        errors: list[dict[str, str]] = []
        environment = await self.repo.get_environment(config.environment)
        if environment is None:
            known = ", ".join(e.id for e in await self.repo.list_environments())
            errors.append(
                {
                    "field": "environment",
                    "message": f"Unknown environment '{config.environment}'. Known: {known}",
                }
            )
        known_tests = {t.id for t in await self.repo.list_test_definitions()}
        unknown = [t for t in config.selected_test_ids if t not in known_tests]
        if unknown:
            errors.append(
                {
                    "field": "selected_test_ids",
                    "message": f"Unknown test id(s): {', '.join(unknown)}",
                }
            )
        if errors:
            raise ValidationFailed(errors)
        return await self.repo.create_run(config)

    async def get_run(self, run_id: int) -> Run:
        run = await self.repo.get_run(run_id)
        if run is None:
            raise NotFound(f"Run {run_id} not found")
        return run

    async def list_runs(
        self, *, environment: str | None, state: RunState | None, limit: int, offset: int
    ) -> tuple[list[Run], int]:
        return await self.repo.list_runs(
            environment=environment, state=state, limit=limit, offset=offset
        )

    async def cancel_run(self, run_id: int) -> Run:
        run = await self.get_run(run_id)
        state = RunState(run.state)
        if state not in CANCELLABLE_STATES:
            raise Conflict(f"Run {run_id} is in {state} and cannot be cancelled")
        if run.cancel_requested_at is not None:
            return run

        if state is RunState.PENDING:
            # Nothing is executing yet; finish it right here.
            try:
                await self.repo.finish_run(
                    run_id,
                    RunState.PENDING,
                    RunState.CANCELLED,
                    reason="Cancelled by user",
                    skip_message="Skipped: run cancelled",
                    event_message="Run cancelled while queued",
                    event_level="warning",
                    cancel_requested_at=utcnow(),
                )
                self.repo.session.expire_all()
                return await self.get_run(run_id)
            except StaleRunError:
                pass  # a worker picked it up meanwhile; fall through to the flag

        await self.repo.request_cancel(run_id)
        await self.repo.add_event(run_id, "Cancellation requested by user", level="warning")
        self.repo.session.expire_all()
        return await self.get_run(run_id)

    async def list_environments(self) -> list[Environment]:
        return await self.repo.list_environments()

    async def update_environment(
        self, environment_id: str, defaults: EnvironmentDefaults
    ) -> Environment:
        environment = await self.repo.get_environment(environment_id)
        if environment is None:
            raise NotFound(f"Environment '{environment_id}' not found")
        if defaults.worker_instances is not None:
            environment.default_worker_instances = defaults.worker_instances
        if defaults.data_generators is not None:
            environment.default_data_generators = defaults.data_generators
        if defaults.deployment_version is not None:
            environment.default_deployment_version = defaults.deployment_version
        if defaults.timeout_seconds is not None:
            environment.default_timeout_seconds = defaults.timeout_seconds
        await self.repo.session.commit()
        await self.repo.session.refresh(environment)
        return environment
