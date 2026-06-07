"""Regression: the live Reconcile inputs that make completion-classification work.

Two production-only bugs surfaced by the multi-project fleet run, both invisible to
the existing fakes:

1. ``Registry.read_active_runs`` did not select ``seed_path``, so the production
   completion probe (``__main__._completion_of``) read ``None`` for every run, never
   detected §13.1 INITIATIVE_COMPLETE, and a cleanly-completed Run was mis-reaped as a
   stall (``halted`` / ``paused_gate``) instead of ``complete``.
2. The PID-liveness probes used ``os.kill(pid, 0)`` unconditionally. On Windows that
   is not a probe — it calls ``TerminateProcess(handle, 0)`` — so the Reconcile pass
   would terminate the very live orchestrator it was observing (and report dead pids
   as alive). The probes now use a read-only ``psutil.pid_exists`` check on Windows.

Hermetic: a fake connection for (1); monkeypatched ``os.name`` + ``psutil.pid_exists``
for (2). No live branch, no real process is ever touched.
"""
from __future__ import annotations

from collections.abc import Sequence

import psutil
import pytest

from supervisor import __main__ as svmain
from supervisor import cycle_wiring
from supervisor.registry import Registry

pytestmark = pytest.mark.unit


# --- (1) read_active_runs carries seed_path for the completion probe -------------


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: Sequence[object] = ()) -> None:
        self._conn.executed.append((query, tuple(params)))

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._conn.fetchall_result


class _Conn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.fetchall_result: list[tuple[object, ...]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:  # pragma: no cover - reads don't commit
        pass


def test_read_active_runs_selects_and_maps_seed_path() -> None:
    conn = _Conn()
    # row order must match the SELECT column tuple the implementation issues.
    conn.fetchall_result = [
        ("oltest_d1", "r1", 4242, None, None, "K:/work/oltest_d1/Seed.md"),
    ]
    rows = Registry(conn).read_active_runs()

    sql, params = conn.executed[0]
    assert "seed_path" in sql, "read_active_runs must SELECT seed_path"
    assert params == ("running",)
    assert rows[0]["seed_path"] == "K:/work/oltest_d1/Seed.md"
    # FR-010 soft reference is surfaced as project_id for the reconcile keying.
    assert rows[0]["project_id"] == "oltest_d1"


# --- (2) PID-liveness probes are read-only on Windows ----------------------------


@pytest.mark.parametrize(
    "probe",
    [svmain._pid_alive, cycle_wiring._default_pid_alive],
    ids=["main._pid_alive", "cycle_wiring._default_pid_alive"],
)
def test_pid_alive_on_windows_never_calls_os_kill(
    probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the Windows branch the probe must consult psutil.pid_exists and must NOT
    reach ``os.kill`` (which on Windows would TerminateProcess the pid)."""
    module = svmain if probe is svmain._pid_alive else cycle_wiring
    monkeypatch.setattr(module.os, "name", "nt")

    def _boom(*_a: object, **_k: object) -> None:  # pragma: no cover - must not run
        raise AssertionError("os.kill must never be called on the Windows branch")

    monkeypatch.setattr(module.os, "kill", _boom)

    seen: list[int] = []

    def _exists(pid: int) -> bool:
        seen.append(pid)
        return pid == 4242

    monkeypatch.setattr(psutil, "pid_exists", _exists)

    assert probe(4242) is True
    assert probe(9999) is False
    assert seen == [4242, 9999]  # the read-only check ran for both


def test_pid_alive_on_posix_uses_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """On POSIX the dead-pid path returns False via ProcessLookupError (unchanged)."""
    monkeypatch.setattr(svmain.os, "name", "posix")

    def _kill(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(svmain.os, "kill", _kill)
    assert svmain._pid_alive(12345) is False
