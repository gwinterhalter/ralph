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
from supervisor import pid_probe
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
    # row order must match the SELECT column tuple the implementation issues:
    # (project_slug, run_id, orchestrator_pid, metadata, spawned_at,
    #  terminal_cost_usd, seed_path)
    conn.fetchall_result = [
        (
            "oltest_d1",
            "r1",
            4242,
            {"pid_start_time": "1717000000.000000"},  # psycopg adapts jsonb → dict
            None,
            None,
            "K:/work/oltest_d1/Seed.md",
        ),
    ]
    rows = Registry(conn).read_active_runs()

    sql, params = conn.executed[0]
    assert "seed_path" in sql, "read_active_runs must SELECT seed_path"
    assert "metadata" in sql, "read_active_runs must SELECT metadata (FR-013 pid_start_time home)"
    assert params == ("running",)
    assert rows[0]["seed_path"] == "K:/work/oltest_d1/Seed.md"
    # FR-013 recorded start-time surfaced flat from metadata.pid_start_time.
    assert rows[0]["pid_start_time"] == "1717000000.000000"
    # FR-010 soft reference is surfaced as project_id for the reconcile keying.
    assert rows[0]["project_id"] == "oltest_d1"


def test_read_active_runs_pid_start_time_none_when_metadata_absent() -> None:
    conn = _Conn()
    conn.fetchall_result = [("p", "r", None, None, None, None, "/s")]  # metadata NULL
    rows = Registry(conn).read_active_runs()
    assert rows[0]["pid_start_time"] is None


# --- (2) PID-liveness probe is read-only on Windows ------------------------------
# The probe is the single shared `pid_probe.pid_alive`; __main__._pid_alive and
# cycle_wiring._default_pid_alive are aliases of it (consolidated — no duplication).


def test_probe_is_a_single_shared_function() -> None:
    assert svmain._pid_alive is pid_probe.pid_alive
    assert cycle_wiring._default_pid_alive is pid_probe.pid_alive


def test_pid_alive_on_windows_never_calls_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the Windows branch the probe must consult psutil.pid_exists and must NOT
    reach ``os.kill`` (which on Windows would TerminateProcess the pid)."""
    monkeypatch.setattr(pid_probe.os, "name", "nt")

    def _boom(*_a: object, **_k: object) -> None:  # pragma: no cover - must not run
        raise AssertionError("os.kill must never be called on the Windows branch")

    monkeypatch.setattr(pid_probe.os, "kill", _boom)

    seen: list[int] = []

    def _exists(pid: int) -> bool:
        seen.append(pid)
        return pid == 4242

    monkeypatch.setattr(psutil, "pid_exists", _exists)

    assert pid_probe.pid_alive(4242) is True
    assert pid_probe.pid_alive(9999) is False
    assert seen == [4242, 9999]  # the read-only check ran for both


def test_pid_alive_on_posix_uses_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """On POSIX the dead-pid path returns False via ProcessLookupError (unchanged)."""
    monkeypatch.setattr(pid_probe.os, "name", "posix")

    def _kill(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(pid_probe.os, "kill", _kill)
    assert pid_probe.pid_alive(12345) is False
