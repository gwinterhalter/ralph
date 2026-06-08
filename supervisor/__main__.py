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

from supervisor.candidate_enrichment import (
    make_seed_candidate_enricher,
    open_work_counts_for,
    seed_hang_timeout_seconds,
)
from supervisor.cycle import SupervisionCycle
from supervisor.cycle_wiring import (
    AttendConfig,
    AttentionStateStore,
    GuardConfig,
    LearnConfig,
    ReconcileConfig,
    ScheduleConfig,
    stall_signals_from_actions,
)
from supervisor.notifications import (
    NotificationPort,
    NullNotificationPort,
    build_notification_port,
)
from supervisor.heartbeats import read_heartbeats_from_log
from supervisor.pid_probe import format_pid_start_time, pid_alive, probe_pid_start_time
from supervisor.ports import RegistryPort, RegistryRow
from supervisor.run_auditor import RunAuditReport
from supervisor.run_auditor import RunRecord as AuditRunRecord
from supervisor.safety_gates import KillSwitch
from supervisor.reattach import derive_reattach_decisions
from supervisor.reconcile import RunCompletion, derive_reconcile_actions

#: Default stall budget (seconds) for the Reconcile + Guard stall detection.
DEFAULT_HANG_TIMEOUT_SECONDS = 1800.0


def _no_completed_runs() -> list[AuditRunRecord]:
    """The §4.4(6) Learn no-op source — no completed Runs (the OLB-01 default)."""
    return []


# The OS pid-liveness probe lives in the shared `pid_probe` module (read-only on every
# platform — critically, it does NOT use os.kill on Windows, where that would terminate
# the pid). `_pid_alive` keeps its private name as the probe wired into the Reconcile +
# re-attach passes; `cycle_wiring` consumes the same single source.
_pid_alive = pid_alive


