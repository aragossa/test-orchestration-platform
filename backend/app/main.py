"""FastAPI application factory.

Wires the database, the mock adapters and the background worker together.
Everything the orchestration engine needs is injected here, so a real
infrastructure or test-runner adapter only needs to be swapped in this file.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.adapters import MockInfrastructureAdapter, MockTestExecutionAdapter
from app.api.routes import router
from app.config import Settings, get_settings
from app.orchestration.worker import Worker
from app.persistence.db import Database
from app.persistence.seed import seed_reference_data

log = logging.getLogger(__name__)


def build_adapters(
    settings: Settings,
) -> tuple[MockInfrastructureAdapter, MockTestExecutionAdapter]:
    infrastructure = MockInfrastructureAdapter(
        prepare_delay_ms=settings.mock_prepare_delay_ms,
        deploy_delay_ms=settings.mock_deploy_delay_ms,
        ready_delay_ms=settings.mock_ready_delay_ms,
        random_failure_rate=settings.mock_random_failure_rate,
    )
    test_runner = MockTestExecutionAdapter(
        delay_scale=settings.mock_test_delay_scale,
        random_failure_rate=settings.mock_random_failure_rate,
    )
    return infrastructure, test_runner


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = database or Database(settings.database_url)
        app.state.db = db
        async with db.session() as session:
            await seed_reference_data(session)

        worker: Worker | None = None
        if settings.worker_enabled:
            infrastructure, test_runner = build_adapters(settings)
            worker = Worker(db, settings, infrastructure, test_runner)
            await worker.start()
            log.info("orchestration worker %s started", worker.worker_id)
        app.state.worker = worker
        try:
            yield
        finally:
            if worker:
                await worker.stop()
            if database is None:
                await db.dispose()

    app = FastAPI(
        title="Test Orchestration Platform",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.worker = None

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        body = detail if isinstance(detail, dict) else {"error": str(detail), "details": []}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
                "message": err.get("msg", "invalid"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422, content={"error": "Invalid request", "details": details}
        )

    app.include_router(router)

    static_dir = Path(settings.static_dir) if settings.static_dir else None
    if static_dir and static_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")
        index = static_dir / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str) -> FileResponse:
            candidate = static_dir / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index)

    return app


app = create_app()
