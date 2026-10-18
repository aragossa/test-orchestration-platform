from app.adapters.base import (
    DeploymentFailedError,
    DeploymentResult,
    EnvironmentPreparationError,
    ExecutionContext,
    InfrastructureAdapter,
    InfrastructureError,
    ReadinessTimeoutError,
    TestExecutionAdapter,
    TestResult,
    TestSpec,
)
from app.adapters.mock_infrastructure import MockInfrastructureAdapter
from app.adapters.mock_test_runner import MockTestExecutionAdapter
from app.config import Settings


def build_adapters(
    settings: Settings,
) -> tuple[MockInfrastructureAdapter, MockTestExecutionAdapter]:
    """The one place that chooses concrete adapters. A real infrastructure or
    test-runner adapter only needs to be swapped in here."""
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


__all__ = [
    "DeploymentFailedError",
    "DeploymentResult",
    "EnvironmentPreparationError",
    "ExecutionContext",
    "InfrastructureAdapter",
    "InfrastructureError",
    "MockInfrastructureAdapter",
    "MockTestExecutionAdapter",
    "ReadinessTimeoutError",
    "TestExecutionAdapter",
    "TestResult",
    "TestSpec",
    "build_adapters",
]
