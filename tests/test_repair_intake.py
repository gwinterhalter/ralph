"""D3 — repair-intake bridge: reconcile stalls → Guard-step StallSignals (FR-046)."""

from __future__ import annotations

import pytest

from supervisor.cycle_wiring import stall_signals_from_actions
from supervisor.reconcile import (
    LIFECYCLE_FAILED,
    LIFECYCLE_PAUSED_GATE,
    REASON_DEAD_PID,
    REASON_STALLED,
    RUN_FAILED,
    RUN_HALTED,
    ReconcileAction,
)
from supervisor.repair_policy import RepairKind

pytestmark = pytest.mark.unit


def test_only_stalls_become_signals_dead_pid_excluded() -> None:
    actions = [
        ReconcileAction("stalled_proj", RUN_HALTED, LIFECYCLE_PAUSED_GATE, REASON_STALLED),
        ReconcileAction("dead_proj", RUN_FAILED, LIFECYCLE_FAILED, REASON_DEAD_PID),
    ]
    signals = stall_signals_from_actions(actions)
    assert set(signals) == {"stalled_proj"}  # dead-PID is terminally reaped, not a repair candidate
    sig = signals["stalled_proj"]
    assert sig.repair_kind == RepairKind.REATTACH_STALLED_RUN
    assert sig.triggering_anomaly == REASON_STALLED
    assert sig.in_scope is True
    assert sig.safety_gate_refuses is False


def test_classification_fields_overridable() -> None:
    actions = [ReconcileAction("p", RUN_HALTED, LIFECYCLE_PAUSED_GATE, REASON_STALLED)]
    signals = stall_signals_from_actions(
        actions, confidence=0.55, in_scope=False, safety_gate_refuses=True
    )
    sig = signals["p"]
    assert sig.confidence == 0.55
    assert sig.in_scope is False
    assert sig.safety_gate_refuses is True


def test_empty_and_no_stalls() -> None:
    assert stall_signals_from_actions([]) == {}
    only_dead = [ReconcileAction("d", RUN_FAILED, LIFECYCLE_FAILED, REASON_DEAD_PID)]
    assert stall_signals_from_actions(only_dead) == {}