# The FR-013 start-time formatter + live probe live in the shared `pid_probe` module
# so the recorder (`supervisor.spawn`, at spawn) and this live probe (the re-attach
# pass) produce the identical format — the recorded/live comparison cannot drift.
# `_pid_start_time` keeps its private name as the production probe wired into
# `derive_reattach_decisions`. Both the *recorded* (spawn-time persistence, FR-013) and
# *live* halves are now real, so a recycled pid is correctly disambiguated.
_pid_start_time = probe_pid_start_time


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
    completion_probe: Callable[[RegistryRow], "RunCompletion | None"] = lambda _row: None,
    admitted_source: Callable[[], Sequence[RegistryRow]] = lambda: [],
    record_start_time: Callable[[str, str], None] | None = None,
    kill_switch: KillSwitch | None = None,
    completed_project_ids: Callable[[], frozenset[str]] = lambda: frozenset(),
    learn_runs_source: Callable[[], Sequence[AuditRunRecord]] | None = None,
    learn_report_sink: Callable[[RunAuditReport], None] | None = None,
    attention_store: AttentionStateStore | None = None,
    notification_port: NotificationPort | None = None,
    delivered_keys: "set[tuple[str, str, str]] | None" = None,
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

    def _hang_timeout_of(row: RegistryRow) -> "float | None":
        """Per-run stall budget (F-4): the run's seed ``budget.hang_timeout_seconds``
        off its recorded ``seed_path``, else ``None`` → the fleet default applies."""
        seed = row.get("seed_path")
        if isinstance(seed, str) and seed:
            return seed_hang_timeout_seconds(seed)
        return None

    reconcile_config = ReconcileConfig(
        active_runs_source=active_runs_source,
        pid_alive=pid_alive,
        hang_timeout_seconds=hang_timeout_seconds,
        progress_at=_progress_at,
        clock=now,
        completion_of=completion_probe,
        hang_timeout_of=_hang_timeout_of,
    )

    def _stall_signals() -> dict[str, object]:
        actions = derive_reconcile_actions(
            list(active_runs_source()),
            pid_alive=pid_alive,
            now=now(),
            hang_timeout_seconds=hang_timeout_seconds,
            progress_at=_progress_at,
            hang_timeout_of=_hang_timeout_of,
        )
        return dict(stall_signals_from_actions(actions))

    schedule_config = ScheduleConfig(
        seed_validator=seed_validator,  # type: ignore[arg-type]
        spawn_port=spawn_port,  # type: ignore[arg-type]
        open_work_counts=open_work_counts_for(
            list(registry.read_running()), workspace_root=workspace_root
        ),
        candidate_enricher=make_seed_candidate_enricher(workspace_root),
        admitted_source=admitted_source,
        record_start_time=record_start_time,
        # FR-036: an engaged Kill-Switch makes admission refuse ALL new dispatch this
        # cycle (running Runs untouched). Default disengaged → normal scheduling.
        kill_switch=kill_switch if kill_switch is not None else KillSwitch(),
        # Item 1 cross-initiative dependency gating: the live `complete` project set a
        # Candidate's depends_on is checked against (production wires
        # registry.read_completed_project_ids). Default empty → nothing is blocked.
        completed_project_ids=completed_project_ids,
    )

    # §4.4(6) Learn: wire the live completed-Run source + report sink when supplied (Item 2);
    # otherwise the OLB-01 no-op (an empty source short-circuits run_learn_step).
    if learn_runs_source is None:
        learn_config = LearnConfig(runs_source=_no_completed_runs)
    elif learn_report_sink is None:
        learn_config = LearnConfig(runs_source=learn_runs_source)
    else:
        learn_config = LearnConfig(
            runs_source=learn_runs_source, report_sink=learn_report_sink
        )

    # Item 3: share ONE attention store across Attend + Guard so escalations the Guard raises
    # (FR-038 safety trips, breaker trips, repair escalations) reach the Attend step's plan and
    # get delivered. Attend runs before Guard, so a Guard-raised escalation is delivered next
    # cycle (acceptable one-cycle latency). Default no-op port → delivery is off unless wired.
    store = attention_store if attention_store is not None else AttentionStateStore()
    attend_config = AttendConfig(
        attention_store=store,
        notification_port=(
            notification_port if notification_port is not None else NullNotificationPort()
        ),
        delivered_keys=delivered_keys,
    )

    return SupervisionCycle(
        registry,
        reconcile_config=reconcile_config,
        schedule_config=schedule_config,
        attend_config=attend_config,
        guard_config=GuardConfig(
            stall_signals=_stall_signals,  # type: ignore[arg-type]
            attention_store=store,
        ),
        learn_config=learn_config,
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
        pid_start_time=_pid_start_time,  # FR-013 live OS start-time probe (psutil)
    )
    reattached = sum(1 for d in decisions if d.is_reattach)
    orphaned = sum(1 for d in decisions if d.is_orphan)
    print(f"supervisor: re-attach pass — {reattached} re-attached, {orphaned} orphaned.")

    workspace_root = os.environ.get("OL_SUPERVISOR_WORKSPACE_ROOT")
    state_dir = os.environ.get("OL_SUPERVISOR_STATE_DIR", ".")
    events_log = os.path.join(state_dir, "logs", "events.jsonl")
    from pathlib import Path

    # Operator drill knobs (all OFF/neutral by default — absent => unchanged behaviour):
    #  * hang-timeout override for the stall drill;
    #  * a KILL_SWITCH sentinel file in the state dir (FR-036 — refuse new dispatch);
    #  * an opt-in emergency cumulative-spend ceiling (T3#6 backstop — kill on breach).
    from decimal import Decimal

    from supervisor.spend_backstop import EmergencySpendConfig, evaluate_spend_backstop

    hang_timeout = float(
        os.environ.get("OL_SUPERVISOR_HANG_TIMEOUT_SECONDS") or DEFAULT_HANG_TIMEOUT_SECONDS
    )
    kill_switch_sentinel = Path(state_dir) / "KILL_SWITCH"
    _ceiling = os.environ.get("OL_SUPERVISOR_EMERGENCY_SPEND_CEILING_USD")
    spend_config = (
        EmergencySpendConfig(ceiling_usd=Decimal(_ceiling))
        if _ceiling
        else EmergencySpendConfig()
    )

    seed_validator = SeedReviewValidator()
    spawn_port = OrchestratorSpawnPort(
        Path(os.environ.get("OL_SUPERVISOR_ORCHESTRATOR", "orchestrator.sh"))
    )

    from supervisor.run_lifecycle import detect_initiative_complete, read_terminal_cost

    def _completion_of(row: RegistryRow) -> "RunCompletion | None":
        """Terminal-completion probe: a run whose orchestrator emitted §13.1
        INITIATIVE_COMPLETE is reconciled ``complete`` (not mis-reaped ``failed`` on
        pid-death). Reads the run's state dir (the seed's sibling ``state/`` per the
        seed ``state_dir_relative`` convention) for the signal + the spend ledger."""
        seed = row.get("seed_path")
        if not isinstance(seed, str) or not seed:
            return None
        state_dir = Path(seed).parent / "state"
        if not detect_initiative_complete(state_dir):
            return None
        return RunCompletion(
            terminated_at=datetime.now(timezone.utc).isoformat(),
            terminal_cost_usd=read_terminal_cost(state_dir),
        )

    # §4.4(6) Learn wiring (Item 2): the live completed-Run source + report/corpus sinks.
    from supervisor.attention import ESCALATION_KIND_ROUTINE, Escalation, intake_escalation
    from supervisor.cost_forecast import forecast_breaches, forecast_fleet
    from supervisor.event_ingest import events_file_for
    from supervisor.learn_assembly import (
        build_correction_attempts,
        completed_run_records,
        findings_to_escalations,
        learning_records,
        read_events_jsonl,
        render_learning_corpus,
        run_facts_from_run,
        scoped_events_for_run,
    )
    from supervisor.run_auditor import (
        AuditConfig,
        CorrectionRecord,
        derive_correction_findings,
        render_audit_report,
    )

    _correction_records: list[CorrectionRecord] = []

    def _ingest_fleet_events() -> None:
        """Ship every project's events.jsonl into the DB `events` table (Fleet Analytics §1).

        Best-effort, idempotent (upsert by event_uuid) — never aborts the cycle. The outer loop is
        the log-shipper the bash hooks could not be (it holds the psycopg connection)."""
        try:
            projects = list(registry.read_all_projects())
        except Exception as exc:  # noqa: BLE001 - best-effort
            print(f"supervisor: event ingest skipped ({exc}).")
            return
        total_new = 0
        for project in projects:
            path = events_file_for(project, workspace_root=workspace_root)
            if path is None:
                continue
            events = read_events_jsonl(path)
            if not events:
                continue
            try:
                total_new += registry.upsert_events(events)
            except Exception as exc:  # noqa: BLE001 - best-effort per project
                print(f"supervisor: event ingest for {project.get('project_id')} skipped ({exc}).")
        if total_new:
            print(f"supervisor: ingested {total_new} new event(s) to the fleet events table.")

    _forecast_warned = {"breaching": False}

    def _forecast_guard() -> None:
        """Warn-only cost-forecast guard (Fleet Analytics §2): raise ONE operator escalation when
        the fleet PROJECTED total first breaches OL_SUPERVISOR_FORECAST_CEILING_USD (the hard
        auto-pause stays the existing emergency spend backstop). Default off (no ceiling env)."""
        ceiling_raw = os.environ.get("OL_SUPERVISOR_FORECAST_CEILING_USD")
        if not ceiling_raw:
            return
        ceiling = Decimal(ceiling_raw)
        try:
            projects = list(registry.read_all_projects())
            open_counts = open_work_counts_for(projects, workspace_root=workspace_root)
            forecast = forecast_fleet(registry.read_learning_records(), open_counts)
        except Exception as exc:  # noqa: BLE001 - best-effort; never abort the cycle
            print(f"supervisor: forecast guard skipped ({exc}).")
            return
        breaching = forecast_breaches(forecast, ceiling)
        if breaching and not _forecast_warned["breaching"]:
            escalation = Escalation(
                project_id="*fleet*",
                gate_id="forecast:over-ceiling",
                kind=ESCALATION_KIND_ROUTINE,
                reversible=True,
                suggested_option=(
                    f"projected fleet total ${forecast.fleet_projected_total_usd} exceeds ceiling "
                    f"${ceiling} — review budget / pause low-value projects"
                ),
                confidence=0.9,
                raised_at=datetime.now(timezone.utc),
            )
            state = attention_store.load()
            attention_store.save(intake_escalation(state, escalation))
            print(f"supervisor: FORECAST WARNING — projected ${forecast.fleet_projected_total_usd} > ${ceiling}.")
        _forecast_warned["breaching"] = breaching

    logs_dir = Path(state_dir) / "logs"

    def _learn_runs_source() -> list[AuditRunRecord]:
        """Read the live terminal Runs, persist the cost/duration learning corpus (file +
        DB, best-effort), and hand the Run-Auditor its records."""
        rows = list(registry.read_completed_runs())
        records = learning_records(rows)
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            (logs_dir / "learning_corpus.jsonl").write_text(
                render_learning_corpus(records), encoding="utf-8"
            )
        except OSError:
            pass
        try:
            registry.upsert_learning_records(records)  # Item 2 DB capture (ol3)
        except Exception as exc:  # noqa: BLE001 - capture is best-effort; never abort the cycle
            print(f"supervisor: learning_records DB capture skipped ({exc}).")
        # Corrections (ol4): capture the L1-L4 correction-loop attempts from each Run's event
        # stream + stash the per-item records for the correction_pattern finding deriver.
        corr_attempts = []
        _correction_records.clear()
        for row in rows:
            for attempt in build_correction_attempts(scoped_events_for_run(row)):
                corr_attempts.append(attempt)
                _correction_records.append(
                    CorrectionRecord(
                        run_id=str(row.get("run_id") or ""),
                        project_slug=str(row.get("project_id") or row.get("project_slug") or ""),
                        item_id=attempt.item_id,
                        level=attempt.level,
                        attempt=attempt.attempt or 0,
                    )
                )
        try:
            registry.upsert_correction_attempts(corr_attempts)  # ol4
        except Exception as exc:  # noqa: BLE001 - best-effort; never abort the cycle
            print(f"supervisor: correction_attempts DB capture skipped ({exc}).")
        return completed_run_records(rows, facts_for=run_facts_from_run)

    def _learn_report_sink(report: RunAuditReport) -> None:
        """Persist the findings-only Run-Auditor report (file + DB capture), then surface any NEW
        findings to the operator via the attention queue (auto-feedback). FR-053 — the auditor
        itself writes nothing; this persists its OWN findings + raises operator offers."""
        # Merge the gate/binding/shape findings (report) with the correction_pattern findings
        # (ol4) into one combined report — persisted to the file + DB + escalated together.
        correction_findings = derive_correction_findings(
            _correction_records, config=AuditConfig()
        )
        all_findings = list(report.findings) + correction_findings
        combined = RunAuditReport(
            findings=tuple(all_findings),
            runs_audited=report.runs_audited,
            min_consistent_runs=report.min_consistent_runs,
            shape_revision_fraction=report.shape_revision_fraction,
        )
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            (logs_dir / "run_auditor_report.md").write_text(
                render_audit_report(combined), encoding="utf-8"
            )
        except OSError:
            pass
        new_keys: list[str] = []
        try:
            new_keys = registry.upsert_audit_findings(
                all_findings, runs_audited=report.runs_audited
            )  # Item 2 DB capture (ol3/ol4); returns the finding_keys not seen before
        except Exception as exc:  # noqa: BLE001 - capture is best-effort; never abort the cycle
            print(f"supervisor: run_audit_findings DB capture skipped ({exc}).")
        # Auto-feedback: surface only the NEW findings as one-confirm operator offers (deduped by
        # the DB table → each learning is raised exactly once), intaken into the SHARED attention
        # store so the next Attend pass delivers them via the notification port (Item 3).
        if new_keys:
            escalations = findings_to_escalations(
                all_findings, new_keys=set(new_keys), now=datetime.now(timezone.utc)
            )
            state = attention_store.load()
            for escalation in escalations:
                state = intake_escalation(state, escalation)
            attention_store.save(state)
            print(f"supervisor: Learn pass — raised {len(escalations)} new learning offer(s).")
        print(
            f"supervisor: Learn pass — {len(all_findings)} finding(s) over "
            f"{report.runs_audited} completed Run(s)."
        )

    # Item 3 notification dispatch: build the attention store, the notification port, and the
    # dedup ledger ONCE here so they persist across cycles (build_production_cycle is re-invoked
    # every cycle). The port is SMTP when OL_SUPERVISOR_SMTP_* is configured, else a no-op — so
    # delivery is safe/off by default. The shared store carries Guard-raised escalations into the
    # next cycle's Attend plan; the ledger pages each unresolved escalation once.
    attention_store = AttentionStateStore()
    notification_port = build_notification_port()
    delivered_keys: set[tuple[str, str, str]] = set()
    if not isinstance(notification_port, NullNotificationPort):
        print("supervisor: notification delivery ENABLED (OL_SUPERVISOR_SMTP_* configured).")

    cycles = 0
    while args.max_cycles is None or cycles < args.max_cycles:
        # Re-read the kill-switch each cycle: the operator can engage/disengage between
        # cycles by creating/removing the sentinel; the spend backstop can also engage it.
        kill_switch = KillSwitch(engaged=kill_switch_sentinel.exists())
        if spend_config.ceiling_usd is not None:
            escalation = evaluate_spend_backstop(
                registry.read_cumulative_spend_usd(), spend_config, project_id="*fleet*"
            )
            if escalation is not None:
                kill_switch.engage()
                print(
                    f"supervisor: SPEND BACKSTOP — {escalation.reason} "
                    f"(killed={escalation.killed}); engaging kill-switch."
                )
        if kill_switch.engaged:
            print("supervisor: KILL-SWITCH ENGAGED — refusing new dispatch this cycle.")
        cycle = build_production_cycle(
            registry,
            seed_validator=seed_validator,
            spawn_port=spawn_port,
            active_runs_source=registry.read_active_runs,
            workspace_root=workspace_root,
            events_log_path=events_log,
            hang_timeout_seconds=hang_timeout,
            completion_probe=_completion_of,
            admitted_source=registry.read_admitted,
            record_start_time=registry.record_pid_start_time,
            kill_switch=kill_switch,
            completed_project_ids=registry.read_completed_project_ids,
            learn_runs_source=_learn_runs_source,
            learn_report_sink=_learn_report_sink,
            attention_store=attention_store,
            notification_port=notification_port,
            delivered_keys=delivered_keys,
        )
        cycle.run_once()
        _ingest_fleet_events()  # Fleet Analytics §1: ship all projects' events.jsonl to the DB
        _forecast_guard()  # Fleet Analytics §2: warn-only projected-spend guard
        cycles += 1
        if args.once or (args.max_cycles is not None and cycles >= args.max_cycles):
            break
        time.sleep(args.interval)
    print(f"supervisor: ran {cycles} cycle(s).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "build_production_cycle",
    "main",
    "DEFAULT_HANG_TIMEOUT_SECONDS",
    "format_pid_start_time",  # re-exported from pid_probe (FR-013 live-probe call-site)
]
