"""Entry point of the worker process: `python -m app.worker_service`.

Builds the same settings, database and adapters as the API but runs only the
orchestration `Worker`. There is no HTTP server here; the only link to the API
is the database. `--check` is the container health check: it exits 0 when this
worker's heartbeat row is fresh.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from app.adapters import build_adapters
from app.config import Settings, get_settings
from app.orchestration.worker import Worker
from app.persistence.db import Database
from app.persistence.repository import WorkerRepository, default_worker_id

log = logging.getLogger(__name__)


async def serve(settings: Settings, stop: asyncio.Event, database: Database | None = None) -> None:
    """Run the worker until `stop` is set, then shut it down gracefully."""
    db = database or Database(settings.database_url)
    infrastructure, test_runner = build_adapters(settings)
    worker = Worker(db, settings, infrastructure, test_runner)
    await worker.start()
    log.info("worker %s started (concurrency=%d)", worker.worker_id, settings.worker_concurrency)
    try:
        await stop.wait()
    finally:
        log.info("worker %s stopping", worker.worker_id)
        await worker.stop()
        if database is None:
            await db.dispose()


async def check(settings: Settings) -> bool:
    """True when this worker's heartbeat is younger than the lease window."""
    worker_id = settings.worker_id or default_worker_id()
    db = Database(settings.database_url)
    try:
        async with db.session() as session:
            alive = await WorkerRepository(session).list_alive(settings.worker_lease_seconds)
    finally:
        await db.dispose()
    return any(w.worker_id == worker_id for w in alive)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    args = sys.argv[1:] if argv is None else argv
    if args == ["--check"]:
        return 0 if asyncio.run(check(settings)) else 1

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await serve(settings, stop)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
