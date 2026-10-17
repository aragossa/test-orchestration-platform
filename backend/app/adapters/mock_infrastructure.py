"""Mock infrastructure provider.

Pretends to prepare an environment and roll out a deployment. Delays are
configurable so progress is visible in the UI; with zero delays the whole
workflow completes instantly, which is what the test suite uses.
"""

from __future__ import annotations

import asyncio
import random
import uuid

from app.adapters.base import (
    DeploymentFailedError,
    DeploymentResult,
    EnvironmentPreparationError,
    ReadinessTimeoutError,
)
from app.domain.config import RunConfig
from app.domain.states import FailureSimulation


class MockInfrastructureAdapter:
    def __init__(
        self,
        *,
        prepare_delay_ms: int = 2000,
        deploy_delay_ms: int = 3000,
        ready_delay_ms: int = 2000,
        random_failure_rate: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        self.prepare_delay_ms = prepare_delay_ms
        self.deploy_delay_ms = deploy_delay_ms
        self.ready_delay_ms = ready_delay_ms
        self.random_failure_rate = random_failure_rate
        self._rng = rng or random.Random()

    def _chaos(self) -> bool:
        return self.random_failure_rate > 0 and self._rng.random() < self.random_failure_rate

    async def prepare_environment(self, environment: str, config: RunConfig) -> None:
        await asyncio.sleep(self.prepare_delay_ms / 1000)
        if self._chaos():
            raise EnvironmentPreparationError(
                f"Environment '{environment}' preparation failed: namespace quota exceeded"
            )

    async def deploy(self, environment: str, config: RunConfig) -> DeploymentResult:
        await asyncio.sleep(self.deploy_delay_ms / 1000)
        if config.failure_simulation is FailureSimulation.DEPLOYMENT_FAILURE:
            raise DeploymentFailedError(
                f"Simulated deployment failure: image '{config.deployment_version}' "
                f"failed health checks on {environment} "
                f"({config.worker_instances} workers, {config.data_generators} generators)"
            )
        if self._chaos():
            raise DeploymentFailedError(
                f"Deployment of '{config.deployment_version}' to {environment} failed: "
                "rollout exceeded progress deadline"
            )
        return DeploymentResult(
            deployment_id=f"dep-{uuid.uuid4().hex[:10]}",
            version=config.deployment_version,
            details=(
                f"{config.worker_instances} worker instance(s), "
                f"{config.data_generators} data generator(s)"
            ),
        )

    async def wait_until_ready(self, deployment_id: str, timeout_ms: int) -> None:
        if self.ready_delay_ms > timeout_ms:
            await asyncio.sleep(timeout_ms / 1000)
            raise ReadinessTimeoutError(
                f"Deployment {deployment_id} did not become ready within {timeout_ms} ms"
            )
        await asyncio.sleep(self.ready_delay_ms / 1000)
