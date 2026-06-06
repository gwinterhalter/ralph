"""T1#1 — §4.4(1) Reconcile step: orphan/stall reaping.

Covers the pure ``derive_reconcile_actions`` core and the ``run_reconcile_step``
composition that applies it through the RegistryPort write seam.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from supervisor.cycle_wiring import ReconcileConfig, run_reconcile_step
from supervisor.reconcile import (
    LIFECYCLE_FAILED,
    LIFECYCLE_PAUSED_GATE,
    REASON_DEAD_PID,
    REASON_STALLED,
    RUN_FAILED,
    RUN_HALTED,
    derive_reconcile_actions,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_HANG = 1800.0


def _run(project_id: str, *, pid: int | None, age_seconds: float, cost: str = "0") -> dict[str, object]:
    spawned = (_NOW - timedelta(seconds=age_seconds)).isoformat()
    row: dict[str, object] = {
        "project_id": project_id,
        "spawned_at": spawned,
        "terminal_cost_usd": Decimal(cost),
    }
    if pid is not None:
        row["orchestrator_pid"] = pid
    return row


# --- pure core --------------------------------------------------------------


def test_dead_pid_reaped_to_failed() -> None:
    runs = [_run("p1", pid=4242, age_seconds=10)]  # young, but PID dead
    actions = derive_reconcile_actions(
        runs, pid_alive=lambda _pid: False, now=_NOW, hang_timeout_seconds=_HANG
    )
    assert len(actions) == 1
    assert actions[0].project_id == "p1"
    assert actions[0].run_status == RUN_FAILED
    assert actions[0].lifecycle_state == LIFECYCLE_FAILED
    assert actions[0].reason == REASON_DEAD_PID


def test_stalled_run_paused_gate() -> None:
    runs = [_run("p2", pid=4242, age_seconds=_HANG + 60)]  # alive but past hang budget
    actions = derive_reconcile_actions(
        runs, pid_alive=lambda _pid: True, now=_NOW, hang_timeout_seconds=_HANG
    )
    assert len(actions) == 1
    assert actions[0].run_status == RUN_HALTED
    assert actions[0].lifecycle_state == LIFECYCLE_PAUSED_GATE
    assert actions[0].reason == REASON_STALLED


def test_healthy_run_no_action() -> None:
    runs = [_run("p3", pid=4242, age_seconds=30)]  # alive + recent
    actions = derive_reconcile_actions(
        runs, pid_alive=lambda _pid: True, now=_NOW, hang_timeout_seconds=_HANG
    )
    assert actions == []


def test_dead_pid_precedes_stall_check() -> None:
    # A dead PID that is also old reaps as failed (orphan), not halted (stall).
    runs = [_run("p4", pid=99, age_seconds=_HANG + 999)]
    actions = derive_reconcile_actions(
        runs, pid_alive=lambda _pid: False, now=_NOW, hang_timeout_seconds=_HANG
    )
    assert actions[0].reason == REASON_DEAD_PID


def test_no_pid_uses_stall_only() -> None:
    # No orchestrator_pid recorded: only the stall path can fire.
    fresh = [_run("p5", pid=None, age_seconds=30)]
    assert derive_reconcile_actions(
        fresh, pid_alive=lambda _pid: False, now=_NOW, hang_timeout_seconds=_HANG
    ) == []
    stale = [_run("p6", pid=None, age_seconds=_HANG + 1)]
    assert len(derive_reconcile_actions(
        stale, pid_alive=lambda _pid: False, now=_NOW, hang_timeout_seconds=_HANG
    )) == 1


def test_missing_project_id_and_bad_timestamp_skipped() -> None:
    runs: list[dict[str, object]] = [
        {"spawned_at": (_NOW - timedelta(hours=2)).isoformat()},  # no project_id
        {"project_id": "p7", "spawned_at": "not-a-timestamp"},  # unparseable
    ]
    assert derive_reconcile_actions(
        runs, pid_alive=lambda _pid: True, now=_NOW, hang_timeout_seconds=_HANG
    ) == []


# --- run_reconcile_step composition -----------------------------------------


class _FakeRegistry:
    def __init__(self) -> None:
        self.reconciled: list[tuple[str, str, str, Decimal]] = []
        self.lifecycle: list[tuple[str, str]] = []

    def reconcile_run(
        self, project_id: str, status: str, *, terminated_at: str, terminal_cost_usd: Decimal
    ) -> None:
        self.reconciled.append((project_id, status, terminated_at, terminal_cost_usd))

    def set_lifecycle_state(self, project_id: str, state: str) -> None:
        self.lifecycle.append((project_id, state))


def test_run_reconcile_step_applies_and_releases_slot() -> None:
    reg = _FakeRegistry()
    runs = [
        _run("dead", pid=99, age_seconds=10, cost="1.2500"),
        _run("healthy", pid=1, age_seconds=5),
    ]
    config = ReconcileConfig(
        active_runs_source=lambda: runs,
        pid_alive=lambda pid: pid != 99,  # 99 is dead
        clock=lambda: _NOW,
    )
    actions = run_reconcile_step(reg, config)  # type: ignore[arg-type]

    assert [a.project_id for a in actions] == ["dead"]
    # reconcile_run carries the run's last-known cost + a terminated_at boundary.
    assert reg.reconciled == [("dead", RUN_FAILED, _NOW.isoformat(), Decimal("1.2500"))]
    # lifecycle moved running -> failed (slot released by status != running).
    assert reg.lifecycle == [("dead", LIFECYCLE_FAILED)]


def test_run_reconcile_step_noop_when_all_healthy() -> None:
    reg = _FakeRegistry()
    config = ReconcileConfig(
        active_runs_source=lambda: [_run("ok", pid=1, age_seconds=5)],
        pid_alive=lambda _pid: True,
        clock=lambda: _NOW,
    )
    assert run_reconcile_step(reg, config) == []  # type: ignore[arg-type]
    assert reg.reconciled == []
    assert reg.lifecycle == []


def test_run_reconcile_step_project_filter_scopes() -> None:
    reg = _FakeRegistry()
    runs = [_run("oltest_x", pid=99, age_seconds=10), _run("prod_y", pid=99, age_seconds=10)]
    config = ReconcileConfig(
        active_runs_source=lambda: runs,
        pid_alive=lambda _pid: False,
        clock=lambda: _NOW,
        project_filter=lambda row: str(row.get("project_id", "")).startswith("oltest_"),
    )
    actions = run_reconcile_step(reg, config)  # type: ignore[arg-type]
    assert [a.project_id for a in actions] == ["oltest_x"]
