"""§4.4(1) Reconcile — mark stalled / terminated running Runs (robustness T1#1).

The cycle's Reconcile step shipped as a no-op stub (``cycle._reconcile``): nothing
detected a running Run whose orchestrator process had died (an *orphan* — the
``ralph_runs`` row stays ``running`` forever) or one that had stalled past the hang
budget with no progress. Because the ``uq_ralph_runs_active_per_project`` partial
unique index gates re-dispatch on ``status = 'running'``, a single un-reaped run
**wedges that Project's concurrency slot permanently**. This module is the pure,
injectable core the Reconcile step composes to detect and terminally reconcile
those runs, releasing the slot.

Legal transitions (Spec v1.3 §5.3, ``supervisor.transitions``) constrain the
outcome: from ``running`` only ``paused_*`` / ``complete`` / ``failed`` are legal
(NOT ``candidate``). So:

* a **dead orchestrator PID** -> Run ``failed`` + Project ``failed`` (a crashed Run
  is terminal; an operator / the attention layer re-opens it — never a silent
  auto-retry that could loop a genuinely broken Project);
* a **stall** (no progress past ``hang_timeout_seconds``) -> Run ``halted`` + Project
  ``paused_gate`` (recoverable: ``paused_gate -> running`` is legal, and the OLB-10
  attention layer surfaces it to the operator).

Both terminal Run statuses leave ``status != 'running'``, so the active-run unique
index releases the slot. The module is pure: the PID-liveness probe, the clock, and
the per-run progress timestamp are all injected, so the live event-stream staleness
source (the FUP-0830 ``phase_complete`` signal) is supplied by the production wiring
without this core reading any substrate.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from supervisor.ports import RegistryRow

#: Run status (``ralph_runs.status``) for a reaped orphan / stall.
RUN_FAILED = "failed"
RUN_HALTED = "halted"

#: Project lifecycle target (``transitions.LEGAL_TRANSITIONS`` from ``running``).
LIFECYCLE_FAILED = "failed"
LIFECYCLE_PAUSED_GATE = "paused_gate"

#: Reason codes (threaded into the action for the operator surface / audit).
REASON_DEAD_PID = "orchestrator_pid_dead"
REASON_STALLED = "stalled_past_hang_timeout"


@dataclass(frozen=True)
class ReconcileAction:
    """One running Run the Reconcile step must terminally reconcile.

    ``run_status`` is the terminal ``ralph_runs.status`` to set (releasing the
    active-run slot); ``lifecycle_state`` is the legal post-``running`` Project state;
    ``reason`` is the audit/operator-surface discriminator.
    """

    project_id: str
    run_status: str
    lifecycle_state: str
    reason: str


def _default_progress_at(row: RegistryRow) -> str | None:
    """Default per-run progress timestamp: the spawn instant.

    The production wiring overrides this with an event-stream-backed lookup (the
    last ``phase_complete`` for the Project) so a long-but-live Run is not mistaken
    for a stall; ``spawned_at`` is the coarse fallback when no event signal exists.
    """
    value = row.get("spawned_at")
    return str(value) if isinstance(value, str) and value else None


def _seconds_since(iso_ts: str, now: datetime) -> float | None:
    """Seconds from ``iso_ts`` to ``now`` (None if ``iso_ts`` is unparseable)."""
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    # Compare tz-aware vs tz-aware; a naive stamp is read in ``now``'s tzinfo.
    if then.tzinfo is None and now.tzinfo is not None:
        then = then.replace(tzinfo=now.tzinfo)
    return (now - then).total_seconds()


def derive_reconcile_actions(
    active_runs: Sequence[RegistryRow],
    *,
    pid_alive: Callable[[int], bool],
    now: datetime,
    hang_timeout_seconds: float,
    progress_at: Callable[[RegistryRow], str | None] = _default_progress_at,
) -> list[ReconcileAction]:
    """Return the terminal reconciliations owed for ``active_runs``.

    A run with a present ``orchestrator_pid`` that ``pid_alive`` reports dead is an
    orphan -> ``failed``. A run still nominally alive (or with no pid recorded) whose
    last progress is older than ``hang_timeout_seconds`` is a stall -> ``halted`` /
    ``paused_gate``. A run that is alive and progressing yields no action. Rows
    missing a ``project_id`` are skipped (nothing to reconcile against).
    """
    actions: list[ReconcileAction] = []
    for row in active_runs:
        project_id = row.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            continue

        pid = row.get("orchestrator_pid")
        if isinstance(pid, int) and not pid_alive(pid):
            actions.append(
                ReconcileAction(
                    project_id=project_id,
                    run_status=RUN_FAILED,
                    lifecycle_state=LIFECYCLE_FAILED,
                    reason=REASON_DEAD_PID,
                )
            )
            continue

        ts = progress_at(row)
        if ts is None:
            continue
        elapsed = _seconds_since(ts, now)
        if elapsed is not None and elapsed > hang_timeout_seconds:
            actions.append(
                ReconcileAction(
                    project_id=project_id,
                    run_status=RUN_HALTED,
                    lifecycle_state=LIFECYCLE_PAUSED_GATE,
                    reason=REASON_STALLED,
                )
            )
    return actions


__all__ = [
    "ReconcileAction",
    "derive_reconcile_actions",
    "RUN_FAILED",
    "RUN_HALTED",
    "LIFECYCLE_FAILED",
    "LIFECYCLE_PAUSED_GATE",
    "REASON_DEAD_PID",
    "REASON_STALLED",
]
