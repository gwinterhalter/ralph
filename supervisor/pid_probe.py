"""Shared OS process start-time probe for FR-013 pid-reuse disambiguation.

The FR-013 re-attach decision compares a Run's **recorded** orchestrator start-time
(persisted at spawn) against the **live** OS start-time of the recorded pid. The two
sides MUST be produced by the identical formatter or a spurious pid-reuse verdict
results, so both the recorder (:mod:`supervisor.spawn`, at spawn) and the live probe
(:mod:`supervisor.__main__`, at the re-attach pass) source the format here.

Pure-ish: :func:`format_pid_start_time` is a pure float→str formatter;
:func:`probe_pid_start_time` lazily imports :mod:`psutil` so importing this module —
and the hermetic unit suite — needs no driver and no live process.
"""
from __future__ import annotations

import os


def pid_alive(pid: int) -> bool:
    """OS pid-liveness probe — **read-only on every platform**.

    On POSIX ``os.kill(pid, 0)`` is a no-op existence check (ProcessLookupError =
    dead; PermissionError = alive, owned by another user). On Windows it is NOT a
    probe: Python's ``os.kill`` has no signal-0 special case and calls
    ``TerminateProcess(handle, 0)`` — which would **kill a live pid** (and raise
    ``OSError`` for a dead one, misreporting it alive). So Windows uses a
    non-destructive :func:`psutil.pid_exists` check; if psutil is unavailable it
    returns ``True`` (never reap on Windows when the probe can't run safely — the
    INITIATIVE_COMPLETE completion probe still classifies clean exits). The single
    source of the liveness probe for the Reconcile + re-attach passes.
    """
    if os.name == "nt":
        try:
            import psutil
        except ImportError:
            return True
        return bool(psutil.pid_exists(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def format_pid_start_time(create_time: float) -> str:
    """Canonical string form of an OS process create-time (epoch seconds).

    Fixed 6-dp so platform float-repr differences can't produce a spurious pid-reuse
    verdict. The single source of the recorded/live FR-013 comparison format.
    """
    return f"{create_time:.6f}"


def probe_pid_start_time(pid: int) -> str | None:
    """Live OS process start-time for ``pid`` (canonical string), or ``None``.

    Returns ``None`` when :mod:`psutil` is unavailable or the process is gone —
    :func:`supervisor.reattach.derive_reattach_decisions` treats ``None`` as
    'cannot disambiguate' and conservatively re-attaches (it never reaps a live pid
    it cannot positively disprove is ours).
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        return format_pid_start_time(psutil.Process(pid).create_time())
    except psutil.Error:
        return None


__all__ = ["pid_alive", "format_pid_start_time", "probe_pid_start_time"]
