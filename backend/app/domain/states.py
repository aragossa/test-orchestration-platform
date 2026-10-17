"""Run lifecycle state machine.

This module is pure Python: it knows nothing about the database or the API.
It defines the run states, the set of legal transitions and the single
function every other layer must go through to change a run's state.
"""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    PENDING = "PENDING"
    PREPARING = "PREPARING"
    DEPLOYING = "DEPLOYING"
    WAITING_FOR_READY = "WAITING_FOR_READY"
    RUNNING_TESTS = "RUNNING_TESTS"
    COLLECTING_RESULTS = "COLLECTING_RESULTS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TestStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    SKIPPED = "SKIPPED"


class FailureSimulation(StrEnum):
    NONE = "none"
    DEPLOYMENT_FAILURE = "deployment_failure"
    TEST_FAILURE = "test_failure"
    TEST_TIMEOUT = "test_timeout"


# The happy path, in order. Each step of the orchestration workflow maps to
# exactly one of these states, which is what "each step should be represented
# explicitly in the backend's execution model" asks for.
HAPPY_PATH: tuple[RunState, ...] = (
    RunState.PENDING,
    RunState.PREPARING,
    RunState.DEPLOYING,
    RunState.WAITING_FOR_READY,
    RunState.RUNNING_TESTS,
    RunState.COLLECTING_RESULTS,
    RunState.COMPLETED,
)

TERMINAL_STATES: frozenset[RunState] = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
)

# States in which a run occupies its target environment. Used for the
# "one deployment per environment at a time" guarantee.
ACTIVE_STATES: frozenset[RunState] = frozenset(
    {
        RunState.PREPARING,
        RunState.DEPLOYING,
        RunState.WAITING_FOR_READY,
        RunState.RUNNING_TESTS,
        RunState.COLLECTING_RESULTS,
    }
)

# States from which a user may request cancellation. Once results are being
# collected the run is about to finish, so cancelling it adds nothing.
CANCELLABLE_STATES: frozenset[RunState] = frozenset(
    {
        RunState.PENDING,
        RunState.PREPARING,
        RunState.DEPLOYING,
        RunState.WAITING_FOR_READY,
        RunState.RUNNING_TESTS,
    }
)

TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.PENDING: frozenset({RunState.PREPARING, RunState.CANCELLED}),
    RunState.PREPARING: frozenset({RunState.DEPLOYING, RunState.FAILED, RunState.CANCELLED}),
    RunState.DEPLOYING: frozenset(
        {RunState.WAITING_FOR_READY, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.WAITING_FOR_READY: frozenset(
        {RunState.RUNNING_TESTS, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.RUNNING_TESTS: frozenset(
        {RunState.COLLECTING_RESULTS, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.COLLECTING_RESULTS: frozenset({RunState.COMPLETED, RunState.FAILED}),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


class InvalidTransitionError(Exception):
    def __init__(self, current: RunState, target: RunState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Cannot transition run from {current} to {target}")


def can_transition(current: RunState, target: RunState) -> bool:
    return target in TRANSITIONS[current]


def assert_transition(current: RunState, target: RunState) -> None:
    """Raise InvalidTransitionError unless current -> target is allowed."""
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES


def next_happy_state(current: RunState) -> RunState:
    """Return the next state on the happy path.

    Raises InvalidTransitionError for terminal states or for states that are
    not on the happy path.
    """
    try:
        index = HAPPY_PATH.index(current)
    except ValueError as exc:
        raise InvalidTransitionError(current, current) from exc
    if index == len(HAPPY_PATH) - 1:
        raise InvalidTransitionError(current, current)
    return HAPPY_PATH[index + 1]
