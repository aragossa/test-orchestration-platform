"""Adapter interfaces between the orchestration engine and external systems.

The engine only ever talks to these protocols. The mock implementations in
this package stand in for real infrastructure (Kubernetes, Terraform, a cloud
API) and a real test runner (pytest, k6, Playwright). Swapping one in is a
matter of implementing the protocol and wiring it in `app.main`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.domain.config import RunConfig
from app.domain.states import TestStatus


class InfrastructureError(Exception):
    """Base class for failures reported by the infrastructure adapter."""


class EnvironmentPreparationError(InfrastructureError):
    pass


class DeploymentFailedError(InfrastructureError):
    pass


class ReadinessTimeoutError(InfrastructureError):
    pass


@dataclass(frozen=True)
class DeploymentResult:
    deployment_id: str
    version: str
    details: str | None = None


@dataclass(frozen=True)
class TestSpec:
    """What the engine knows about a catalog test when it hands it to a runner."""

    test_id: str
    name: str
    category: str
    base_duration_ms: int


@dataclass(frozen=True)
class ExecutionContext:
    run_id: int
    environment: str
    deployment_id: str
    config: RunConfig
    timeout_seconds: int
    tests: dict[str, TestSpec] = field(default_factory=dict)
    # The test on which a simulated test failure/timeout is injected.
    failure_target_test_id: str | None = None


@dataclass(frozen=True)
class TestResult:
    test_id: str
    status: TestStatus
    duration_ms: int
    message: str | None = None
    error_message: str | None = None


class InfrastructureAdapter(Protocol):
    async def prepare_environment(self, environment: str, config: RunConfig) -> None:
        """Make the environment ready to receive a deployment."""

    async def deploy(self, environment: str, config: RunConfig) -> DeploymentResult:
        """Deploy the requested configuration. Raises DeploymentFailedError."""

    async def wait_until_ready(self, deployment_id: str, timeout_ms: int) -> None:
        """Block until the deployment reports ready. Raises ReadinessTimeoutError."""


class TestExecutionAdapter(Protocol):
    async def execute_test(self, test_id: str, context: ExecutionContext) -> TestResult:
        """Run one test to completion and report its outcome.

        Implementations may take arbitrarily long; the engine enforces the
        run's timeout and records TIMED_OUT itself.
        """
