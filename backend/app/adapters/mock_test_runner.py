"""Mock test runner.

Each catalog test "runs" for its base duration and passes with a canned
message. Failure simulation injects one failing or one hanging test; the
engine, not the mock, decides that a hanging test has timed out.
"""

from __future__ import annotations

import asyncio
import random
import time

from app.adapters.base import ExecutionContext, TestResult
from app.domain.states import FailureSimulation, TestStatus

SUCCESS_MESSAGES: dict[str, str] = {
    "connectivity_check": "All required endpoints responded successfully.",
    "data_ingestion": "Sample data was accepted and processed.",
    "processing_validation": "Processed output matched expected results.",
    "service_restart_recovery": "Service recovered and resumed processing after restart.",
    "high_load_processing": "Throughput stayed within limits under increased load.",
}

FAILURE_MESSAGES: dict[str, str] = {
    "connectivity_check": "Endpoint /health returned 503 Service Unavailable.",
    "data_ingestion": "Expected 1000 records, received 942.",
    "processing_validation": "Checksum mismatch in processed batch #3.",
    "service_restart_recovery": "Service did not resume consuming within 30s after restart.",
    "high_load_processing": "P99 latency 2.4s exceeded the 1.0s budget.",
}


class MockTestExecutionAdapter:
    def __init__(
        self,
        *,
        delay_scale: float = 1.0,
        random_failure_rate: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        self.delay_scale = delay_scale
        self.random_failure_rate = random_failure_rate
        self._rng = rng or random.Random()

    def _duration_ms(self, test_id: str, context: ExecutionContext) -> int:
        spec = context.tests.get(test_id)
        base = spec.base_duration_ms if spec else 2000
        return int(base * self.delay_scale)

    async def execute_test(self, test_id: str, context: ExecutionContext) -> TestResult:
        simulation = context.config.failure_simulation
        is_target = context.failure_target_test_id == test_id
        started = time.monotonic()

        if simulation is FailureSimulation.TEST_TIMEOUT and is_target:
            # Hang well past the run's timeout. The engine cancels this
            # coroutine once the deadline passes and records TIMED_OUT.
            await asyncio.sleep(context.timeout_seconds + 60)

        duration_ms = self._duration_ms(test_id, context)
        await asyncio.sleep(duration_ms / 1000)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        if simulation is FailureSimulation.TEST_FAILURE and is_target:
            return TestResult(
                test_id=test_id,
                status=TestStatus.FAILED,
                duration_ms=elapsed_ms,
                error_message="Simulated test failure: "
                + FAILURE_MESSAGES.get(test_id, "assertion failed"),
            )

        if self.random_failure_rate > 0 and self._rng.random() < self.random_failure_rate:
            return TestResult(
                test_id=test_id,
                status=TestStatus.FAILED,
                duration_ms=elapsed_ms,
                error_message=FAILURE_MESSAGES.get(test_id, "assertion failed"),
            )

        return TestResult(
            test_id=test_id,
            status=TestStatus.PASSED,
            duration_ms=elapsed_ms,
            message=SUCCESS_MESSAGES.get(test_id, "Test passed."),
        )
