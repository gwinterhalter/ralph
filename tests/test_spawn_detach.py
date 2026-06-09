"""Regression: the orchestrator spawn detaches from the supervisor's console.

Surfaced operationally — running ``python -m supervisor`` repeatedly closed the
operator's own terminal. Root cause: the orchestrator ``Popen`` carried no
platform detach flags, so on Windows the child stayed on the parent's console and
a console-shutdown (CTRL_CLOSE) event cascaded back to the operator's shell when
the supervisor process exited. It is also the FR-013 precondition: a re-attachable
orchestrator must outlive the supervisor cycle, which requires its own session.

DB-free / hermetic: exercises :func:`supervisor.spawn._detach_flags` directly on
both platform branches via monkeypatched ``os.name`` — no spawn, no live branch.
"""
from __future__ import annotations

import subprocess

import pytest

from supervisor import spawn
from supervisor.spawn import _detach_flags

pytestmark = pytest.mark.unit


def test_windows_branch_detaches_console_and_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows the child gets DETACHED_PROCESS (no console) + a new process
    group (isolated from parent Ctrl signals); start_new_session is inert there."""
    monkeypatch.setattr(spawn.os, "name", "nt")
    creationflags, new_session = _detach_flags()

    expected = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    assert creationflags == expected
    assert new_session is False
    # On a real Windows interpreter both flags are non-zero and present.
    if hasattr(subprocess, "DETACHED_PROCESS"):
        assert creationflags & subprocess.DETACHED_PROCESS
        assert creationflags & subprocess.CREATE_NEW_PROCESS_GROUP


def test_posix_branch_starts_a_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """On POSIX the child is moved into its own session (``setsid``), off the
    controlling terminal; no Windows creationflags are set."""
    monkeypatch.setattr(spawn.os, "name", "posix")
    assert _detach_flags() == (0, True)


def test_detach_kwargs_are_accepted_by_popen_signature() -> None:
    """``creationflags`` / ``start_new_session`` are real ``Popen`` parameters on
    this platform (guards a typo that would only surface at live-spawn time)."""
    import inspect

    params = inspect.signature(subprocess.Popen.__init__).parameters
    assert "creationflags" in params
    assert "start_new_session" in params
