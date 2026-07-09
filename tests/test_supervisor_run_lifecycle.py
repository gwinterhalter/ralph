"""Dedicated unit tests for supervisor.run_lifecycle — the §14 teardown: read a spawned Run's
terminal outcome (INITIATIVE_COMPLETE signal + exact-Decimal cost) and reconcile it to complete.

Covers: exact/rounded/absent cost read; signal detection (log/stdout/absent/no-file); the
resolve_run_terminal classification (completed / no-signal / non-zero exit / timeout-kill); and
reconcile_run_complete (calls the ports on a completed run, refuses an incomplete one).
"""
from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from supervisor.run_lifecycle import (
    RunTerminal,
    detect_initiative_complete,
    read_terminal_cost,
    reconcile_run_complete,
    resolve_run_terminal,
    wait_for_orchestrator,
)


# ---- read_terminal_cost (FR-014 / NFR-007 exact Decimal at numeric(10,4) grain) ----

def test_cost_absent_ledger_is_zero(tmp_path: Path) -> None:
    assert read_terminal_cost(tmp_path) == Decimal("0.0000")


def test_cost_exact_no_float_drift(tmp_path: Path) -> None:
    (tmp_path / "spend.json").write_text('{"total_spend_usd": 3.2567}', encoding="utf-8")
    assert read_terminal_cost(tmp_path) == Decimal("3.2567")


def test_cost_quantizes_half_up_to_the_money_grain(tmp_path: Path) -> None:
    (tmp_path / "spend.json").write_text('{"total_spend_usd": 1.23455}', encoding="utf-8")
    assert read_terminal_cost(tmp_path) == Decimal("1.2346")


# ---- detect_initiative_complete ----

def _write_log(tmp_path: Path, text: str) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "orchestrator.log").write_text(text, encoding="utf-8")


def test_detect_signal_in_orchestrator_log(tmp_path: Path) -> None:
    _write_log(tmp_path, "2026 ITERATION 0003 end\n2026 INITIATIVE_COMPLETE: all passed\n")
    assert detect_initiative_complete(tmp_path) is True


def test_detect_signal_in_stdout_capture(tmp_path: Path) -> None:
    sp = tmp_path / "spawn_stdout.txt"
    sp.write_text("INITIATIVE_COMPLETE\n", encoding="utf-8")
    assert detect_initiative_complete(tmp_path, sp) is True


def test_detect_signal_absent(tmp_path: Path) -> None:
    _write_log(tmp_path, "ITERATION 0003 end\nHALT: something\n")
    assert detect_initiative_complete(tmp_path) is False


def test_detect_no_files_is_false(tmp_path: Path) -> None:
    assert detect_initiative_complete(tmp_path) is False


# ---- resolve_run_terminal (classification over a spawned process) ----

class _FakeProc:
    """A minimal Popen stand-in: .wait(timeout) returns rc (or raises TimeoutExpired); .kill()."""

    def __init__(self, rc: int, *, timeout: bool = False) -> None:
        self._rc = rc
        self._timeout = timeout
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd="orchestrator", timeout=timeout)
        return self._rc

    def kill(self) -> None:
        self.killed = True


def test_resolve_completed_run(tmp_path: Path) -> None:
    _write_log(tmp_path, "INITIATIVE_COMPLETE\n")
    (tmp_path / "spend.json").write_text('{"total_spend_usd": 2.0}', encoding="utf-8")
    t = resolve_run_terminal(_FakeProc(0), tmp_path, timeout_s=1.0, clock=lambda: "T")
    assert t.completed and bool(t) is True
    assert t.exit_code == 0
    assert t.terminal_cost_usd == Decimal("2.0000")
    assert t.detail == ""


def test_resolve_exit0_but_no_signal_is_incomplete(tmp_path: Path) -> None:
    t = resolve_run_terminal(_FakeProc(0), tmp_path, timeout_s=1.0, clock=lambda: "T")
    assert not t.completed
    assert "without INITIATIVE_COMPLETE" in t.detail


def test_resolve_nonzero_exit_is_incomplete_even_with_signal(tmp_path: Path) -> None:
    _write_log(tmp_path, "INITIATIVE_COMPLETE\n")
    t = resolve_run_terminal(_FakeProc(3), tmp_path, timeout_s=1.0, clock=lambda: "T")
    assert not t.completed
    assert t.exit_code == 3


def test_resolve_timeout_kills_and_is_incomplete(tmp_path: Path) -> None:
    proc = _FakeProc(0, timeout=True)
    t = resolve_run_terminal(proc, tmp_path, timeout_s=0.01, clock=lambda: "T")
    assert not t.completed
    assert t.exit_code is None
    assert proc.killed is True
    assert "did not terminate" in t.detail


def test_wait_for_orchestrator_returns_code_or_none() -> None:
    assert wait_for_orchestrator(_FakeProc(5), timeout_s=1.0) == 5
    assert wait_for_orchestrator(_FakeProc(0, timeout=True), timeout_s=0.01) is None


# ---- reconcile_run_complete (calls the existing ports; refuses incomplete) ----

class _FakeRegistry:
    def __init__(self) -> None:
        self.reconciled: tuple | None = None
        self.lifecycle: tuple | None = None

    def reconcile_run(self, project_id, status, *, terminated_at, terminal_cost_usd):
        self.reconciled = (project_id, status, terminated_at, terminal_cost_usd)

    def set_lifecycle_state(self, project_id, state):
        self.lifecycle = (project_id, state)


def test_reconcile_completed_run_calls_both_ports() -> None:
    reg = _FakeRegistry()
    terminal = RunTerminal(
        completed=True, exit_code=0, terminated_at="T", terminal_cost_usd=Decimal("2.0000")
    )
    reconcile_run_complete(reg, "p1", terminal)
    assert reg.reconciled == ("p1", "complete", "T", Decimal("2.0000"))
    assert reg.lifecycle == ("p1", "complete")


def test_reconcile_refuses_an_incomplete_run() -> None:
    reg = _FakeRegistry()
    terminal = RunTerminal(
        completed=False, exit_code=1, terminated_at="T",
        terminal_cost_usd=Decimal("0.0000"), detail="exited 1 without INITIATIVE_COMPLETE",
    )
    with pytest.raises(ValueError):
        reconcile_run_complete(reg, "p1", terminal)
    assert reg.reconciled is None  # nothing written on the refusal path
    assert reg.lifecycle is None
