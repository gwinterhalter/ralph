"""Live orchestrator-spawn Port for the Outer Loop Supervisor (OLB-08 / C2).

The live ``SpawnPort`` impl OLB-07 deferred: invokes the real ``orchestrator.sh``
(Spec v1.3 §6.3 FR-021) as an OS subprocess against a Candidate's seed, confined
to the recorded Blast-Radius Scope, capturing the spawned orchestrator pid (the
§6.3 active boundary paired with ``spawned_at``). OLB-07 shipped the DB-free
admission decision layer behind the injectable
:class:`~supervisor.admission.SpawnPort` Protocol; this module is the live wiring
behind that seam — the C2 single-project end-to-end checkpoint.

It is a CONSUMER of the OLB-07 admission layer: it implements the existing
``SpawnPort`` Protocol (``spawn(seed_path, blast_radius_scope) -> SpawnResult``)
and is injected into :func:`~supervisor.admission.admit_and_spawn` unchanged. It
edits neither the admission layer nor the OLB-02 registry, and it originates no
substrate access of its own beyond launching the orchestrator inside the recorded
scope (the spawned Run confines itself to its own seed thereafter).
"""
from __future__ import annotations

import os
import shutil

# subprocess launches the framework orchestrator.sh with constant args, not caller
# text; shell is never used. See the Popen call site for the B603 rationale.
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path

from supervisor.admission import SpawnResult
from supervisor.pid_probe import probe_pid_start_time
from supervisor.safety_gates import BlastRadiusScope

#: Suffix for the captured orchestrator stdout+stderr stream, written alongside
#: the spawned seed so the §14 teardown (``supervisor.run_lifecycle``) can read the
#: §13.1 INITIATIVE_COMPLETE terminal signal off the spawned Run's own output.
SPAWN_LOG_SUFFIX = ".spawn.out"


def _detach_flags() -> tuple[int, bool]:
    """``(creationflags, start_new_session)`` that fully decouple the spawned
    orchestrator from the supervisor's console / controlling terminal.

    Two correctness reasons, not cosmetics:

    * **The orchestrator must outlive the supervisor cycle.** It runs its own
      role-call loop to terminal and is re-attached on a later cycle (FR-013); a
      child tied to the supervisor's session would be torn down the moment the
      supervisor process exits, defeating re-attach.
    * **No console-close cascade back to the parent.** A Windows child left on the
      parent's console delivers a CTRL_CLOSE/shutdown event to every process
      sharing that console when the supervisor exits — which can close the
      operator's own terminal. Detaching prevents that blast-back.

    On Windows ``DETACHED_PROCESS`` gives the child no console at all (stdio is
    already redirected to a file / DEVNULL) and ``CREATE_NEW_PROCESS_GROUP``
    isolates it from parent Ctrl signals; ``start_new_session`` is inert there. On
    POSIX ``start_new_session=True`` (``setsid``) moves it into its own
    session/process group, off the controlling terminal; ``creationflags`` is 0.
    Both kwargs are valid on every platform (the inert one defaults harmlessly), so
    the spawn passes both explicitly.
    """
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        return flags, False
    return 0, True


@dataclass(frozen=True)
class SpawnHandle:
    """A launched orchestrator subprocess — the live :class:`SpawnResult` companion.

    Carries the live ``Popen`` and the captured-stream path so the §14 teardown
    can wait on the process and read its terminal INITIATIVE_COMPLETE signal.
    Distinct from :class:`~supervisor.admission.SpawnResult` (the Protocol return
    shape, which carries only the pid the registry persists via FR-009).
    """

    process: subprocess.Popen[bytes]
    stdout_path: Path
    seed_path: Path


