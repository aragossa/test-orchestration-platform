"""Request and response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.config import MAX_INSTANCES, MIN_INSTANCES
from app.domain.states import FailureSimulation, RunState, TestStatus


class EnvironmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    display_name: str
    default_worker_instances: int
    default_data_generators: int
    default_deployment_version: str
    default_timeout_seconds: int
    updated_at: datetime


class EnvironmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_worker_instances: int | None = Field(default=None, ge=MIN_INSTANCES, le=MAX_INSTANCES)
    default_data_generators: int | None = Field(default=None, ge=MIN_INSTANCES, le=MAX_INSTANCES)
    default_deployment_version: str | None = Field(default=None, min_length=1, max_length=100)
    default_timeout_seconds: int | None = Field(default=None, gt=0, le=3600)


class TestDefinitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    category: str
    description: str
    base_duration_ms: int


class RunCreate(BaseModel):
    """Mirrors RunConfig but lives here so the API contract can evolve separately."""

    model_config = ConfigDict(extra="forbid")

    environment: str
    deployment_version: str = Field(min_length=1, max_length=100)
    worker_instances: int = Field(ge=MIN_INSTANCES, le=MAX_INSTANCES)
    data_generators: int = Field(ge=MIN_INSTANCES, le=MAX_INSTANCES)
    timeout_seconds: int = Field(gt=0, le=3600)
    selected_test_ids: list[str] = Field(min_length=1)
    failure_simulation: FailureSimulation = FailureSimulation.NONE


class RunTestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    test_id: str
    name: str
    category: str
    position: int
    status: TestStatus
    duration_ms: int | None
    message: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None


class TransitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_state: RunState | None
    to_state: RunState
    reason: str | None
    created_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    level: str
    message: str
    payload: dict[str, Any] | None
    created_at: datetime


class TestCounts(BaseModel):
    total: int
    passed: int
    failed: int
    timed_out: int
    skipped: int
    pending: int


class RunSummaryOut(BaseModel):
    id: int
    label: str
    environment: str
    state: RunState
    is_terminal: bool
    current_step: str | None
    final_result: str | None
    error_message: str | None
    failure_simulation: FailureSimulation
    selected_test_ids: list[str]
    test_counts: TestCounts
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None


class RunDetailOut(RunSummaryOut):
    config: dict[str, Any]
    deployment_id: str | None
    worker_id: str | None
    cancel_requested_at: datetime | None
    tests: list[RunTestOut]
    transitions: list[TransitionOut]
    events: list[EventOut]


class RunListOut(BaseModel):
    items: list[RunSummaryOut]
    total: int
    limit: int
    offset: int


class ErrorDetail(BaseModel):
    field: str | None = None
    message: str


class ErrorOut(BaseModel):
    error: str
    details: list[ErrorDetail] = []


class HealthOut(BaseModel):
    status: str
    database: str
    worker: dict[str, Any]
    version: str
