"""Component tests for the OLB-13 Repair-Auto-OK Policy (``supervisor/repair_policy.py``).

Covers the OLB-13 repair-policy predicate (Spec v1.3 §11) — one ``@pytest.mark.unit``
case per FR-044..FR-048 plus a payload-shape edge — entirely DB-free over in-memory
:class:`RepairAction` fixtures with an injected ``confidence_threshold`` (gate
``olb13-repair-teardown-build-substrate`` = A). The policy is a pure decision layer,
so every case is a direct call with no port, no database, no file I/O, no wall-clock
read, and no real escalation send: the FR-046 escalation payload is *built* (not
routed) and asserted against the OLB-10 :class:`supervisor.attention.Escalation` shape.

The fixtures are crafted so each FR rule is isolated: the FR-045 grant case flips each
of the three grant conditions in turn to prove every one is load-bearing; the FR-048
case holds all three grant conditions and toggles only the safety-gate predicate.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from supervisor.attention import ESCALATION_KIND_ROUTINE, Escalation
from supervisor.repair_policy import (
    AutoRepairAuditRecord,
    RepairAction,
    RepairKind,
    ReversibilityClass,
    build_audit_record,
    build_repair_escalation,
    classify_reversibility,
    evaluate_repair,
)

# A fixed threshold + clock — the policy reads neither a seed nor a wall-clock; both
# are supplied here (gate olb13-repair-teardown-build-substrate = A).
THRESHOLD = 0.7
RAISED_AT = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)

#: The four §11.3 v1.0 repair classes whose default Reversibility Class is reversible.
REVERSIBLE_KINDS = (
    RepairKind.REATTACH_STALLED_RUN,
    RepairKind.RECONCILE_EXITED_UNRECONCILED_RUN,
    RepairKind.RESUME_PAUSED_RUN,
    RepairKind.RESPAWN_FAILED_AT_SPAWN_RUN,
)


def _action(
    kind: RepairKind = RepairKind.REATTACH_STALLED_RUN,
    *,
    confidence: float = 0.9,
    project_id: str = "proj-a",
    triggering_anomaly: str = "spend_delta_anomaly",
) -> RepairAction:
    """A repair action with sensible reversible/high-confidence defaults; each test
    overrides only the field its FR rule keys on."""
    return RepairAction(
        kind=kind,
        project_id=project_id,
        triggering_anomaly=triggering_anomaly,
        confidence=confidence,
    )


@pytest.mark.unit
def test_fr044_reversibility_assigned_before_execution() -> None:
    """FR-044: each §11.3 v1.0 repair class classifies to its default Reversibility
    Class purely from its kind — the four restore-to-prior-state repairs are
    reversible and the discard/rewrite class is irreversible — independent of any
    grant/escalate decision."""
    for kind in REVERSIBLE_KINDS:
        assert classify_reversibility(_action(kind)) is ReversibilityClass.REVERSIBLE
    assert (
        classify_reversibility(_action(RepairKind.DISCARD_OR_REWRITE_COMMITTED))
        is ReversibilityClass.IRREVERSIBLE
    )


@pytest.mark.unit
def test_fr045_reversible_in_scope_high_confidence_grants() -> None:
    """FR-045: a reversible, in-scope, high-confidence repair with no safety refusal is
    granted autonomously (no escalation), and flipping ANY one of the three conditions
    flips the decision to escalate."""
    decision = evaluate_repair(
        _action(),
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=False,
    )
    assert decision.grant is True
    assert decision.escalate is False
    assert decision.reversibility is ReversibilityClass.REVERSIBLE
    assert bool(decision) is True

    # Flip reversibility -> escalate.
    irreversible = evaluate_repair(
        _action(RepairKind.DISCARD_OR_REWRITE_COMMITTED),
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=False,
    )
    assert irreversible.grant is False and irreversible.escalate is True

    # Flip in_scope -> escalate.
    out_of_scope = evaluate_repair(
        _action(),
        confidence_threshold=THRESHOLD,
        in_scope=False,
        safety_gate_refuses=False,
    )
    assert out_of_scope.grant is False and out_of_scope.escalate is True

    # Flip confidence below threshold -> escalate.
    low_conf = evaluate_repair(
        _action(confidence=0.5),
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=False,
    )
    assert low_conf.grant is False and low_conf.escalate is True


@pytest.mark.unit
def test_fr046_irreversible_or_out_of_scope_or_below_threshold_escalates() -> None:
    """FR-046: an irreversible repair escalates to gate_human even at confidence 1.0
    (never auto-executes); a below-threshold repair escalates; an out-of-scope repair
    escalates."""
    irreversible_full_conf = evaluate_repair(
        _action(RepairKind.DISCARD_OR_REWRITE_COMMITTED, confidence=1.0),
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=False,
    )
    assert irreversible_full_conf.grant is False
    assert irreversible_full_conf.escalate is True

    below_threshold = evaluate_repair(
        _action(confidence=0.69),
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=False,
    )
    assert below_threshold.grant is False and below_threshold.escalate is True

    out_of_scope = evaluate_repair(
        _action(),
        confidence_threshold=THRESHOLD,
        in_scope=False,
        safety_gate_refuses=False,
    )
    assert out_of_scope.grant is False and out_of_scope.escalate is True

    # The threshold is a >=, so confidence exactly at the threshold grants (boundary).
    at_threshold = evaluate_repair(
        _action(confidence=THRESHOLD),
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=False,
    )
    assert at_threshold.grant is True


@pytest.mark.unit
def test_fr047_audit_trail_complete() -> None:
    """FR-047: a granted reversible repair's audit record carries all four facts —
    the action, the Reversibility Class, the triggering anomaly, and the rationale —
    so an operator can audit the unattended action; building a record for a
    non-granted decision is refused."""
    action = _action(triggering_anomaly="target_loop")
    decision = evaluate_repair(
        action,
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=False,
    )
    record = build_audit_record(action, decision)
    assert isinstance(record, AutoRepairAuditRecord)
    assert record.action is action
    assert record.reversibility is ReversibilityClass.REVERSIBLE
    assert record.triggering_anomaly == "target_loop"
    assert record.rationale
    assert record.rationale == decision.rationale

    # An escalated (non-granted) decision has no auto-repair audit record.
    escalated = evaluate_repair(
        _action(RepairKind.DISCARD_OR_REWRITE_COMMITTED),
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=False,
    )
    with pytest.raises(ValueError):
        build_audit_record(action, escalated)


@pytest.mark.unit
def test_fr048_safety_gate_precedence_refuses_grantable_action() -> None:
    """FR-048 (§9.3): a reversible, in-scope, high-confidence repair the safety gate
    refuses is escalated rather than granted, even though it is otherwise grantable —
    the safety floor overrides the policy grant."""
    granted = evaluate_repair(
        _action(),
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=False,
    )
    refused = evaluate_repair(
        _action(),
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=True,
    )

    assert granted.grant is True
    assert refused.grant is False
    assert refused.escalate is True
    assert refused.reversibility is ReversibilityClass.REVERSIBLE
    assert "safety" in refused.rationale.lower()


@pytest.mark.unit
def test_repair_escalation_payload_is_attention_shaped() -> None:
    """Edge: build_repair_escalation returns an OLB-10 ``attention.Escalation``-shaped
    payload (the FR-046 route into the §8 FR-028 intake) carrying the repair as the
    suggested option, the confidence, and the reversibility — without calling
    intake_escalation. evaluate_repair also attaches the payload when a raised_at is
    supplied; a granted decision has no escalation to build."""
    action = _action(RepairKind.DISCARD_OR_REWRITE_COMMITTED, confidence=0.95)
    decision = evaluate_repair(
        action,
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=False,
    )
    escalation = build_repair_escalation(action, decision, raised_at=RAISED_AT)
    assert isinstance(escalation, Escalation)
    assert escalation.project_id == "proj-a"
    assert escalation.kind == ESCALATION_KIND_ROUTINE
    assert escalation.suggested_option == RepairKind.DISCARD_OR_REWRITE_COMMITTED.value
    assert escalation.confidence == 0.95
    assert escalation.reversible is False
    assert escalation.raised_at == RAISED_AT

    # evaluate_repair attaches the payload when raised_at is supplied.
    with_payload = evaluate_repair(
        action,
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=False,
        raised_at=RAISED_AT,
    )
    assert with_payload.escalation is not None
    assert with_payload.escalation.gate_id == escalation.gate_id

    # A granted repair has no escalation to build.
    granted = evaluate_repair(
        _action(),
        confidence_threshold=THRESHOLD,
        in_scope=True,
        safety_gate_refuses=False,
    )
    with pytest.raises(ValueError):
        build_repair_escalation(_action(), granted, raised_at=RAISED_AT)
