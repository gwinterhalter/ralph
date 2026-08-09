"""Supervisor loop control — run one cycle / start / stop / probe the autonomous loop from the GUI.

⚠ SPAWNS REAL ORCHESTRATORS (real $). The API gates this behind an explicit operator confirm; the
supervisor's own kill-switch + spend-ceiling rails still apply to whatever it dispatches. The loop
process is detached (outlives the request) and tracked by a pid file so it can be stopped later.
Injected into the app so tests substitute a fake runner (no real spawn).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path


def _detach_flags() -> dict[str, object]:
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


class SupervisorRunner:
    """Launches/stops `python -m supervisor` for the GUI control surface."""

    def __init__(self, repo_root: str | Path, pid_file: str | Path) -> None:
        self.repo_root = Path(repo_root)
        self.pid_file = Path(pid_file)

    def _spawn(self, args: list[str]) -> int:
        proc = subprocess.Popen(
            [sys.executable, "-m", "supervisor", *args],
            cwd=str(self.repo_root), env=dict(os.environ),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            **_detach_flags(),  # type: ignore[arg-type]
        )
        return proc.pid

    def run_once(self) -> int:
        """Spawn a single supervisor cycle (`--once`) detached; returns its pid."""
        return self._spawn(["--once"])

    def start_loop(self, interval: float = 30.0) -> int:
        """Start the autonomous loop (`--interval`) detached + record its pid. Errors if already up."""
        if self.loop_running():
            raise RuntimeError("supervisor loop already running")
        pid = self._spawn(["--interval", str(interval)])
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(str(pid), encoding="utf-8")
        return pid

    def stop_loop(self) -> bool:
        """Terminate the tracked loop (if any); returns True iff a pid was tracked."""
        pid = self._read_pid()
        if pid is None:
            return False
        try:
            import psutil

            if psutil.pid_exists(pid):
                psutil.Process(pid).terminate()
        except Exception:
            logging.getLogger(__name__).debug("best-effort stop failed", exc_info=True)
        self.pid_file.unlink(missing_ok=True)
        return True

    def loop_running(self) -> bool:
        pid = self._read_pid()
        if pid is None:
            return False
        try:
            import psutil

            return bool(psutil.pid_exists(pid))
        except Exception:  # noqa: BLE001
            return False

    def _read_pid(self) -> int | None:
        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None


__all__ = ["SupervisorRunner"]
