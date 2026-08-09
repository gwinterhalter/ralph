"""Run-lifecycle teardown for the Outer Loop Supervisor (OLB-08 / §14).

The §14 teardown reconcile call-site OLB-08a deferred to OLB-08: after a spawned
orchestrator Run terminates, read its terminal outcome (the §13.1
INITIATIVE_COMPLETE signal + the summed run cost) and reconcile the Run Registry
row to ``complete`` carrying ``terminated_at`` (Spec v1.3 §5.4 FR-011) +
``terminal_cost_usd`` (FR-014, exact ``Decimal`` per NFR-007), then move the
Project to the ``complete`` lifecycle state (FR-008-legal ``running -> complete``).

This is SMALL wiring over the existing OLB-02/08a RegistryPort methods
(:meth:`~supervisor.ports.RegistryPort.reconcile_run` +
:meth:`~supervisor.ports.RegistryPort.set_lifecycle_state`); it adds NO new
reconcile logic to the closed seams and writes nothing of its own.
"""
from __future__ import annotations

import json

# subprocess here only waits on a caller-supplied Popen handle (type + .wait());
# it spawns no process of its own.
import subprocess  # nosec B404
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from supervisor.ports import RegistryPort

#: The §13.1 orchestrator terminal token printed on a clean INITIATIVE_COMPLETE.
INITIATIVE_COMPLETE_SIGNAL = "INITIATIVE_COMPLETE"
#: The terminal Run status the §14 teardown reconciles a completed Run to (§5.4).
RUN_STATUS_COMPLETE = "complete"
#: The terminal Project lifecycle state (§5.3 — running -> complete is FR-008-legal).
LIFECYCLE_COMPLETE = "complete"
#: The orchestrator running-spend ledger (orchestrator.sh RUNNING_SPEND_FILE).
SPEND_FILE_NAME = "spend.json"
#: The orchestrator append-only log carrying the INITIATIVE_COMPLETE line (§13.1).
ORCHESTRATOR_LOG_REL: tuple[str, ...] = ("logs", "orchestrator.log")
#: The persisted money grain for the run cost — the canonical
#: ``ralph_runs.terminal_cost_usd numeric(10,4)`` scale (Ralph_Runs_Table_Migration v1.0,
#: a closed table read but never reshaped per Spec v1.3 §4.2). The summed ledger cost is
#: quantized to this grain so the in-memory terminal cost equals the value
#: ``reconcile_run`` persists and reads back — NFR-007 exact ``Decimal`` AT the storage
#: scale, with no float and no round-trip drift. Postgres rounds a numeric cast half away
#: from zero, so ROUND_HALF_UP matches the DB.
TERMINAL_COST_SCALE = Decimal("0.0001")


