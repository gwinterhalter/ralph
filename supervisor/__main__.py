"""Production entry point — assemble + run the full §4.4 supervision cycle (D6).

The component logic and the four wired cycle steps were complete, but there was no
runnable supervisor: nothing constructed a :class:`SupervisionCycle` with all of the
Reconcile / Schedule / Attend / Guard / Learn configs against live substrates and ran
it on a loop. This module is that assembly.

``build_production_cycle`` is the testable wiring — it composes every config (threading
the D2 live open-work-count source, the D4 heartbeat-pointer progress source, the
FUP-0855 seed candidate-enricher, and the D3 reconcile→Guard stall bridge) and returns
a ready cycle; it is unit-tested with fakes over a zero-row registry. ``main`` is the
thin live front-end (``python -m supervisor``): it preflights the schema, runs the
FR-013 re-attach pass, then drives ``run_once`` on the bounded cadence. The live DB +
real spawn make ``main`` itself the only un-unit-tested (live-only) part.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from supervisor.candidate_enrichment import make_seed_candidate_enricher, open_work_counts_for
from supervisor.cycle import SupervisionCycle
from supervisor.cycle_wiring import (
    AttendConfig,
    GuardConfig,
    LearnConfig,
    ReconcileConfig,
    ScheduleConfig,
    stall_signals_from_actions,
)
from supervisor.heartbeats import read_heartbeats_from_log
from supervisor.ports import RegistryPort, RegistryRow
from supervisor.reattach import derive_reattach_decisions
from supervisor.reconcile import derive_reconcile_actions

#: Default stall budget (seconds) for the Reconcile + Guard stall detection.
DEFAULT_HANG_TIMEOUT_SECONDS = 1800.0


def _pid_alive(pid: int) -> bool:
    """OS pid-liveness probe (ProcessLookupError = dead; PermissionError = alive)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def build_production_cycle(
    registry: RegistryPort,
    *,
    seed_validator: object,
    spawn_port: object,
    active_runs_source: Callable[[], Sequence[RegistryRow]],
    workspace_root: str | os.PathLike[str] | None = None,
    events_log_path: str | os.PathLike[str] | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    hang_timeout_seconds: float = DEFAULT_HANG_TIMEOUT_SECONDS,
) -> SupervisionCycle:
    """Assemble a :class:`SupervisionCycle` wired with all five §4.4 step configs.

    Threads the live sources built in D1–D5: the heartbeat-pointer progress source
    (D4) into both Reconcile's ``progress_at`` and the stall bridge; the open-work-count
    source (D2) into the scheduler's FR-024 bias; the seed candidate-enricher (FUP-0855);
    and the reconcile→Guard stall bridge (D3). ``seed_validator`` / ``spawn_port`` are
    typed ``object`` here so a fake satisfies them in tests; production passes the real
    ``SeedReviewValidator`` / ``OrchestratorSpawnPort`` (which satisfy the
    ``SeedValidatorPort`` / ``SpawnPort`` Protocols structurally).
    """
    heartbeats = (
        read_heartbeats_from_log(os.fspath(events_log_path))
        if events_log_path is not None
        else {}
    )

    def _progress_at(row: RegistryRow) -> str | None:
        project_id = row.get("project_id")
        if isinstance(project_id, str):
            hb = heartbeats.get(project_id)
            if hb is not None:
                return hb.isoformat()
        spawned = row.get("spawned_at")  # coarse fallback (D4 docstring)
        return str(spawned) if isinstance(spawned, str) and spawned else None

    reconcile_config = ReconcileConfig(
        active_runs_source=active_runs_source,
        pid_alive=pid_alive,
        hang_timeout_seconds=hang_timeout_seconds,
        progress_at=_progress_at,
        clock=now,
    )

    def _stall_signals() -> dict[str, object]:
        actions = derive_reconcile_actions(
            list(active_runs_source()),
            pid_alive=pid_alive,
            now=now(),
            hang_timeout_seconds=hang_timeout_seconds,
            progress_at=_progress_at,
        )
        return dict(stall_signals_from_actions(actions))

    schedule_config = ScheduleConfig(
        seed_validator=seed_validator,  # type: ignore[arg-type]
        spawn_port=spawn_port,  # type: ignore[arg-type]
        open_work_counts=open_work_counts_for(
            list(registry.read_running()), workspace_root=workspace_root
        ),
        candidate_enricher=make_seed_candidate_enricher(workspace_root),
    )

    return SupervisionCycle(
        registry,
        reconcile_config=reconcile_config,
        schedule_config=schedule_config,
        attend_config=AttendConfig(),
        guard_config=GuardConfig(stall_signals=_stall_signals),  # type: ignore[arg-type]
        learn_config=LearnConfig(runs_source=lambda: []),  # live completed-Run corpus reader: deferred
    )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - live DB + real ports
    import argparse
    import time

    from supervisor.preflight import run_preflight
    from supervisor.reconcile import ReconcileAction  # noqa: F401  (documents the reaper handoff)

    parser = argparse.ArgumentParser(prog="supervisor", description="Run the Outer Loop Supervisor.")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between cycles")
    parser.add_argument("--max-cycles", type=int, default=None, help="stop after N cycles")
    parser.add_argument("--skip-preflight", action="store_true", help="skip the schema gate")
    args = parser.parse_args(argv)

    dsn = os.environ.get("OL_SUPERVISOR_DB_URL")
    if not dsn:
        print("supervisor: OL_SUPERVISOR_DB_URL is not set — cannot reach the registry.")
        return 1
    try:
        import psycopg

        from supervisor.registry import DBConnection, Registry
        from supervisor.seed_validation import SeedReviewValidator
        from supervisor.spawn import OrchestratorSpawnPort
    except ImportError as exc:
        print(f"supervisor: missing a runtime dependency ({exc}).")
        return 1

    from typing import cast

    conn = cast("DBConnection", psycopg.connect(dsn))
    registry = Registry(conn)

    if not args.skip_preflight:
        result = run_preflight(conn)
        if not result.ok:
            print("supervisor: preflight FAILED — substrate drift:")
            for failure in result.failures:
                print(f"  - {failure}")
            return 1

    # FR-013 re-attach pass: orphans (dead / pid-reused) are handed to the Reconcile
    # reaper on the first cycle; re-attached Runs are simply left running.
    decisions = derive_reattach_decisions(
        list(registry.read_active_runs()),
        pid_alive=_pid_alive,
        pid_start_time=lambda _pid: None,  # OS start-time probe: inject psutil in deployment
    )
    reattached = sum(1 for d in decisions if d.is_reattach)
    orphaned = sum(1 for d in decisions if d.is_orphan)
    print(f"supervisor: re-attach pass — {reattached} re-attached, {orphaned} orphaned.")

    workspace_root = os.environ.get("OL_SUPERVISOR_WORKSPACE_ROOT")
    state_dir = os.environ.get("OL_SUPERVISOR_STATE_DIR", ".")
    events_log = os.path.join(state_dir, "logs", "events.jsonl")
    from pathlib import Path

    seed_validator = SeedReviewValidator()
    spawn_port = OrchestratorSpawnPort(
        Path(os.environ.get("OL_SUPERVISOR_ORCHESTRATOR", "orchestrator.sh"))
    )

    cycles = 0
    while args.max_cycles is None or cycles < args.max_cycles:
        cycle = build_production_cycle(
            registry,
            seed_validator=seed_validator,
            spawn_port=spawn_port,
            active_runs_source=registry.read_active_runs,
            workspace_root=workspace_root,
            events_log_path=events_log,
        )
        cycle.run_once()
        cycles += 1
        if args.once or (args.max_cycles is not None and cycles >= args.max_cycles):
            break
        time.sleep(args.interval)
    print(f"supervisor: ran {cycles} cycle(s).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["build_production_cycle", "main", "DEFAULT_HANG_TIMEOUT_SECONDS"]
