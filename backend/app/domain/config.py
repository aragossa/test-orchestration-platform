"""Run configuration model.

A RunConfig is the immutable snapshot that gets stored with every run.
Validation lives here so the API layer and the engine share one definition.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.states import FailureSimulation

MIN_INSTANCES = 1
MAX_INSTANCES = 5


class RunConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: str = Field(min_length=1)
    deployment_version: str = Field(min_length=1, max_length=100)
    worker_instances: int = Field(ge=MIN_INSTANCES, le=MAX_INSTANCES)
    data_generators: int = Field(ge=MIN_INSTANCES, le=MAX_INSTANCES)
    timeout_seconds: int = Field(gt=0, le=3600)
    selected_test_ids: tuple[str, ...] = Field(min_length=1)
    failure_simulation: FailureSimulation = FailureSimulation.NONE

    @field_validator("selected_test_ids")
    @classmethod
    def _no_duplicates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("selected_test_ids must not contain duplicates")
        return value

    @field_validator("deployment_version")
    @classmethod
    def _strip_version(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("deployment_version must not be blank")
        return stripped
