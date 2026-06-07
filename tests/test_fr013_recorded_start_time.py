"""FR-013 recorded half: the orchestrator OS start-time is captured at spawn and
persisted on the Run row, so a Supervisor restart can disambiguate pid reuse.

The live probe half (``__main__._pid_start_time``) and the pure decision
(``reattach.derive_reattach_decisions``) were already real; this covers the wiring
that records the value:

* the SpawnPort captures the spawned pid's start-time into ``SpawnResult``;
* ``admit_and_spawn`` forwards it to the injected ``record_start_time`` recorder
  (only when both a value and a recorder are present);
* the concrete ``Registry.set_run_orchestrator_start_time`` issues the UPDATE.

Hermetic: ``Popen`` is faked to a real pid (this process) so the psutil probe returns
a real start-time without launching anything; the registry/recorder are fakes.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from supervisor.admission import SpawnResult, admit_and_spawn
from supervisor.pid_probe import probe_pid_start_time
from supervisor.registry import Registry
from supervisor.safety_gates import BlastRadiusScope
from supervisor.spawn import OrchestratorSpawnPort

pytestmark = pytest.mark.unit


# --- (1) SpawnPort captures the start-time into SpawnResult ----------------------


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def test_spawn_captures_orchestrator_start_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "orchestrator.sh"
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    seed = tmp_path / "seed.md"
    seed.write_text("seed\n", encoding="utf-8")

    # Fake the launch to THIS process's pid so the psutil probe returns a real value
    # without actually spawning anything; accept the detach kwargs the call passes.
    def _fake_popen(*_a: object, **_k: object) -> _FakeProc:
        return _FakeProc(os.getpid())

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    port = OrchestratorSpawnPort(script, bash_executable=str(script))
    scope = BlastRadiusScope(
        read_only_paths=frozenset(),
        writable_paths=frozenset({str(tmp_path)}),
        mcp_roots=frozenset(),
        design_zone=None,
    )
    result = port.spawn(str(seed), scope)

    assert result.ok
    assert result.orchestrator_pid == os.getpid()
    # Identical formatter as the live re-attach probe → recorded == live can match.
    assert result.orchestrator_start_time == probe_pid_start_time(os.getpid())
    assert result.orchestrator_start_time is not None


# --- (2) admit_and_spawn forwards the start-time to the recorder ------------------


class _RecordingRegistry:
    """Minimal RegistryPort double that no-ops writes and records start-time calls."""

    def __init__(self) -> None:
        self.start_time_calls: list[tuple[str, str]] = []

    def set_lifecycle_state(self, project_id: str, state: str) -> None:
        return None

    def record_run(self, project_id: str, run: object) -> None:
        return None

    def update_run_status(self, project_id: str, status: str) -> None:
        return None

    def set_run_orchestrator_pid(self, project_id: str, orchestrator_pid: int) -> None:
        return None

    def set_run_orchestrator_start_time(self, project_id: str, start_time: str) -> None:
        self.start_time_calls.append((project_id, start_time))


class _Spawn:
    def __init__(self, result: SpawnResult) -> None:
        self._result = result

    def spawn(self, seed_path: str, blast_radius_scope: object) -> SpawnResult:
        return self._result


def _candidate() -> dict[str, object]:
    return {"project_id": "p1", "seed_path": "/seed"}


def _scope() -> BlastRadiusScope:
    return BlastRadiusScope(
        read_only_paths=frozenset(),
        writable_paths=frozenset({"/tmp"}),
        mcp_roots=frozenset(),
        design_zone=None,
    )


def test_admit_and_spawn_records_start_time_when_present() -> None:
    registry = _RecordingRegistry()
    spawn_port = _Spawn(
        SpawnResult(ok=True, orchestrator_pid=4321, orchestrator_start_time="999.000000")
    )

    admit_and_spawn(
        _candidate(),
        registry_port=registry,  # type: ignore[arg-type]
        spawn_port=spawn_port,  # type: ignore[arg-type]
        blast_radius_scope=_scope(),
        record_start_time=registry.set_run_orchestrator_start_time,
    )

    assert registry.start_time_calls == [("p1", "999.000000")]


def test_admit_and_spawn_skips_recorder_when_start_time_absent() -> None:
    registry = _RecordingRegistry()
    spawn_port = _Spawn(SpawnResult(ok=True, orchestrator_pid=4321))  # no start-time

    admit_and_spawn(
        _candidate(),
        registry_port=registry,  # type: ignore[arg-type]
        spawn_port=spawn_port,  # type: ignore[arg-type]
        blast_radius_scope=_scope(),
        record_start_time=registry.set_run_orchestrator_start_time,
    )

    assert registry.start_time_calls == []  # nothing to record → recorder not called


def test_admit_and_spawn_without_recorder_does_not_raise() -> None:
    spawn_port = _Spawn(
        SpawnResult(ok=True, orchestrator_pid=4321, orchestrator_start_time="999.000000")
    )
    # record_start_time omitted (None) — the unit/non-production path must still spawn.
    result = admit_and_spawn(
        _candidate(),
        registry_port=_RecordingRegistry(),  # type: ignore[arg-type]
        spawn_port=spawn_port,  # type: ignore[arg-type]
        blast_radius_scope=_scope(),
    )
    assert bool(result) is True


# --- (3) the concrete Registry write ---------------------------------------------


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: Sequence[object] = ()) -> None:
        self._conn.executed.append((query, tuple(params)))


class _Conn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1


def test_set_run_orchestrator_start_time_updates_running_row_and_commits() -> None:
    conn = _Conn()
    Registry(conn).set_run_orchestrator_start_time("p1", "1717000000.000000")

    sql, params = conn.executed[0]
    norm = " ".join(sql.split())
    assert norm.startswith("UPDATE ralph_runs SET orchestrator_start_time")
    assert "status = 'running'" in norm
    assert params == ("1717000000.000000", "p1")
    assert conn.commits == 1
