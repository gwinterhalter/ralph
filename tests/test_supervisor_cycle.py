"""Component tests for the OLB-01 supervision cycle host (``supervisor/cycle.py``).

Covers the OLB-01 predicate (Spec v1.3 §4.4; NFR-004 single-host, NFR-005
mechanical, NFR-006 sole-writer): one ``run_once()`` pass over a zero-candidate
registry executes the six supervision steps in order, issues no registry write,
and neither returns a value nor raises.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from supervisor.cycle import SupervisionCycle
from supervisor.ports import RegistryPort

# The six §4.4 supervision steps, in their normative order (Spec v1.3 §4.4).
EXPECTED_ORDER = ["reconcile", "admit", "schedule", "attend", "guard", "learn"]


class _ZeroRowRegistry:
    """A zero-candidate :class:`RegistryPort` stub.

    The ``read_*`` methods return empty sequences (no candidates, no running
    Projects); every write call appends to ``writes`` so a test can assert that
    none were made.
    """

    def __init__(self) -> None:
        self.writes: list[str] = []

    def read_candidates(self) -> list[dict[str, object]]:
        return []

    def read_running(self) -> list[dict[str, object]]:
        return []

    def set_lifecycle_state(self, project_id: str, state: str) -> None:
        self.writes.append("set_lifecycle_state")

    def record_run(self, project_id: str, run: dict[str, object]) -> None:
        self.writes.append("record_run")

    def update_run_status(self, project_id: str, status: str) -> None:
        self.writes.append("update_run_status")

    def reconcile_run(
        self,
        project_id: str,
        status: str,
        *,
        terminated_at: str,
        terminal_cost_usd: Decimal,
    ) -> None:
        self.writes.append("reconcile_run")

    def set_run_orchestrator_pid(
        self, project_id: str, orchestrator_pid: int
    ) -> None:
        self.writes.append("set_run_orchestrator_pid")


@pytest.fixture
def registry() -> _ZeroRowRegistry:
    """A fresh zero-row registry stub per test."""
    return _ZeroRowRegistry()


@pytest.mark.unit
def test_zero_row_stub_satisfies_registry_port(registry: _ZeroRowRegistry) -> None:
    """The injected stub is a structural RegistryPort (the OLB-01 -> OLB-02 seam
    is honoured: the cycle host depends only on this Protocol)."""
    assert isinstance(registry, RegistryPort)


@pytest.mark.unit
def test_run_once_executes_six_steps_in_section_4_4_order(
    registry: _ZeroRowRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OLB-01 predicate (a): ``run_once()`` invokes the six §4.4 hooks once each,
    in the order reconcile -> admit -> schedule -> attend -> guard -> learn."""
    cycle = SupervisionCycle(registry)
    calls: list[str] = []
    for step in EXPECTED_ORDER:
        monkeypatch.setattr(cycle, f"_{step}", lambda s=step: calls.append(s))

    cycle.run_once()

    assert calls == EXPECTED_ORDER


@pytest.mark.unit
def test_run_once_issues_no_registry_write_on_zero_candidate(
    registry: _ZeroRowRegistry,
) -> None:
    """OLB-01 predicate (b): a zero-candidate pass invokes no RegistryPort write
    method (NFR-006 sole-writer — the skeleton never writes)."""
    cycle = SupervisionCycle(registry)

    cycle.run_once()

    assert registry.writes == []


@pytest.mark.unit
def test_run_once_returns_none_and_does_not_raise_on_zero_candidate(
    registry: _ZeroRowRegistry,
) -> None:
    """OLB-01 predicate (c): ``run_once()`` over a zero-candidate registry returns
    None and raises nothing — a complete no-op pass."""
    cycle = SupervisionCycle(registry)

    result = cycle.run_once()

    assert result is None
