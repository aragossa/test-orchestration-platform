"""Application settings, read from environment variables (or a .env file)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://orchestrator:orchestrator@localhost:5432/orchestrator"
    database_echo: bool = False

    # Execution worker
    worker_enabled: bool = True
    worker_id: str | None = None  # defaults to hostname:pid
    worker_poll_interval_ms: int = Field(default=1000, ge=10)
    worker_concurrency: int = Field(default=4, ge=1, le=64)
    worker_lease_seconds: int = Field(default=15, ge=1)
    worker_heartbeat_seconds: int = Field(default=5, ge=1)

    # Mock adapter delays. Zero makes the whole workflow instant (used in tests).
    mock_prepare_delay_ms: int = Field(default=2000, ge=0)
    mock_deploy_delay_ms: int = Field(default=3000, ge=0)
    mock_ready_delay_ms: int = Field(default=2000, ge=0)
    mock_test_delay_scale: float = Field(default=1.0, ge=0)
    # Optional chaos: probability that a mock operation fails even with
    # failure_simulation=none. Off by default so evaluation is reproducible.
    mock_random_failure_rate: float = Field(default=0.0, ge=0, le=1)

    cors_origins: list[str] = ["http://localhost:5173"]
    static_dir: str | None = None  # built frontend, served at / when set


@lru_cache
def get_settings() -> Settings:
    return Settings()
