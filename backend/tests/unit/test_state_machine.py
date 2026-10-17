import pytest

from app.domain.states import (
    ACTIVE_STATES,
    CANCELLABLE_STATES,
    HAPPY_PATH,
    TERMINAL_STATES,
    TRANSITIONS,
    InvalidTransitionError,
    RunState,
    assert_transition,
    can_transition,
    is_terminal,
    next_happy_state,
)


def test_every_state_has_a_transition_entry() -> None:
    assert set(TRANSITIONS) == set(RunState)


def test_happy_path_is_fully_allowed() -> None:
    for current, following in zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False):
        assert can_transition(current, following), f"{current} -> {following}"
        assert next_happy_state(current) is following


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()
        assert is_terminal(state)
        with pytest.raises(InvalidTransitionError):
            next_happy_state(state)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunState.PENDING, RunState.RUNNING_TESTS),
        (RunState.PENDING, RunState.COMPLETED),
        (RunState.PREPARING, RunState.COMPLETED),
        (RunState.DEPLOYING, RunState.PREPARING),
        (RunState.COMPLETED, RunState.PENDING),
        (RunState.FAILED, RunState.COMPLETED),
        (RunState.CANCELLED, RunState.PREPARING),
        (RunState.COLLECTING_RESULTS, RunState.CANCELLED),
    ],
)
def test_illegal_transitions_are_rejected(current: RunState, target: RunState) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidTransitionError) as exc_info:
        assert_transition(current, target)
    assert exc_info.value.current is current
    assert exc_info.value.target is target


def test_every_active_state_can_fail() -> None:
    for state in ACTIVE_STATES:
        assert can_transition(state, RunState.FAILED), state


def test_pending_cannot_fail_directly() -> None:
    # A run that never started has nothing to fail on; it can only be cancelled.
    assert not can_transition(RunState.PENDING, RunState.FAILED)
    assert can_transition(RunState.PENDING, RunState.CANCELLED)


def test_cancellable_states_can_reach_cancelled() -> None:
    for state in CANCELLABLE_STATES:
        assert can_transition(state, RunState.CANCELLED), state
    for state in set(RunState) - CANCELLABLE_STATES:
        assert not can_transition(state, RunState.CANCELLED), state


def test_states_are_plain_strings_for_persistence() -> None:
    assert RunState.RUNNING_TESTS == "RUNNING_TESTS"
    assert RunState("FAILED") is RunState.FAILED
