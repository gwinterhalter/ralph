"""Component tests for the OLB-10 Operator-Attention Scheduler (``supervisor/attention.py``).

Covers the OLB-10 predicate (Spec v1.3 §8) — one ``@pytest.mark.unit`` case per
FR-028..FR-033 plus resolve/empty edges — entirely DB-free over in-memory
:class:`Escalation` / :class:`AttentionState` fixtures with a supplied ``now`` clock and
Quiet-Hours / batch-window / threshold config (gate
``olb10-attention-scheduler-build-substrate`` = A). The attention scheduler is a pure
decision/policy layer, so every case is a direct call with no port, no database, no real
notification send, and no file I/O.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from supervisor.attention import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    ESCALATION_KIND_KILL_SWITCH,
    ESCALATION_KIND_ROUTINE,
    ESCALATION_KIND_SAFETY_GATE,
    AttentionState,
    Escalation,
    OneConfirmOffer,
    QuietHours,
    UrgencyTier,
    assign_urgency_tier,
    auto_pick_eligible,
    build_one_confirm_offer,
    build_state,
    intake_escalation,
    plan_notifications,
    resolve_escalation,
)

# A fixed batch window + reference clock — the module reads no wall-clock, so every
# time value the policy keys on is supplied here.
BATCH_WINDOW = timedelta(minutes=15)
# 14:00 — outside the QUIET_HOURS window below (an active window).
ACTIVE_NOW = datetime(2026, 6, 5, 14, 0, 0)
# 23:00 — inside the QUIET_HOURS window below (a suppressed window).
QUIET_NOW = datetime(2026, 6, 5, 23, 0, 0)
# 22:00 -> 07:00, wrapping past midnight.
QUIET_HOURS = QuietHours(start=time(22, 0), end=time(7, 0))


def _escalation(
    project_id: str,
    gate_id: str,
    *,
    kind: str = ESCALATION_KIND_ROUTINE,
    reversible: bool = True,
    suggested_option: str | None = None,
    confidence: float = 0.0,
    raised_at: datetime = ACTIVE_NOW,
) -> Escalation:
    """Build an :class:`Escalation` with defaults for the fields not under test."""
    return Escalation(
        project_id=project_id,
        gate_id=gate_id,
        kind=kind,
        reversible=reversible,
        suggested_option=suggested_option,
        confidence=confidence,
        raised_at=raised_at,
    )


@pytest.mark.unit
def test_fr028_intake_increments_debt_and_queues() -> None:
    """FR-028: a ``running`` Project raising a ``gate_human`` escalation has its
    Attention Debt incremented by 1 and the escalation queued for operator attention."""
    state = AttentionState.empty()
    escalation = _escalation("proj-a", "gate-1")

    updated = intake_escalation(state, escalation)

    assert updated.debt_for("proj-a") == 1
    assert escalation in updated.queue
    # Pure — the supplied state is not mutated.
    assert state.debt_for("proj-a") == 0
    assert state.queue == ()

    # FR-004: a second unresolved escalation reads debt 2 (the §5.2 acceptance criterion).
    twice = intake_escalation(updated, _escalation("proj-a", "gate-2"))
    assert twice.debt_for("proj-a") == 2
    assert len(twice.queue) == 2


@pytest.mark.unit
def test_fr029_top_tier_surfaced_first_and_bypasses_quiet_hours() -> None:
    """FR-029: a safety-gate escalation queued with a routine one is tiered top, ordered
    first, and surfaced even during Quiet Hours (the top tier is never deferred)."""
    safety = _escalation("proj-a", "gate-safety", kind=ESCALATION_KIND_SAFETY_GATE)
    routine = _escalation("proj-b", "gate-routine", kind=ESCALATION_KIND_ROUTINE)

    assert assign_urgency_tier(safety) is UrgencyTier.TOP
    assert assign_urgency_tier(routine) is UrgencyTier.ROUTINE
    # The kill-switch kind is also top tier.
    assert assign_urgency_tier(_escalation("p", "g", kind=ESCALATION_KIND_KILL_SWITCH)) is (
        UrgencyTier.TOP
    )

    state = build_state(queue=[routine, safety])
    plan = plan_notifications(
        state, now=QUIET_NOW, quiet_hours=QUIET_HOURS, batch_window=BATCH_WINDOW
    )

    # Top-tier batch is first and carries the safety escalation; it is delivered even
    # though we are inside Quiet Hours, and the routine escalation is deferred (not first).
    assert plan.batches[0].tier is UrgencyTier.TOP
    assert plan.batches[0].escalations == (safety,)
    assert routine in plan.deferred
    assert all(safety not in batch.escalations for batch in plan.batches[1:])


@pytest.mark.unit
def test_fr030_batching_collapses_routine_escalations() -> None:
    """FR-030: three routine escalations due in one window collapse into ONE batched
    notification, not three."""
    routine = [
        _escalation("proj-a", "gate-1"),
        _escalation("proj-b", "gate-2"),
        _escalation("proj-c", "gate-3"),
    ]
    state = build_state(queue=routine)

    plan = plan_notifications(
        state, now=ACTIVE_NOW, quiet_hours=None, batch_window=BATCH_WINDOW
    )

    routine_batches = [b for b in plan.batches if b.tier is UrgencyTier.ROUTINE]
    assert len(routine_batches) == 1
    assert len(routine_batches[0].escalations) == 3
    assert plan.deferred == ()
    # The single batch carries the configured window bounds.
    assert routine_batches[0].window_end == ACTIVE_NOW
    assert routine_batches[0].window_start == ACTIVE_NOW - BATCH_WINDOW


@pytest.mark.unit
def test_fr031_quiet_hours_defers_without_loss() -> None:
    """FR-031: a routine escalation raised during Quiet Hours is NOT delivered then, and
    IS delivered in the next active-window batch (still queued, never dropped)."""
    routine = _escalation("proj-a", "gate-1")
    state = build_state(queue=[routine])

    # During Quiet Hours: suppressed, deferred, no routine batch delivered.
    quiet_plan = plan_notifications(
        state, now=QUIET_NOW, quiet_hours=QUIET_HOURS, batch_window=BATCH_WINDOW
    )
    assert quiet_plan.batches == ()
    assert quiet_plan.deferred == (routine,)

    # Next active window, SAME state (escalation never left the queue): delivered.
    active_plan = plan_notifications(
        state, now=ACTIVE_NOW, quiet_hours=QUIET_HOURS, batch_window=BATCH_WINDOW
    )
    assert active_plan.deferred == ()
    routine_batches = [b for b in active_plan.batches if b.tier is UrgencyTier.ROUTINE]
    assert len(routine_batches) == 1
    assert routine in routine_batches[0].escalations


@pytest.mark.unit
def test_fr032_auto_pick_eligibility() -> None:
    """FR-032: eligible iff a suggested option is present AND confidence >= threshold AND
    reversible; an eligible escalation yields a well-formed one-confirm offer."""
    threshold = DEFAULT_CONFIDENCE_THRESHOLD  # 0.7

    eligible = _escalation(
        "proj-a", "gate-1", reversible=True, suggested_option="B", confidence=0.92
    )
    below_threshold = _escalation(
        "proj-a", "gate-2", reversible=True, suggested_option="B", confidence=0.5
    )
    irreversible = _escalation(
        "proj-a", "gate-3", reversible=False, suggested_option="B", confidence=0.92
    )
    no_suggestion = _escalation(
        "proj-a", "gate-4", reversible=True, suggested_option=None, confidence=0.92
    )

    assert auto_pick_eligible(eligible, confidence_threshold=threshold) is True
    assert auto_pick_eligible(below_threshold, confidence_threshold=threshold) is False
    assert auto_pick_eligible(irreversible, confidence_threshold=threshold) is False
    assert auto_pick_eligible(no_suggestion, confidence_threshold=threshold) is False
    # Boundary: confidence exactly at threshold is eligible (>= not >).
    at_threshold = _escalation(
        "proj-a", "gate-5", reversible=True, suggested_option="B", confidence=threshold
    )
    assert auto_pick_eligible(at_threshold, confidence_threshold=threshold) is True

    offer = build_one_confirm_offer(eligible)
    assert offer == OneConfirmOffer(
        gate_id="gate-1", project_id="proj-a", suggested_option="B"
    )
    # Building an offer for an escalation with no suggested option is rejected.
    with pytest.raises(ValueError):
        build_one_confirm_offer(no_suggestion)


@pytest.mark.unit
def test_fr033_resolve_decrements_and_returns_runnable() -> None:
    """FR-033: resolving a Project's only escalation drops its Debt to 0 and signals
    return-to-runnable; with another block present the Debt decrements but the Project
    does not return to runnable."""
    escalation = _escalation("proj-a", "gate-1")
    state = intake_escalation(AttentionState.empty(), escalation)
    assert state.debt_for("proj-a") == 1

    # Single escalation resolves, no other block -> debt 0 and returns_runnable True.
    new_state, returns_runnable = resolve_escalation(
        state, project_id="proj-a", gate_id="gate-1", project_otherwise_blocked=False
    )
    assert new_state.debt_for("proj-a") == 0
    assert returns_runnable is True
    assert new_state.queue == ()
    # Pure — the supplied state is untouched.
    assert state.debt_for("proj-a") == 1

    # Another block present -> debt still decrements but the Project is NOT runnable.
    blocked_state, blocked_runnable = resolve_escalation(
        state, project_id="proj-a", gate_id="gate-1", project_otherwise_blocked=True
    )
    assert blocked_state.debt_for("proj-a") == 0
    assert blocked_runnable is False


@pytest.mark.unit
def test_resolve_unknown_escalation_is_noop_or_floored() -> None:
    """Edge: resolving an escalation that is not queued (or when debt is already 0) floors
    at 0 — no negative debt, no crash, and no spurious return-to-runnable signal."""
    state = build_state(attention_debt={"proj-a": 0}, queue=[])

    new_state, returns_runnable = resolve_escalation(
        state, project_id="proj-a", gate_id="does-not-exist", project_otherwise_blocked=False
    )

    assert new_state.debt_for("proj-a") == 0
    assert returns_runnable is False
    assert new_state.queue == ()


@pytest.mark.unit
def test_empty_state_plan_notifications_returns_empty_plan() -> None:
    """Edge: planning over an empty queue yields empty batches + empty deferred, no
    exception — whether or not we are inside Quiet Hours."""
    state = AttentionState.empty()

    active = plan_notifications(
        state, now=ACTIVE_NOW, quiet_hours=QUIET_HOURS, batch_window=BATCH_WINDOW
    )
    assert active.batches == ()
    assert active.deferred == ()

    quiet = plan_notifications(
        state, now=QUIET_NOW, quiet_hours=QUIET_HOURS, batch_window=BATCH_WINDOW
    )
    assert quiet.batches == ()
    assert quiet.deferred == ()
