"""FR-013 Run re-attach — reconnect to a still-live Run after a Supervisor restart (D5).

When the Supervisor restarts it finds ``ralph_runs`` rows still marked ``running``
with a recorded ``orchestrator_pid``. It must NOT blindly re-spawn (that would
duplicate the Run, violating the active-run uniqueness index) nor blindly reap (the
orchestrator may still be alive and progressing). FR-013: re-attach to the recorded
process — but a bare pid check is unsafe because the OS can RECYCLE a pid to an
unrelated process. So the decision is disambiguated by the process **start-time**:

* pid dead                          -> ORPHAN (dead) — hand to Reconcile to reap;
* pid alive + start-time matches    -> RE-ATTACH — it is genuinely our Run, keep tracking;
* pid alive + start-time MISMATCHES -> ORPHAN (pid reused) — a different process now
  owns that pid; the original Run is gone, hand to Reconcile to reap;
* pid alive + start-time unknown    -> RE-ATTACH (conservative — never reap a live pid
  we cannot positively disprove is ours).

Pure + injectable: ``pid_alive``, ``pid_start_time`` (the live OS probe), and
``recorded_start_time_of`` (read off the run row) are all supplied, so the decision
logic is deterministic and unit-tested without a real process. Confirming a re-attach
against an actually-restarted orchestrator is LIVE-ONLY.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from supervisor.ports import RegistryRow

DECISION_REATTACH = "reattach"
DECISION_ORPHAN_DEAD = "orphan_pid_dead"
DECISION_ORPHAN_REUSED = "orphan_pid_reused"


@dataclass(frozen=True)
class ReattachDecision:
    """The FR-013 verdict for one recorded running Run."""

    project_id: str
    orchestrator_pid: int
    decision: str  # one of DECISION_REATTACH / DECISION_ORPHAN_DEAD / DECISION_ORPHAN_REUSED

    @property
    def is_reattach(self) -> bool:
        return self.decision == DECISION_REATTACH

    @property
    def is_orphan(self) -> bool:
        return self.decision in (DECISION_ORPHAN_DEAD, DECISION_ORPHAN_REUSED)


def _default_recorded_start_time(row: RegistryRow) -> str | None:
    """Default recorded start-time accessor: the persisted OS process start-time.

    Production records the orchestrator's OS start-time at spawn (run-row
    ``orchestrator_start_time``); absent it, no disambiguation is possible and the
    decision falls through to the conservative RE-ATTACH branch.
    """
    value = row.get("orchestrator_start_time")
    return str(value) if isinstance(value, str) and value else None


def derive_reattach_decisions(
    active_runs: Sequence[RegistryRow],
    *,
    pid_alive: Callable[[int], bool],
    pid_start_time: Callable[[int], str | None],
    recorded_start_time_of: Callable[[RegistryRow], str | None] = _default_recorded_start_time,
) -> list[ReattachDecision]:
    """Classify each recorded running Run as RE-ATTACH or ORPHAN (FR-013).

    Rows without an integer ``orchestrator_pid`` are skipped (no pid to re-attach to —
    the Reconcile stall path covers a never-pidded Run). Never raises.
    """
    decisions: list[ReattachDecision] = []
    for row in active_runs:
        project_id = row.get("project_id")
        pid = row.get("orchestrator_pid")
        if not isinstance(project_id, str) or not project_id or not isinstance(pid, int):
            continue

        if not pid_alive(pid):
            verdict = DECISION_ORPHAN_DEAD
        else:
            recorded = recorded_start_time_of(row)
            live = pid_start_time(pid)
            if recorded is None or live is None:
                verdict = DECISION_REATTACH  # cannot disambiguate → never reap a live pid
            elif recorded == live:
                verdict = DECISION_REATTACH
            else:
                verdict = DECISION_ORPHAN_REUSED
        decisions.append(
            ReattachDecision(project_id=project_id, orchestrator_pid=pid, decision=verdict)
        )
    return decisions


__all__ = [
    "ReattachDecision",
    "derive_reattach_decisions",
    "DECISION_REATTACH",
    "DECISION_ORPHAN_DEAD",
    "DECISION_ORPHAN_REUSED",
]
