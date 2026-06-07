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


__all__ = ["format_pid_start_time", "probe_pid_start_time"]
