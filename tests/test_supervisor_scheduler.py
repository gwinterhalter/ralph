"""Component tests for the OLB-09 Cross-Project Scheduler (``supervisor/scheduler.py``).

Covers the OLB-09 predicate (Spec v1.3 §7) — one ``@pytest.mark.unit`` case per
FR-022..FR-027 plus an empty-runnable-set edge — entirely DB-free over in-memory
:class:`ProjectRecord` fixtures (gate ``olb09-scheduler-build-substrate`` = A). The
scheduler is a pure decision layer, so every case is a direct call with no port, no
database, and no file I/O.
"""
from __future__ import annotations

import pytest

from supervisor.scheduler import (
    DISPATCH_RESUME,
    DISPATCH_SPAWN,
    REASON_CLOSEST_TO_DONE,
    REASON_PRIORITY,
    REASON_STARVATION,
    STARVATION_ROUND_THRESHOLD,
    ProjectRecord,
    derive_runnable,
    select_next_dispatch,
)

# The five non-runnable lifecycle states FR-022 must exclude (the OLB-02
# ``transitions`` set minus the two runnable states).
NON_RUNNABLE_STATES = (
    "paused_gate",
    "paused_budget",
    "paused_safety",
    "complete",
    "failed",
)


def _project(
    project_id: str,
    lifecycle_state: str,
    *,
    priority: int = 1,
    open_work_count: int = 0,
) -> ProjectRecord:
    """Build a :class:`ProjectRecord` with defaults for the field not under test."""
    return ProjectRecord(
        project_id=project_id,
        lifecycle_state=lifecycle_state,
        priority=priority,
        open_work_count=open_work_count,
    )


@pytest.mark.unit
def test_fr022_runnable_set_excludes_non_runnable() -> None:
    """FR-022: only ``admitted`` (with headroom) / ``running`` Projects are runnable;
    every paused/terminal state is excluded."""
    blocked = [_project(f"p_{state}", state) for state in NON_RUNNABLE_STATES]
    projects = [*blocked, _project("p_admitted", "admitted"), _project("p_running", "running")]

    runnable = derive_runnable(projects, in_flight_project_ids=(), ceiling_headroom=2)
    assert {p.project_id for p in runnable} == {"p_admitted", "p_running"}

    # admitted needs ceiling headroom to spawn; running resumes without a new slot.
    no_headroom = derive_runnable(projects, in_flight_project_ids=(), ceiling_headroom=0)
    assert {p.project_id for p in no_headroom} == {"p_running"}


@pytest.mark.unit
def test_fr023_priority_weighted_selection() -> None:
    """FR-023: the higher-priority runnable Project is selected absent a starvation override."""
    projects = [
        _project("low", "admitted", priority=1),
        _project("high", "admitted", priority=9),
    ]

    decision = select_next_dispatch(
        projects, in_flight_project_ids=(), round_state={}, ceiling_headroom=2
    )

    assert decision is not None
    assert decision.project_id == "high"
    assert decision.reason == REASON_PRIORITY
    assert decision.dispatch_kind == DISPATCH_SPAWN  # admitted -> spawn


@pytest.mark.unit
def test_fr024_closest_to_done_bias() -> None:
    """FR-024: equal priority breaks toward the fewest open work-registry items."""
    projects = [
        _project("far", "running", priority=5, open_work_count=9),
        _project("near", "running", priority=5, open_work_count=2),
    ]

    decision = select_next_dispatch(
        projects, in_flight_project_ids=(), round_state={}, ceiling_headroom=2
    )

    assert decision is not None
    assert decision.project_id == "near"
    assert decision.reason == REASON_CLOSEST_TO_DONE
    assert decision.dispatch_kind == DISPATCH_RESUME  # running -> resume


@pytest.mark.unit
def test_fr025_starvation_guard_promotes() -> None:
    """FR-025: a runnable Project skipped >= threshold rounds is promoted ahead of a
    higher-priority peer for one Dispatch, then its skip-count resets to 0."""
    projects = [
        _project("hungry", "running", priority=1),
        _project("fat", "running", priority=9),
    ]
    round_state = {"hungry": STARVATION_ROUND_THRESHOLD, "fat": 0}

    decision = select_next_dispatch(
        projects, in_flight_project_ids=(), round_state=round_state, ceiling_headroom=2
    )

    assert decision is not None
    assert decision.project_id == "hungry"  # promoted over higher-priority "fat"
    assert decision.reason == REASON_STARVATION
    assert decision.round_state["hungry"] == 0  # reset after selection
    assert decision.round_state["fat"] == 1  # skipped this round
    assert round_state["hungry"] == STARVATION_ROUND_THRESHOLD  # input not mutated


@pytest.mark.unit
def test_fr026_gate_blocked_skip_without_demotion() -> None:
    """FR-026: a Project blocked in ``paused_gate`` is skipped without its priority or
    skip-count being altered, so it resumes its normal position when runnable again."""
    higher = _project("higher", "running", priority=9)
    blocked = _project("blocker", "paused_gate", priority=5)
    # "blocker" earned a skip-count of 3 while runnable; then its gate tripped.
    round_state = {"higher": 0, "blocker": 3}

    decision = select_next_dispatch(
        [higher, blocked],
        in_flight_project_ids=(),
        round_state=round_state,
        ceiling_headroom=2,
    )

    assert decision is not None
    assert decision.project_id == "higher"  # blocked Project is not considered
    # The gate-blocked round did NOT increment the blocked Project's skip-count, so the
    # block never demotes it:
    assert decision.round_state["blocker"] == 3
    # priority is a read-only input the scheduler never mutates:
    assert blocked.priority == 5

    # When the gate resolves the same Project re-appears at its unchanged priority and
    # preserved skip-count, resuming its normal scheduling position.
    runnable_again = _project("blocker", "running", priority=5)
    resumed = derive_runnable(
        [higher, runnable_again], in_flight_project_ids=(), ceiling_headroom=2
    )
    assert "blocker" in {p.project_id for p in resumed}


@pytest.mark.unit
def test_fr027_dispatch_idempotency() -> None:
    """FR-027: a Project with an in-flight Dispatch is not selected again until its
    Dispatch is reconciled, preserving the at-most-one-active-Run invariant."""
    projects = [
        _project("in_flight", "running", priority=9),  # higher priority, but in flight
        _project("free", "running", priority=1),
    ]

    decision = select_next_dispatch(
        projects,
        in_flight_project_ids={"in_flight"},
        round_state={},
        ceiling_headroom=2,
    )

    assert decision is not None
    assert decision.project_id == "free"  # the in-flight higher-priority one is skipped
    assert decision.dispatch_kind == DISPATCH_RESUME


@pytest.mark.unit
def test_empty_runnable_set_returns_none() -> None:
    """Edge: an empty fleet, or one where every Project is blocked, yields no Dispatch
    and no exception."""
    assert (
        select_next_dispatch([], in_flight_project_ids=(), round_state={}, ceiling_headroom=2)
        is None
    )

    all_blocked = [_project(f"p_{state}", state) for state in NON_RUNNABLE_STATES]
    assert (
        select_next_dispatch(
            all_blocked, in_flight_project_ids=(), round_state={}, ceiling_headroom=2
        )
        is None
    )
