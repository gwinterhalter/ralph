"""Item 3 — gate-escalation delivery wiring (run_attend_step -> notification port).

The OLB-10 attention layer planned notifications but nothing delivered them. ``run_attend_step``
now dispatches the planned batches through an injected notification port, deduped by a delivered
ledger so an unresolved escalation is paged once (not every cycle), and the production wiring
shares one attention store across Guard + Attend. These hermetic tests assert the delivery,
the dedup, and the shared-store flow against a fake port (no socket).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from supervisor.attention import (
    ESCALATION_KIND_SAFETY_GATE,
    Escalation,
    NotificationPlan,
    intake_escalation,
)
from supervisor.cycle_wiring import AttendConfig, AttentionStateStore, run_attend_step

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


def _esc(pid: str = "p1") -> Escalation:
    return Escalation(
        project_id=pid,
        gate_id=f"{pid}-gate",
        kind=ESCALATION_KIND_SAFETY_GATE,  # top-tier — surfaced immediately, never quiet-deferred
        reversible=False,
        suggested_option=None,
        confidence=1.0,
        raised_at=_T0,
    )


class _FakePort:
    """Records each delivered plan; reports the escalation count as 'sent'."""

    def __init__(self) -> None:
        self.delivered: list[NotificationPlan] = []

    def deliver(self, plan: NotificationPlan) -> int:
        self.delivered.append(plan)
        return sum(len(b.escalations) for b in plan.batches)


def _registry() -> object:
    return object()  # run_attend_step deletes its registry arg (OLB-16 read seam, unread)


def test_attend_delivers_planned_escalation() -> None:
    port = _FakePort()
    config = AttendConfig(
        notification_port=port,  # type: ignore[arg-type]
        delivered_keys=set(),
        incoming=lambda: (_esc(),),
        clock=lambda: _T0,
    )
    plan = run_attend_step(_registry(), config)  # type: ignore[arg-type]

    assert len(plan.batches) == 1  # the top-tier escalation was planned
    assert len(port.delivered) == 1  # ...and delivered
    assert port.delivered[0].batches[0].escalations[0].project_id == "p1"


def test_attend_default_port_is_noop() -> None:
    # Default AttendConfig uses NullNotificationPort + no ledger — planning works, nothing raised.
    config = AttendConfig(incoming=lambda: (_esc(),), clock=lambda: _T0)
    plan = run_attend_step(_registry(), config)  # type: ignore[arg-type]
    assert len(plan.batches) == 1  # planned, delivered to the no-op port without error


def test_attend_dedups_unresolved_escalation_across_cycles() -> None:
    port = _FakePort()
    ledger: set[tuple[str, str, str]] = set()
    store = AttentionStateStore()
    pending = [(_esc(),), ()]  # first cycle intakes it; second cycle has no new escalation

    def _incoming() -> tuple[Escalation, ...]:
        return pending.pop(0) if pending else ()

    config = AttendConfig(
        attention_store=store,
        notification_port=port,  # type: ignore[arg-type]
        delivered_keys=ledger,
        incoming=_incoming,
        clock=lambda: _T0,
    )

    run_attend_step(_registry(), config)  # type: ignore[arg-type]  # delivers once
    run_attend_step(_registry(), config)  # type: ignore[arg-type]  # same queued esc → no re-send

    assert len(port.delivered) == 1  # paged once, not every cycle
    assert ledger == {("p1", "p1-gate", _T0.isoformat())}


def test_attend_delivers_escalation_intaken_into_shared_store() -> None:
    # Simulate the Guard step intaking an escalation into the SHARED store; the Attend step
    # then plans + delivers it from the accumulated state (the production Guard+Attend flow).
    store = AttentionStateStore()
    store.save(intake_escalation(store.load(), _esc("guarded")))
    port = _FakePort()
    config = AttendConfig(
        attention_store=store,
        notification_port=port,  # type: ignore[arg-type]
        delivered_keys=set(),
        incoming=lambda: (),  # no NEW escalation this pass; it came via the shared store
        clock=lambda: _T0,
    )

    run_attend_step(_registry(), config)  # type: ignore[arg-type]

    assert len(port.delivered) == 1
    assert port.delivered[0].batches[0].escalations[0].project_id == "guarded"
