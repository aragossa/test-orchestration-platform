"""Turn ORM rows into API responses."""

from __future__ import annotations

from app.api.schemas import (
    EventOut,
    RunDetailOut,
    RunSummaryOut,
    RunTestOut,
    TestCounts,
    TransitionOut,
)
from app.domain.states import RunState, TestStatus, is_terminal
from app.persistence.models import Run


def run_label(run_id: int) -> str:
    return f"RUN-{run_id}"


def test_counts(run: Run) -> TestCounts:
    statuses = [t.status for t in run.tests]
    return TestCounts(
        total=len(statuses),
        passed=statuses.count(TestStatus.PASSED),
        failed=statuses.count(TestStatus.FAILED),
        timed_out=statuses.count(TestStatus.TIMED_OUT),
        skipped=statuses.count(TestStatus.SKIPPED),
        pending=statuses.count(TestStatus.PENDING) + statuses.count(TestStatus.RUNNING),
    )


def final_result(run: Run, counts: TestCounts) -> str | None:
    state = RunState(run.state)
    if state is RunState.COMPLETED:
        return f"{counts.passed}/{counts.total} tests passed"
    if state is RunState.FAILED:
        executed = counts.passed + counts.failed + counts.timed_out
        if executed == 0:
            return "Failed before tests ran"
        return f"{counts.passed}/{counts.total} tests passed"
    if state is RunState.CANCELLED:
        return "Cancelled"
    return None


def duration_seconds(run: Run) -> float | None:
    if run.started_at is None:
        return None
    end = run.finished_at
    if end is None:
        return None
    return round((end - run.started_at).total_seconds(), 1)


def to_summary(run: Run) -> RunSummaryOut:
    counts = test_counts(run)
    return RunSummaryOut(
        id=run.id,
        label=run_label(run.id),
        environment=run.environment_id,
        state=RunState(run.state),
        is_terminal=is_terminal(RunState(run.state)),
        current_step=run.current_step,
        final_result=final_result(run, counts),
        error_message=run.error_message,
        failure_simulation=run.failure_simulation,  # type: ignore[arg-type]
        selected_test_ids=[t.test_id for t in run.tests],
        test_counts=counts,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=duration_seconds(run),
    )


def to_detail(run: Run) -> RunDetailOut:
    summary = to_summary(run)
    return RunDetailOut(
        **summary.model_dump(),
        config=run.config_snapshot,
        deployment_id=run.deployment_id,
        worker_id=run.worker_id,
        cancel_requested_at=run.cancel_requested_at,
        tests=[
            RunTestOut(
                test_id=t.test_id,
                name=t.definition.name if t.definition else t.test_id,
                category=t.definition.category if t.definition else "",
                position=t.position,
                status=TestStatus(t.status),
                duration_ms=t.duration_ms,
                message=t.message,
                error_message=t.error_message,
                started_at=t.started_at,
                finished_at=t.finished_at,
            )
            for t in run.tests
        ],
        transitions=[TransitionOut.model_validate(t) for t in run.transitions],
        events=[EventOut.model_validate(e) for e in run.events],
    )
