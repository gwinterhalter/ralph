"""Dedicated unit tests for supervisor.transitions — the §5.3 FR-008 lifecycle-state
transition legality (previously exercised only indirectly via registry.set_lifecycle_state).

Covers: every legal edge is accepted; the load-bearing illegal edges are rejected; unknown
states have no targets; assert_ raises IllegalTransitionError.
"""
from __future__ import annotations

import pytest

from supervisor.transitions import (
    LEGAL_TRANSITIONS,
    LIFECYCLE_STATES,
    IllegalTransitionError,
    assert_legal_transition,
    is_legal_transition,
    legal_targets,
)


def test_nine_lifecycle_states_including_pending_approval() -> None:
    assert len(LIFECYCLE_STATES) == 9
    assert "pending_approval" in LIFECYCLE_STATES
    for terminal in ("complete", "failed", "candidate", "admitted", "running"):
        assert terminal in LIFECYCLE_STATES


def test_every_legal_edge_is_accepted() -> None:
    for src, dsts in LEGAL_TRANSITIONS.items():
        for dst in dsts:
            assert is_legal_transition(src, dst), f"{src}->{dst} should be legal"
            assert_legal_transition(src, dst)  # must not raise


@pytest.mark.parametrize(
    "src,dst",
    [
        ("pending_approval", "admitted"),  # only pending_approval -> candidate is legal
        ("pending_approval", "running"),
        ("candidate", "running"),          # must go candidate -> admitted -> running
        ("admitted", "candidate"),
        ("running", "candidate"),          # a running project must re-admit, not jump back
        ("complete", "running"),           # FR-008: re-admission required (complete -> candidate only)
        ("failed", "running"),
        ("admitted", "admitted"),          # self-transition is not a legal edge
        ("paused_gate", "complete"),       # paused_* resumes only to running
        ("paused_budget", "failed"),
        ("complete", "failed"),
    ],
)
def test_illegal_edges_are_rejected(src: str, dst: str) -> None:
    assert not is_legal_transition(src, dst)
    with pytest.raises(IllegalTransitionError):
        assert_legal_transition(src, dst)


def test_pending_approval_only_reaches_candidate() -> None:
    assert legal_targets("pending_approval") == frozenset({"candidate"})


def test_complete_and_failed_reopen_only_to_candidate() -> None:
    assert legal_targets("complete") == frozenset({"candidate"})
    assert legal_targets("failed") == frozenset({"candidate"})


def test_running_reaches_the_three_pauses_and_two_terminals() -> None:
    assert legal_targets("running") == frozenset(
        {"paused_gate", "paused_budget", "paused_safety", "complete", "failed"}
    )


def test_paused_states_resume_to_running() -> None:
    for paused in ("paused_gate", "paused_budget", "paused_safety"):
        assert legal_targets(paused) == frozenset({"running"})


def test_unknown_state_has_no_targets_and_never_transitions() -> None:
    assert legal_targets("bogus_state") == frozenset()
    assert not is_legal_transition("bogus_state", "candidate")
    with pytest.raises(IllegalTransitionError):
        assert_legal_transition("bogus_state", "candidate")


def test_illegal_transition_error_message_cites_legal_targets() -> None:
    with pytest.raises(IllegalTransitionError) as exc:
        assert_legal_transition("running", "candidate")
    msg = str(exc.value)
    assert "running" in msg and "candidate" in msg
