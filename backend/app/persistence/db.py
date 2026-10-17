from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


class Database:
    """Holds the engine and session factory for one application instance."""

    def __init__(self, url: str | None = None, echo: bool | None = None) -> None:
        settings = get_settings()
        self.engine: AsyncEngine = create_async_engine(
            url or settings.database_url,
            echo=settings.database_echo if echo is None else echo,
            pool_pre_ping=True,
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