class OrchestratorSpawnPort:
    """Live :class:`~supervisor.admission.SpawnPort` — spawns ``orchestrator.sh``.

    Conforms structurally to the OLB-07 ``SpawnPort`` Protocol. ``orchestrator_script``
    is the framework controller (``Ralph-dev/orchestrator.sh``); ``bash_executable``
    is resolved from ``PATH`` when not supplied. The orchestrator is launched
    NON-BLOCKING — it runs its own role-call loop to terminal — with its working
    directory confined to a writable root of the recorded Blast-Radius Scope and
    no broader. The most recent launch is retained on :attr:`last_handle` so the
    §14 teardown can wait on it; the pid is returned in the :class:`SpawnResult`
    for the FR-009 ``set_run_orchestrator_pid`` persistence.
    """

    def __init__(
        self,
        orchestrator_script: Path,
        *,
        bash_executable: str | None = None,
    ) -> None:
        self._orchestrator_script = Path(orchestrator_script)
        # FUP-0867: resolve bash PATH-independently. Precedence: explicit arg >
        # OL_SUPERVISOR_BASH env (an absolute path to bash.exe) > PATH lookup. A clean
        # PowerShell launch shell has no `bash` on PATH, so `shutil.which("bash")` returned
        # None and the spawn failed with "no bash executable on PATH" (the 2026-06-11 M4G S1
        # blocker). The env override lets the operator pin Git bash without mutating PATH.
        self._bash = (
            bash_executable
            or os.environ.get("OL_SUPERVISOR_BASH")
            or shutil.which("bash")
        )
        self.last_handle: SpawnHandle | None = None

    def spawn(
        self, seed_path: str, blast_radius_scope: BlastRadiusScope
    ) -> SpawnResult:
        """Spawn the orchestrator Run for ``seed_path`` confined to ``blast_radius_scope``.

        Returns ``SpawnResult(ok=True, orchestrator_pid=<pid>)`` once the subprocess
        is launched, or ``SpawnResult(ok=False, detail=...)`` on any pre-launch or
        launch failure (``admit_and_spawn`` reconciles the Run row either way).
        """
        if self._bash is None:
            return SpawnResult(
                ok=False,
                detail="no `bash` executable on PATH to run orchestrator.sh",
            )
        if not self._orchestrator_script.is_file():
            return SpawnResult(
                ok=False,
                detail=f"orchestrator script not found: {self._orchestrator_script}",
            )
        cwd = self._confined_cwd(blast_radius_scope)
        if cwd is None:
            return SpawnResult(
                ok=False,
                detail=(
                    "Blast-Radius Scope declares no existing writable root to "
                    "confine the orchestrator spawn to (FR-020)"
                ),
            )
        seed = Path(seed_path)
        stdout_path = seed.with_name(seed.name + SPAWN_LOG_SUFFIX)
        # Capture stdout+stderr to a file co-located with the seed. The parent's
        # handle is closed once Popen has dup'd it to the child (the `with` block);
        # the child keeps writing through its own inherited descriptor.
        # Detach from the supervisor's console/session: the orchestrator must survive
        # the supervisor exiting (FR-013 re-attach) and must not blast a console-close
        # event back to the parent terminal.
        creationflags, new_session = _detach_flags()
        try:
            with stdout_path.open("wb") as stream:
                # shell=False; argv is framework-internal constants (bash +
                # orchestrator.sh + seed path), never caller text — no shell-injection
                # vector, so the B603/S603 subprocess warnings are suppressed.
                process = subprocess.Popen(  # noqa: S603  # nosec B603
                    [self._bash, str(self._orchestrator_script), str(seed)],
                    cwd=str(cwd),
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=creationflags,
                    start_new_session=new_session,
                )
        except OSError as exc:
            return SpawnResult(ok=False, detail=f"orchestrator spawn failed: {exc!r}")
        self.last_handle = SpawnHandle(
            process=process, stdout_path=stdout_path, seed_path=seed
        )
        # FR-013 recorded half: capture the spawned orchestrator's OS start-time now,
        # via the SAME formatter the live re-attach probe uses, so a later
        # `recorded == live` comparison can positively disambiguate pid reuse. None when
        # psutil is absent or the child already exited (re-attach then falls back to the
        # conservative branch — it never reaps a live pid it cannot disprove is ours).
        start_time = probe_pid_start_time(process.pid)
        return SpawnResult(
            ok=True,
            orchestrator_pid=process.pid,
            orchestrator_start_time=start_time,
        )

    @staticmethod
    def _confined_cwd(scope: BlastRadiusScope) -> Path | None:
        """The deterministic writable root the spawn is confined to, or ``None``.

        Picks the lexicographically-first existing directory in the scope's
        ``writable_paths`` so the orchestrator's relative-path operations stay
        inside the recorded scope; returns ``None`` when no writable root exists on
        disk (the caller turns that into a reconcilable spawn failure).
        """
        for raw in sorted(scope.writable_paths):
            candidate = Path(raw)
            if candidate.is_dir():
                return candidate
        return None


__all__ = ["SPAWN_LOG_SUFFIX", "SpawnHandle", "OrchestratorSpawnPort"]
