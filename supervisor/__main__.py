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
from datetime import UTC, datetime, timedelta

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
from supervisor.heartbeats import read_heartbeats_from_log
from supervisor.notifications import (
    NotificationPort,
    NullNotificationPort,
    build_notification_port,
)
from supervisor.pid_probe import format_pid_start_time, pid_alive, probe_pid_start_time
from supervisor.ports import RegistryPort, RegistryRow
from supervisor.reattach import derive_reattach_decisions
from supervisor.reconcile import RunCompletion, derive_reconcile_actions
from supervisor.run_auditor import RunAuditReport
from supervisor.run_auditor import RunRecord as AuditRunRecord
from supervisor.run_signals import has_pending_gate, latest_progress_ts
from supervisor.safety_gates import DEFAULT_CONCURRENCY_CEILING, KillSwitch

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
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    hang_timeout_seconds: float = DEFAULT_HANG_TIMEOUT_SECONDS,
    completion_probe: Callable[[RegistryRow], RunCompletion | None] = lambda _row: None,
    admitted_source: Callable[[], Sequence[RegistryRow]] = list,
    record_start_time: Callable[[str, str], None] | None = None,
    record_failure_detail: Callable[[str, str], None] | None = None,
    kill_switch: KillSwitch | None = None,
    completed_project_ids: Callable[[], frozenset[str]] = lambda: frozenset(),
    learn_runs_source: Callable[[], Sequence[AuditRunRecord]] | None = None,
    learn_report_sink: Callable[[RunAuditReport], None] | None = None,
    attention_store: AttentionStateStore | None = None,
    notification_port: NotificationPort | None = None,
    delivered_keys: set[tuple[str, str, str]] | None = None,
    concurrency_ceiling: int = DEFAULT_CONCURRENCY_CEILING,
    max_dispatches_per_cycle: int | None = None,
    dispatch_gate: Callable[[], bool] | None = None,
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
        # Progress-based stall detection: read the run's OWN events.jsonl FRESH each
        # pass (time since last progress), not a one-shot startup snapshot of one
        # global log — the prior wiring froze ``heartbeats`` at build time, so a
        # long-but-live multi-iteration Run was reaped on wall-clock-since-spawn.
        seed = row.get("seed_path")
        fresh = latest_progress_ts(seed if isinstance(seed, str) else None)
        if fresh is not None:
            return fresh
        project_id = row.get("project_id")
        if isinstance(project_id, str):
            hb = heartbeats.get(project_id)  # startup-snapshot fallback
            if hb is not None:
                return hb.isoformat()
        spawned = row.get("spawned_at")  # coarse fallback (D4 docstring)
        return str(spawned) if isinstance(spawned, str) and spawned else None

    def _gate_pending_of(row: RegistryRow) -> bool:
        # True iff the run escalated a needs-review gate awaiting an operator response
        # → reconcile ``paused_gate`` (surfaced + resumable), not ``failed``.
        seed = row.get("seed_path")
        return has_pending_gate(seed if isinstance(seed, str) else None)

    def _hang_timeout_of(row: RegistryRow) -> float | None:
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
        gate_pending_of=_gate_pending_of,
    )

    def _stall_signals() -> dict[str, object]:
        actions = derive_reconcile_actions(
            list(active_runs_source()),
            pid_alive=pid_alive,
            now=now(),
            hang_timeout_seconds=hang_timeout_seconds,
            progress_at=_progress_at,
            hang_timeout_of=_hang_timeout_of,
            gate_pending_of=_gate_pending_of,
        )
        return dict(stall_signals_from_actions(actions))

    schedule_config = ScheduleConfig(
        seed_validator=seed_validator,  # type: ignore[arg-type]
        spawn_port=spawn_port,  # type: ignore[arg-type]
        # Concurrency improvement (2026-06-09): the operator-tunable FR-037 ceiling and
        # the fill-to-ceiling dispatch bound. ``max_dispatches_per_cycle`` defaults to the
        # ceiling so a cold fleet ramps to full concurrency in one cycle (the empirically
        # proven N-way headroom); the live running_count guard keeps it from overshooting.
        concurrency_ceiling=concurrency_ceiling,
        max_dispatches_per_cycle=(
            max_dispatches_per_cycle
            if max_dispatches_per_cycle is not None
            else concurrency_ceiling
        ),
        # Tier-2 usage-window pause hook (concurrency, 2026-06-09): pauses NEW dispatch when
        # the rolling Max session/weekly cap is reached; running Runs untouched. Default
        # always-allow → no pacing unless an operator configures a window ceiling.
        dispatch_gate=(dispatch_gate if dispatch_gate is not None else (lambda: True)),
        open_work_counts=open_work_counts_for(
            list(registry.read_running()), workspace_root=workspace_root
        ),
        candidate_enricher=make_seed_candidate_enricher(workspace_root),
        admitted_source=admitted_source,
        record_start_time=record_start_time,
        record_failure_detail=record_failure_detail,
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
    from supervisor.reconcile import (
        ReconcileAction,  # noqa: F401  (documents the reaper handoff)
    )

    parser = argparse.ArgumentParser(prog="supervisor", description="Run the Outer Loop Supervisor.")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between cycles")
    parser.add_argument("--max-cycles", type=int, default=None, help="stop after N cycles")
    parser.add_argument("--skip-preflight", action="store_true", help="skip the schema gate")
    args = parser.parse_args(argv)

    dsn = os.environ.get("PROD_DB_URL")
    if not dsn:
        print("supervisor: PROD_DB_URL is not set — cannot reach the registry.")
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
    # FUP-0869: OL_SUPERVISOR_WORKSPACE_ROOT is REQUIRED for candidate enrichment (seed_path /
    # writable_paths / open_item_count). Missing -> enrichment silently no-ops -> the §6 admission
    # gate refuses EVERY candidate (no blast-radius derivable) and the fleet never dispatches, with
    # no error. Surface that loudly at startup rather than letting it fail invisibly (2026-06-11 M4G).
    if not workspace_root:
        print(
            "supervisor: WARNING — OL_SUPERVISOR_WORKSPACE_ROOT is not set. Candidate enrichment "
            "will no-op and admission will refuse every candidate (no seed_path / blast-radius "
            "derivable), so nothing dispatches. Set it to the Sub_Projects root before launch."
        )
    state_dir = os.environ.get("OL_SUPERVISOR_STATE_DIR", ".")
    events_log = os.path.join(state_dir, "logs", "events.jsonl")
    # Operator drill knobs (all OFF/neutral by default — absent => unchanged behaviour):
    #  * hang-timeout override for the stall drill;
    #  * a KILL_SWITCH sentinel file in the state dir (FR-036 — refuse new dispatch);
    #  * an opt-in emergency cumulative-spend ceiling (T3#6 backstop — kill on breach).
    from decimal import Decimal
    from pathlib import Path

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

    def _completion_of(row: RegistryRow) -> RunCompletion | None:
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
            terminated_at=datetime.now(UTC).isoformat(),
            terminal_cost_usd=read_terminal_cost(state_dir),
        )

    # §4.4(6) Learn wiring (Item 2): the live completed-Run source + report/corpus sinks.
    from supervisor.attention import (
        ESCALATION_KIND_ROUTINE,
        Escalation,
        intake_escalation,
    )
    from supervisor.cost_forecast import forecast_breaches, forecast_fleet
    from supervisor.effect_measure import (
        DEFAULT_MIN_POST_RUNS,
        NO_EFFECT,
        REGRESSED,
        measure_effect,
        relevant_project_ids,
    )
    from supervisor.event_ingest import events_file_for
    from supervisor.learn_assembly import (
        build_correction_attempts,
        completed_run_records,
        count_iterations,
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
        # Retention (D3): opt-in prune of events older than OL_SUPERVISOR_EVENT_RETENTION_DAYS.
        retention_raw = os.environ.get("OL_SUPERVISOR_EVENT_RETENTION_DAYS")
        if retention_raw:
            from datetime import timedelta

            try:
                cutoff = (
                    datetime.now(UTC) - timedelta(days=float(retention_raw))
                ).isoformat()
                pruned = registry.prune_events(before_iso=cutoff)
                if pruned:
                    print(f"supervisor: pruned {pruned} event(s) older than {retention_raw}d.")
            except Exception as exc:  # noqa: BLE001 - best-effort
                print(f"supervisor: event prune skipped ({exc}).")

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
                raised_at=datetime.now(UTC),
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
        # items_closed (D1): count each Run's iteration_end events → per-item cost forecasting basis.
        records = learning_records(
            rows, items_closed_for=lambda row: count_iterations(scoped_events_for_run(row))
        )
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
                all_findings, new_keys=set(new_keys), now=datetime.now(UTC)
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
    # FUP-0879: seed the dedup ledger from the DB so a loop restart does NOT re-send escalations
    # that were already delivered (the in-memory-only ledger reset on each of the 4 restarts in the
    # 2026-06-11 run, re-emailing the routine offers). Keys are pipe-joined project|gate|raised_at.
    def _key_from_str(s: str) -> tuple[str, str, str]:
        parts = s.split("|", 2)
        while len(parts) < 3:
            parts.append("")
        return (parts[0], parts[1], parts[2])

    try:
        delivered_keys: set[tuple[str, str, str]] = {
            _key_from_str(s) for s in registry.read_delivered_notification_keys()
        }
    except Exception as exc:  # noqa: BLE001 - DB read is best-effort; fall back to empty ledger
        print(f"supervisor: delivered-key ledger load skipped ({exc}); starting empty.")
        delivered_keys = set()
    _persisted_delivered_keys: set[tuple[str, str, str]] = set(delivered_keys)
    if not isinstance(notification_port, NullNotificationPort):
        print("supervisor: notification delivery ENABLED (OL_SUPERVISOR_SMTP_* configured).")

    def _effect_parse_ts(value: object) -> datetime | None:
        # events read from the DB carry a tz-aware datetime ts_utc (timestamptz); runs carry
        # ISO strings. Handle BOTH — a str-only parser silently disabled the run-window filter
        # (events DB ts is a datetime), putting every event in every bucket (live-found 2026-06-08).
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str) or not value:
            return None
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    _effect_escalated: set[str] = set()

    def _measure_learning_effects() -> None:
        """Measure each APPLIED finding's before/after effect from the events table (read-only) and
        persist it; escalate ONCE on a non-confirmed outcome (D1 surface-only — never auto-revert)."""
        try:
            applied = [
                f for f in registry.read_audit_findings() if str(f.get("status")) == "applied"
            ]
            if not applied:
                return
            all_events = [dict(e) for e in registry.read_events_db(limit=100000)]
            runs = list(registry.read_completed_runs())
        except Exception as exc:  # noqa: BLE001 - best-effort; never abort the cycle
            print(f"supervisor: effect measure skipped ({exc}).")
            return
        # Pre-bucket each completed run's events by its [spawned_at, terminated_at] window (D2).
        buckets: list[tuple[str, datetime | None, datetime | None, list[dict[str, object]]]] = []
        for run in runs:
            pid = run.get("project_id")
            if not isinstance(pid, str):
                continue
            start = _effect_parse_ts(run.get("spawned_at"))
            end = _effect_parse_ts(run.get("terminated_at"))
            evs = []
            for e in all_events:
                if e.get("project_id") != pid:
                    continue
                ts = _effect_parse_ts(e.get("ts_utc"))
                if start is not None and ts is not None and ts < start:
                    continue
                if end is not None and ts is not None and ts > end:
                    continue
                evs.append(e)
            buckets.append((pid, start, end, evs))
        for finding in applied:
            kind = str(finding.get("kind"))
            subject = str(finding.get("subject"))
            bclass = finding.get("binding_class")
            bclass = str(bclass) if isinstance(bclass, str) else None
            fkey = str(finding.get("finding_key"))
            applied_raw = finding.get("decided_at")
            applied_at = _effect_parse_ts(applied_raw if isinstance(applied_raw, str) else (applied_raw.isoformat() if hasattr(applied_raw, "isoformat") else None))
            relevant = relevant_project_ids(kind, subject, all_events, binding_class=bclass)
            before_runs: list[list[dict[str, object]]] = []
            after_runs: list[list[dict[str, object]]] = []
            for pid, _start, end, evs in buckets:
                if pid not in relevant:
                    continue
                is_after = applied_at is not None and end is not None and end > applied_at
                (after_runs if is_after else before_runs).append(evs)
            record = measure_effect(
                kind, subject, before_runs=before_runs, after_runs=after_runs,
                binding_class=bclass, finding_key=fkey,
                applied_at=applied_raw.isoformat() if hasattr(applied_raw, "isoformat") else (applied_raw if isinstance(applied_raw, str) else None),
            )
            try:
                registry.upsert_audit_effect(record)
            except Exception as exc:  # noqa: BLE001 - best-effort
                print(f"supervisor: effect upsert for {fkey} skipped ({exc}).")
            if (
                record.outcome in (NO_EFFECT, REGRESSED)
                and record.post_adoption_runs >= DEFAULT_MIN_POST_RUNS
                and f"{fkey}:{record.outcome}" not in _effect_escalated
            ):
                escalation = Escalation(
                    project_id="*fleet*",
                    gate_id=f"effect:{fkey}",
                    kind=ESCALATION_KIND_ROUTINE,
                    reversible=True,
                    suggested_option=(
                        f"adopted learning '{fkey}' shows {record.outcome} "
                        f"({record.detail}) — consider reverting via cf-* / re-propose"
                    ),
                    confidence=0.9,
                    raised_at=datetime.now(UTC),
                )
                state = attention_store.load()
                attention_store.save(intake_escalation(state, escalation))
                _effect_escalated.add(f"{fkey}:{record.outcome}")
                print(f"supervisor: EFFECT — '{fkey}' {record.outcome}; raised an operator offer.")

    # Concurrency improvement (2026-06-09): the operator-tunable Concurrency Ceiling and
    # fill-to-ceiling dispatch bound. Default ceiling stays DEFAULT_CONCURRENCY_CEILING so
    # the status surfaces (which default to the same constant) keep matching; raise it via
    # OL_SUPERVISOR_CONCURRENCY_CEILING. Empirically one Max account sustains >=12 concurrent
    # heavy runs (measured 2026-06-09), so the ceiling — not the API — is the only governor.
    def _positive_int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print(f"supervisor: {name}={raw!r} is not an integer — using {default}.")
            return default
        if value < 1:
            print(f"supervisor: {name}={value} must be >= 1 — using {default}.")
            return default
        return value

    concurrency_ceiling = _positive_int_env(
        "OL_SUPERVISOR_CONCURRENCY_CEILING", DEFAULT_CONCURRENCY_CEILING
    )
    # Defaults to the ceiling: fill the whole fleet in one cycle. Lower it to stagger spawns.
    max_dispatches = _positive_int_env(
        "OL_SUPERVISOR_MAX_DISPATCHES_PER_CYCLE", concurrency_ceiling
    )
    print(
        f"supervisor: concurrency ceiling = {concurrency_ceiling}, "
        f"max dispatches/cycle = {max_dispatches}."
    )

    # Tier-2 usage-window pacing (concurrency, 2026-06-09): opt-in ROLLING caps against the Max
    # session (5-hour) + weekly allowance — the real governor (instantaneous concurrency is not,
    # measured 2026-06-09). Unlike the emergency spend backstop (cumulative $, hard kill), this
    # PAUSES new dispatch (running Runs continue) and reports when the window frees. Absent env →
    # no windows → never paces (safe default OFF). The unit is our recorded cost_usd PROXY — the
    # Max internal quota is not queryable; the operator sets the per-window proxy-dollar budget.
    from supervisor.usage_window import UsageEvent, UsageWindow, evaluate_usage_windows

    def _decimal_env(name: str) -> Decimal | None:
        raw = os.environ.get(name)
        if not raw:
            return None
        try:
            return Decimal(raw)
        except (ArithmeticError, ValueError):
            print(f"supervisor: {name}={raw!r} is not a decimal — that usage window disabled.")
            return None

    usage_windows: list[UsageWindow] = []
    _u5 = _decimal_env("OL_SUPERVISOR_USAGE_5H_CEILING_USD")
    if _u5 is not None:
        usage_windows.append(UsageWindow("5h", timedelta(hours=5), _u5))
    _uw = _decimal_env("OL_SUPERVISOR_USAGE_WEEKLY_CEILING_USD")
    if _uw is not None:
        usage_windows.append(UsageWindow("weekly", timedelta(days=7), _uw))
    if usage_windows:
        print(
            "supervisor: usage-window pacing ENABLED — "
            + ", ".join(f"{w.name}<=${w.budget_usd}" for w in usage_windows)
        )
    _usage_paced: set[str] = set()

    def _dispatch_allowed_by_usage() -> bool:
        """True if new Dispatch is allowed under the rolling usage windows (pause-not-kill).

        Reads the widest window's worth of ``llm_call`` usage, evaluates every window, and on a
        breach logs + raises ONE operator escalation per (window, reset) carrying the reset time.
        Best-effort: a read error allows dispatch (never abort the cycle on the guard)."""
        if not usage_windows:
            return True
        widest = max(w.duration for w in usage_windows)
        now_dt = datetime.now(UTC)
        since = (now_dt - widest).isoformat()
        try:
            rows = registry.read_usage_events_since(since)
        except Exception as exc:  # noqa: BLE001 - best-effort; never abort the cycle
            print(f"supervisor: usage-window read skipped ({exc}); allowing dispatch.")
            return True
        events = [UsageEvent(ts=ts, cost_usd=cost) for ts, cost in rows]
        decision = evaluate_usage_windows(events, usage_windows, now=now_dt)
        for breach in decision.breaches:
            key = f"{breach.name}:{breach.resets_at.isoformat()}"
            if key in _usage_paced:
                continue
            _usage_paced.add(key)
            print(
                f"supervisor: USAGE PACING — {breach.name} window at "
                f"${breach.used_usd}/${breach.budget_usd}; pausing NEW dispatch until "
                f"~{breach.resets_at.isoformat()} (running Runs continue)."
            )
            escalation = Escalation(
                project_id="*fleet*",
                gate_id=f"usage:{breach.name}",
                kind=ESCALATION_KIND_ROUTINE,
                reversible=True,
                suggested_option=(
                    f"usage {breach.name} window ${breach.used_usd} >= ${breach.budget_usd} — new "
                    f"dispatch paused until ~{breach.resets_at.isoformat()} (Max cap pacing)"
                ),
                confidence=0.9,
                raised_at=now_dt,
            )
            state = attention_store.load()
            attention_store.save(intake_escalation(state, escalation))
        return decision.dispatch_allowed

    def _const_gate(allowed: bool) -> Callable[[], bool]:
        """A by-value dispatch gate for this cycle's usage verdict (typed for mypy --strict)."""
        return lambda: allowed

    # Lifecycle milestone emails (operator 2026-06-12): email when RL STARTS a project
    # (-> running), FINISHES one (-> complete), or a project STOPS despite this cycle's
    # reconcile/repair (-> failed / halted, i.e. RL's recovery did not save it). These are FYI
    # MILESTONES, not escalations — sent straight through the notification port, bypassing the
    # routine-suppression filter, so the operator sees the signal they asked for. Restart-safe:
    # the snapshot re-baselines on launch, so a restart never re-emails an already-running
    # project (a milestone fires only on an observed transition). Default ON; OL_SUPERVISOR_
    # NOTIFY_LIFECYCLE=0 disables. ROUTINE auto-feedback stays suppressed independently.
    _notify_lifecycle = os.environ.get("OL_SUPERVISOR_NOTIFY_LIFECYCLE", "1") != "0"
    _LIFECYCLE_MILESTONES = {
        "running": "started",
        "complete": "finished",
        "failed": "stopped (recovery exhausted)",
        "halted": "stopped (recovery exhausted)",
        # FUP-0862: a project blocked on an operator-answerable gate (the broker / plan_review
        # non-convergence / spend-limit paths now write a gate_request -> reconcile classifies
        # `paused_gate`). This is the ACTION-REQUIRED "RL needs you" signal — emailed immediately
        # (it is NOT a stop-state, so it bypasses the one-cycle stop-confirmation deferral).
        "paused_gate": "NEEDS OPERATOR ANSWER",
    }

    def _lifecycle_snapshot() -> dict[str, str]:
        try:
            return {
                str(p.get("project_id")): str(p.get("lifecycle_state") or "")
                for p in registry.read_all_projects()
            }
        except Exception as exc:  # noqa: BLE001 - best-effort; never abort the cycle
            print(f"supervisor: lifecycle snapshot skipped ({exc}).")
            return {}

    _LIFECYCLE_STOP_STATES = {"failed", "halted"}
    # FUP-0880 refinement: a project that stops and is auto-recovered (re-dispatched) next cycle
    # should NOT read as a terminal stop. So STOP emails are DEFERRED one cycle: arm a failed/
    # halted project, and only email if it is STILL stopped on the following cycle (not recovered).
    # started/finished email immediately. _pending_stop maps project_id -> the armed stop state.
    _pending_stop: dict[str, str] = {}

    def _send_milestone(pid: str, state: str, verb: str, prev_state: str | None) -> None:
        subject = f"[ol-build supervisor] project {pid} {verb}"
        body = (
            f"Project {pid} is now '{state}' ({verb}).\n"
            f"Previous state: {prev_state or '(new/unknown)'}.\n"
        )
        try:
            notification_port.send_message(subject, body)
            print(f"supervisor: LIFECYCLE — emailed '{pid} {verb}'.")
        except Exception as exc:  # noqa: BLE001 - a notify failure must never abort the cycle
            print(f"supervisor: lifecycle email for {pid} skipped ({exc}).")

    def _emit_lifecycle(prev: dict[str, str], curr: dict[str, str]) -> None:
        if not _notify_lifecycle:
            return
        # 1. Immediate milestones — started (-> running) / finished (-> complete).
        for pid, state in curr.items():
            if state in _LIFECYCLE_STOP_STATES:
                continue
            verb = _LIFECYCLE_MILESTONES.get(state)
            if verb is None or prev.get(pid) == state:
                continue  # not a milestone, or no transition into it this cycle
            _send_milestone(pid, state, verb, prev.get(pid))
        # 2. Confirm deferred STOPs armed a cycle ago: email only if still stopped (else the
        #    project was recovered/re-dispatched, so drop the arm silently — no false stop).
        for pid in list(_pending_stop):
            armed = _pending_stop.pop(pid)
            now_state = curr.get(pid)
            if now_state in _LIFECYCLE_STOP_STATES:
                _send_milestone(pid, now_state, _LIFECYCLE_MILESTONES[now_state], armed)
        # 3. Arm freshly-stopped projects for next-cycle confirmation.
        for pid, state in curr.items():
            if state in _LIFECYCLE_STOP_STATES and prev.get(pid) != state:
                _pending_stop[pid] = state

    _lifecycle_prev = _lifecycle_snapshot()

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
        # Tier-2: evaluate the rolling usage windows ONCE per cycle; the result gates only the
        # spawn path (running Runs continue). Bound via default-arg so the gate is this cycle's.
        usage_allowed = _dispatch_allowed_by_usage()
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
            record_failure_detail=registry.record_run_failure_detail,
            kill_switch=kill_switch,
            completed_project_ids=registry.read_completed_project_ids,
            learn_runs_source=_learn_runs_source,
            learn_report_sink=_learn_report_sink,
            attention_store=attention_store,
            notification_port=notification_port,
            delivered_keys=delivered_keys,
            concurrency_ceiling=concurrency_ceiling,
            max_dispatches_per_cycle=max_dispatches,
            dispatch_gate=_const_gate(usage_allowed),
        )
        cycle.run_once()
        _ingest_fleet_events()  # Fleet Analytics §1: ship all projects' events.jsonl to the DB
        _forecast_guard()  # Fleet Analytics §2: warn-only projected-spend guard
        _measure_learning_effects()  # Effect-Measurement Loop: did adopted learnings help?
        # Lifecycle milestone emails: diff project states vs the pre-cycle snapshot and email the
        # operator on start / finish / unrecoverable-stop transitions (operator 2026-06-12).
        _lifecycle_curr = _lifecycle_snapshot()
        _emit_lifecycle(_lifecycle_prev, _lifecycle_curr)
        _lifecycle_prev = _lifecycle_curr
        # FUP-0879: persist escalation keys delivered this cycle so a restart will not re-send them.
        _new_delivered = delivered_keys - _persisted_delivered_keys
        if _new_delivered:
            try:
                registry.record_delivered_notification_keys(
                    ["|".join(k) for k in _new_delivered]
                )
                _persisted_delivered_keys |= _new_delivered
            except Exception as exc:  # noqa: BLE001 - persistence is best-effort; never abort
                print(f"supervisor: delivered-key persist skipped ({exc}).")
        cycles += 1
        if args.once or (args.max_cycles is not None and cycles >= args.max_cycles):
            break
        time.sleep(args.interval)
    print(f"supervisor: ran {cycles} cycle(s).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "DEFAULT_HANG_TIMEOUT_SECONDS",
    "build_production_cycle",
    "format_pid_start_time",  # re-exported from pid_probe (FR-013 live-probe call-site)
    "main",
]
