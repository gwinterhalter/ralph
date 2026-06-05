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

import shutil

# subprocess launches the framework orchestrator.sh with constant args, not caller
# text; shell is never used. See the Popen call site for the B603 rationale.
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path

from supervisor.admission import SpawnResult
from supervisor.safety_gates import BlastRadiusScope

#: Suffix for the captured orchestrator stdout+stderr stream, written alongside
#: the spawned seed so the §14 teardown (``supervisor.run_lifecycle``) can read the
#: §13.1 INITIATIVE_COMPLETE terminal signal off the spawned Run's own output.
SPAWN_LOG_SUFFIX = ".spawn.out"


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
        self._bash = bash_executable or shutil.which("bash")
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
                )
        except OSError as exc:
            return SpawnResult(ok=False, detail=f"orchestrator spawn failed: {exc!r}")
        self.last_handle = SpawnHandle(
            process=process, stdout_path=stdout_path, seed_path=seed
        )
        return SpawnResult(ok=True, orchestrator_pid=process.pid)

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
