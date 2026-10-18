from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.api.presenters import to_detail, to_summary
from app.api.schemas import (
    EnvironmentOut,
    EnvironmentUpdate,
    ErrorOut,
    HealthOut,
    RunCreate,
    RunDetailOut,
    RunListOut,
    TestDefinitionOut,
    WorkerOut,
)
from app.domain.config import RunConfig
from app.domain.states import RunState
from app.persistence.repository import WorkerRepository
from app.services import Conflict, EnvironmentDefaults, NotFound, RunService, ValidationFailed

router = APIRouter(prefix="/api")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.db.session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def service(session: SessionDep) -> RunService:
    return RunService(session)


ServiceDep = Annotated[RunService, Depends(service)]

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
    422: {"model": ErrorOut},
}


# ----------------------------------------------------------------- reference


@router.get("/environments", response_model=list[EnvironmentOut])
async def list_environments(svc: ServiceDep) -> list[EnvironmentOut]:
    return [EnvironmentOut.model_validate(e) for e in await svc.list_environments()]


@router.put(
    "/environments/{environment_id}", response_model=EnvironmentOut, responses=ERROR_RESPONSES
)
async def update_environment(
    environment_id: str, payload: EnvironmentUpdate, svc: ServiceDep
) -> EnvironmentOut:
    try:
        env = await svc.update_environment(
            environment_id,
            EnvironmentDefaults(
                worker_instances=payload.default_worker_instances,
                data_generators=payload.default_data_generators,
                deployment_version=payload.default_deployment_version,
                timeout_seconds=payload.default_timeout_seconds,
            ),
        )
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return EnvironmentOut.model_validate(env)


@router.get("/tests", response_model=list[TestDefinitionOut])
async def list_tests(session: SessionDep) -> list[TestDefinitionOut]:
    from app.persistence.repository import RunRepository

    rows = await RunRepository(session).list_test_definitions()
    return [TestDefinitionOut.model_validate(r) for r in rows]


# ---------------------------------------------------------------------- runs


@router.post(
    "/runs",
    response_model=RunDetailOut,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
async def create_run(payload: RunCreate, svc: ServiceDep) -> RunDetailOut:
    try:
        config = RunConfig(
            environment=payload.environment,
            deployment_version=payload.deployment_version,
            worker_instances=payload.worker_instances,
            data_generators=payload.data_generators,
            timeout_seconds=payload.timeout_seconds,
            selected_test_ids=tuple(payload.selected_test_ids),
            failure_simulation=payload.failure_simulation,
        )
    except ValidationError as exc:
        details = [
            {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]}
            for err in exc.errors()
        ]
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "Invalid run configuration", "details": details},
        ) from exc
    try:
        run = await svc.create_run(config)
    except ValidationFailed as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "Invalid run configuration", "details": exc.errors},
        ) from exc
    return to_detail(run)


@router.get("/runs", response_model=RunListOut)
async def list_runs(
    svc: ServiceDep,
    environment: str | None = None,
    state: RunState | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RunListOut:
    runs, total = await svc.list_runs(
        environment=environment, state=state, limit=limit, offset=offset
    )
    return RunListOut(items=[to_summary(r) for r in runs], total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}", response_model=RunDetailOut, responses=ERROR_RESPONSES)
async def get_run(run_id: int, svc: ServiceDep) -> RunDetailOut:
    try:
        run = await svc.get_run(run_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return to_detail(run)


@router.post("/runs/{run_id}/cancel", response_model=RunDetailOut, responses=ERROR_RESPONSES)
async def cancel_run(run_id: int, svc: ServiceDep) -> RunDetailOut:
    try:
        run = await svc.cancel_run(run_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Conflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return to_detail(run)


# -------------------------------------------------------------------- health


@router.get("/health", response_model=HealthOut)
async def health(request: Request, session: SessionDep) -> HealthOut:
    """`ok` needs a reachable database and at least one live worker. Workers
    run in their own processes, so liveness comes from their heartbeat rows:
    a heartbeat older than the lease window means the worker is gone."""
    workers: list[WorkerOut] = []
    try:
        await session.execute(text("SELECT 1"))
        database = "ok"
        stale_after = request.app.state.settings.worker_lease_seconds
        alive = await WorkerRepository(session).list_alive(stale_after)
        workers = [WorkerOut(**vars(w)) for w in alive]
    except Exception as exc:  # noqa: BLE001
        database = f"error: {exc.__class__.__name__}"
    return HealthOut(
        status="ok" if database == "ok" and workers else "degraded",
        database=database,
        workers=workers,
        version=__version__,
    )