def _utc_now_iso() -> str:
    """The default ``terminated_at`` clock — an ISO-8601 UTC timestamp."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RunTerminal:
    """The observed terminal outcome of a spawned orchestrator Run (§13.1 / §14).

    ``completed`` is True iff the orchestrator exited 0 AND emitted the §13.1
    INITIATIVE_COMPLETE signal. ``terminal_cost_usd`` is the summed run cost read
    from ``spend.json`` as an exact ``Decimal`` (NFR-007). Truthy iff completed, so
    callers may write ``if terminal:``.
    """

    completed: bool
    exit_code: int | None
    terminated_at: str
    terminal_cost_usd: Decimal
    detail: str = ""

    def __bool__(self) -> bool:
        return self.completed


def wait_for_orchestrator(
    process: subprocess.Popen[bytes], *, timeout_s: float
) -> int | None:
    """Block until the spawned orchestrator exits; return its exit code, or ``None``
    on timeout (the caller decides whether to kill + treat as incomplete)."""
    try:
        return process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None


def read_terminal_cost(state_dir: Path) -> Decimal:
    """The summed run cost from ``spend.json`` as an exact ``Decimal`` (FR-014 /
    NFR-007); ``Decimal('0')`` when the ledger is absent.

    Parsed with ``parse_float=Decimal`` so the money value is taken from the JSON
    literal with no intermediate float rounding, then quantized to the persisted
    ``numeric(10,4)`` money grain (``TERMINAL_COST_SCALE``) so the returned cost equals
    what ``reconcile_run`` stores + reads back (NFR-007; no round-trip drift).
    """
    spend_file = state_dir / SPEND_FILE_NAME
    if not spend_file.is_file():
        return Decimal(0).quantize(TERMINAL_COST_SCALE)
    data = json.loads(spend_file.read_text(encoding="utf-8"), parse_float=Decimal)
    raw = data.get("total_spend_usd", 0)
    cost = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    return cost.quantize(TERMINAL_COST_SCALE, rounding=ROUND_HALF_UP)


def detect_initiative_complete(
    state_dir: Path, stdout_path: Path | None = None
) -> bool:
    """True iff the §13.1 INITIATIVE_COMPLETE signal appears in the orchestrator log
    or the captured spawn stdout."""
    log_path = state_dir.joinpath(*ORCHESTRATOR_LOG_REL)
    for candidate in (log_path, stdout_path):
        if candidate is not None and candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if INITIATIVE_COMPLETE_SIGNAL in text:
                return True
    return False


def resolve_run_terminal(
    process: subprocess.Popen[bytes],
    state_dir: Path,
    *,
    timeout_s: float,
    stdout_path: Path | None = None,
    clock: Callable[[], str] = _utc_now_iso,
) -> RunTerminal:
    """Wait for the spawned orchestrator and classify its terminal outcome.

    On timeout the process is killed and an incomplete :class:`RunTerminal` is
    returned. Otherwise ``completed`` requires exit 0 AND the §13.1
    INITIATIVE_COMPLETE signal; the summed cost is read regardless so a failed run's
    spend is still observable.
    """
    exit_code = wait_for_orchestrator(process, timeout_s=timeout_s)
    terminated_at = clock()
    cost = read_terminal_cost(state_dir)
    if exit_code is None:
        process.kill()
        return RunTerminal(
            completed=False,
            exit_code=None,
            terminated_at=terminated_at,
            terminal_cost_usd=cost,
            detail=f"orchestrator did not terminate within {timeout_s}s (killed)",
        )
    completed = exit_code == 0 and detect_initiative_complete(state_dir, stdout_path)
    detail = (
        ""
        if completed
        else f"orchestrator exited {exit_code} without INITIATIVE_COMPLETE"
    )
    return RunTerminal(
        completed=completed,
        exit_code=exit_code,
        terminated_at=terminated_at,
        terminal_cost_usd=cost,
        detail=detail,
    )


def reconcile_run_complete(
    registry_port: RegistryPort,
    project_id: str,
    terminal: RunTerminal,
) -> None:
    """The §14 teardown reconcile call-site (the live ``reconcile_run`` site OLB-08a
    deferred to OLB-08).

    Reconciles the active Run row to ``complete`` (FR-011 ``terminated_at`` + FR-014
    exact-``Decimal`` ``terminal_cost_usd``) then moves the Project to the
    ``complete`` lifecycle state (FR-008-legal ``running -> complete``). Calls ONLY
    the existing OLB-02/08a RegistryPort methods; adds no new reconcile logic.

    Refuses to reconcile a run that did not reach INITIATIVE_COMPLETE — the caller
    handles a non-completed run (the FR-021 spawn-failure path already reconciled a
    failed spawn; an incomplete drain is surfaced, never silently marked complete).
    """
    if not terminal.completed:
        raise ValueError(
            f"refusing to reconcile an incomplete run to 'complete': {terminal.detail}"
        )
    registry_port.reconcile_run(
        project_id,
        RUN_STATUS_COMPLETE,
        terminated_at=terminal.terminated_at,
        terminal_cost_usd=terminal.terminal_cost_usd,
    )
    registry_port.set_lifecycle_state(project_id, LIFECYCLE_COMPLETE)


__all__ = [
    "INITIATIVE_COMPLETE_SIGNAL",
    "LIFECYCLE_COMPLETE",
    "ORCHESTRATOR_LOG_REL",
    "RUN_STATUS_COMPLETE",
    "SPEND_FILE_NAME",
    "TERMINAL_COST_SCALE",
    "RunTerminal",
    "detect_initiative_complete",
    "read_terminal_cost",
    "reconcile_run_complete",
    "resolve_run_terminal",
    "wait_for_orchestrator",
]
