"""Live wiring collaborators for the §4.4 supervision-cycle host (OLB-11).

The C3 fleet-scheduling checkpoint (Spec v1.3 §7/§8/§9) fills the
intentionally-empty ``SupervisionCycle._schedule()`` / ``_attend()`` policy hooks
(OLB-01) with the real composition of the already-built, closed components — the
OLB-09 Cross-Project Scheduler (``supervisor/scheduler.py``), the OLB-07/07x
Admission Pipeline (``supervisor/admission.py``), the OLB-06 Safety-Gates floor
(``supervisor/safety_gates.py``), and the OLB-10 Operator-Attention Scheduler
(``supervisor/attention.py``). Resolved per gate ``olb11-c3-cycle-wiring-scope``
(option A — additive fill of the empty hooks behind unchanged signatures, the
§6-permitted additive case, OLB-08a precedent).

The C4 anomaly-drills checkpoint (Spec v1.3 §9/§10/§11) additively extends the same
module to fill the third empty hook — ``SupervisionCycle._guard()`` — composing the
already-built, closed OLB-12 Cost Circuit-Breaker
(``supervisor/cost_circuit_breaker.py``), the OLB-13 Repair-Auto-OK Policy
(``supervisor/repair_policy.py``), and the OLB-06 Safety-Gates floor's FR-038 trip
(``supervisor/safety_gates.py``), routing the resulting top-tier escalations through
the OLB-10 attention intake. Resolved per gate ``olb14-c4-guard-wiring-scope``
(option A — additive fill of the empty hook behind unchanged signatures, the same
§6-permitted additive case as the OLB-11 schedule/attend fill).

This module is the additive home for the wiring's supporting pieces so the cycle
host stays the visible composition site while the reusable / persisted parts live
here:

* :class:`RoundStateStore` — the persisted FR-025/FR-026 scheduler round-state
  (skip-counts) carried across rounds; in-memory by default, JSON-file-backed when
  a path is supplied (the persistence the OLB-09 forward-reference owed C3/OLB-11).
* :class:`AttentionStateStore` — the persisted OLB-10 :class:`AttentionState`
  (Attention Debt + the unresolved-escalation queue) carried across rounds.
* :class:`ScheduleConfig` / :class:`AttendConfig` — the defaulted collaborator
  bundles ``SupervisionCycle.__init__`` accepts so the hooks run the real policy
  only when configured (a config-less cycle stays the OLB-01 mechanical no-op).
* :func:`to_project_records` — adapts the OLB-02 ``RegistryRow`` reads into the
  scheduler's :class:`~supervisor.scheduler.ProjectRecord` value objects (the
  adaptation the OLB-09 ``ProjectRecord`` docstring reserves for "the OLB-11
  wiring step").

Closed-seam discipline: nothing here re-implements or re-shapes a closed
component. ``_schedule`` consumes the scheduler + admission through their public
surfaces; ``_attend`` consumes the attention layer's pure functions. The live
``projects.attention_debt`` column write through the ``RegistryPort`` (no writer
method exists on the seam) and the live ``orchestrator.sh`` spawn / resume of a
``running`` Project remain forward-referenced to OLB-16 — neither is needed for
the OLB-11 predicate (priority order, ceiling hold, dispatch idempotency,
gate-blocked skip).
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from supervisor.admission import (
    AdmissionRejection,
    AdmittedHold,
    DependencyHold,
    ReconciledFailure,
    RunRecord,
    SeedValidatorPort,
    SpawnPort,
    admit_candidate,
    unmet_prerequisites,
)
from supervisor.attention import (
    ESCALATION_KIND_SAFETY_GATE,
    AttentionState,
    Escalation,
    NotificationBatch,
    NotificationPlan,
    QuietHours,
    intake_escalation,
    plan_notifications,
)
from supervisor.candidate_enrichment import default_candidate_enricher
from supervisor.notifications import (
    NotificationPort,
    NullNotificationPort,
)
from supervisor.pid_probe import pid_alive
from supervisor.reconcile import (
    REASON_STALLED,
    RUN_COMPLETE,
    ReconcileAction,
    RunCompletion,
    derive_reconcile_actions,
)
from supervisor.run_auditor import (
    AuditConfig,
    RunAuditReport,
    run_audit_pass,
)
from supervisor.run_auditor import RunRecord as AuditRunRecord
from supervisor.cost_circuit_breaker import (
    BreakerConfig,
    BreakerTrip,
    IterationObservation,
    evaluate_fleet,
)
from supervisor.repair_policy import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    AutoRepairAuditRecord,
    RepairAction,
    RepairKind,
    build_audit_record,
    evaluate_repair,
)
from supervisor.safety_gates import (
    DEFAULT_CONCURRENCY_CEILING,
    KillSwitch,
    SafetyEscalation,
    trip_to_paused_safety,
)
from supervisor.scheduler import (
    ADMITTED_STATE,
    RUNNING_STATE,
    STARVATION_ROUND_THRESHOLD,
    ProjectRecord,
    SchedulerDecision,
    select_next_dispatch,
)

if TYPE_CHECKING:
    from supervisor.ports import RegistryPort, RegistryRow

# The terminal admission outcomes _schedule's spawn arm may receive (Spec v1.3
# §6.1). A RunRecord is a spawned Run; AdmittedHold is the FR-019 ceiling hold;
# ReconciledFailure / AdmissionRejection are the non-spawn terminals. Named once
# so the spawn-arm return annotation cannot drift from admit_candidate's.
DispatchOutcome = (
    RunRecord | ReconciledFailure | AdmittedHold | DependencyHold | AdmissionRejection
)


# --- Persisted scheduler round-state (FR-025 / FR-026) ------------------------


class RoundStateStore:
    """The persisted scheduler round-state (Spec v1.3 §7.2 FR-025 / FR-026).

    Holds the ``project_id -> skip-count`` map the Cross-Project Scheduler threads
    across rounds (the starvation counter; gate-blocked Projects' counts carry
    forward untouched per FR-026). In-memory by default; when constructed with a
    ``path`` it persists to that JSON file so the round-state survives across
    distinct cycle passes (the persistence OLB-09 forward-referenced to OLB-11).
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        initial: Mapping[str, int] | None = None,
    ) -> None:
        self._path = path
        self._memory: dict[str, int] = dict(initial or {})
        if path is not None and initial is not None and not path.exists():
            # Seed a fresh JSON file from the supplied initial state so the first
            # load reflects it; an existing file is authoritative and never clobbered.
            self.save(self._memory)

    def load(self) -> dict[str, int]:
        """Return the current round-state map (a fresh copy each call)."""
        if self._path is not None and self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in raw.items()}
        return dict(self._memory)

    def save(self, round_state: Mapping[str, int]) -> None:
        """Persist ``round_state`` as the new authoritative round-state."""
        self._memory = {str(k): int(v) for k, v in round_state.items()}
        if self._path is not None:
            self._path.write_text(
                json.dumps(self._memory, sort_keys=True), encoding="utf-8"
            )


# --- Persisted operator-attention state (FR-028 / FR-033) ---------------------


class AttentionStateStore:
    """The persisted Operator-Attention state (Spec v1.3 §8 FR-028 / FR-033).

    Holds the OLB-10 :class:`~supervisor.attention.AttentionState` — the per-Project
    Attention Debt map plus the unresolved-escalation queue — carried across cycle
    passes. In-memory by default; the live ``projects.attention_debt`` column write
    through the ``RegistryPort`` (no writer method on the seam) stays OLB-16.
    """

    def __init__(self, *, initial: AttentionState | None = None) -> None:
        self._state = initial if initial is not None else AttentionState.empty()

    def load(self) -> AttentionState:
        """Return the current persisted :class:`AttentionState`."""
        return self._state

    def save(self, state: AttentionState) -> None:
        """Persist ``state`` as the new authoritative Operator-Attention state."""
        self._state = state


# --- Defaulted collaborator bundles -------------------------------------------


def _identity_enricher(row: RegistryRow) -> RegistryRow:
    """A pass-through candidate enricher — returns the row unchanged.

    Retained for callers (and checkpoint tests) that pre-enrich the discovered row
    themselves and want no further derivation. The ``ScheduleConfig`` default is
    :func:`supervisor.candidate_enrichment.default_candidate_enricher` (FUP-0855),
    which derives the §6 admission-input fields (``seed_path`` / ``open_item_count``
    / ``writable_paths`` / ``mcp_roots`` / ``read_only_paths``) from the candidate's
    seed when ``OL_SUPERVISOR_WORKSPACE_ROOT`` is configured, and falls back to this
    pass-through behaviour when it is not.
    """
    return row


def _utc_now_iso() -> str:
    """Default admission ``spawned_at`` clock — an ISO-8601 UTC timestamp."""
    return datetime.now().astimezone().isoformat()


def _utc_now_dt() -> datetime:
    """Default Reconcile clock — a tz-aware ``datetime`` (T1#1)."""
    return datetime.now().astimezone()


# Default PID-liveness probe for the Reconcile step (T1#1): the single shared,
# read-only `pid_probe.pid_alive` (does NOT use os.kill on Windows, where that would
# terminate the pid). Production injects the same via `__main__._pid_alive`; this
# default mirrors it so the un-injected path is safe too.
_default_pid_alive = pid_alive


@dataclass(frozen=True)
class ReconcileConfig:
    """The §4.4 step-1 Reconcile collaborators (robustness T1#1).

    ``active_runs_source`` returns the ``ralph_runs`` rows currently ``running``
    (the production wiring passes ``Registry.read_active_runs``; a checkpoint test
    passes a fixture). ``pid_alive`` probes orchestrator liveness, ``progress_at``
    yields each run's last-progress instant (default ``spawned_at``; production
    injects the event-stream ``phase_complete`` lookup), and ``hang_timeout_seconds``
    is the stall budget (defaults to the seed ``hang_timeout_seconds``). A dead-PID
    run is reconciled ``failed``; a stalled run ``halted`` + ``paused_gate``. Both
    release the active-run unique-index slot. ``project_filter`` scopes the pass (a
    bounded checkpoint reconciles only its own disposable runs).
    """

    active_runs_source: Callable[[], "Sequence[RegistryRow]"]
    pid_alive: Callable[[int], bool] = _default_pid_alive
    hang_timeout_seconds: float = 1800.0
    progress_at: Callable[["RegistryRow"], "str | None"] = field(
        default=lambda row: (
            str(row.get("spawned_at"))
            if isinstance(row.get("spawned_at"), str) and row.get("spawned_at")
            else None
        )
    )
    clock: Callable[[], datetime] = _utc_now_dt
    project_filter: Callable[["RegistryRow"], bool] = field(
        default_factory=lambda: (lambda row: True)
    )
    #: Terminal-completion probe (D6 follow-on): reports a clean INITIATIVE_COMPLETE
    #: for a run (production reads its state-dir artifacts via run_lifecycle). Default
    #: never-completed → the failed/stall-only behaviour is unchanged.
    completion_of: Callable[["RegistryRow"], "RunCompletion | None"] = field(
        default=lambda _row: None
    )
    #: Per-run hang-budget override (F-4): returns the run's seed
    #: ``budget.hang_timeout_seconds`` (production reads it off ``seed_path``), else
    #: ``None`` → the fleet-default ``hang_timeout_seconds`` applies. Default never
    #: overrides → unchanged behaviour.
    hang_timeout_of: Callable[["RegistryRow"], "float | None"] = field(
        default=lambda _row: None
    )


def run_reconcile_step(
    registry: "RegistryPort", config: ReconcileConfig
) -> list[ReconcileAction]:
    """Compose the §4.4 step-1 Reconcile for one pass (robustness T1#1).

    Reads the active runs, derives the terminal reconciliations owed (dead-PID /
    stall), and applies each via ``reconcile_run`` (terminal status + ``terminated_at``
    + the run's last-known cost, releasing the active-run slot) then
    ``set_lifecycle_state`` (the legal post-``running`` Project state). Returns the
    actions taken (for the surface / audit). A zero-run or all-healthy pass is a
    genuine no-op — no registry write.
    """
    runs = [row for row in config.active_runs_source() if config.project_filter(row)]
    now_dt = config.clock()
    actions = derive_reconcile_actions(
        runs,
        pid_alive=config.pid_alive,
        now=now_dt,
        hang_timeout_seconds=config.hang_timeout_seconds,
        progress_at=config.progress_at,
        completion_of=config.completion_of,
        hang_timeout_of=config.hang_timeout_of,
    )
    if not actions:
        return actions
    now_iso = now_dt.isoformat()
    cost_by_project = {
        str(row.get("project_id")): row.get("terminal_cost_usd") for row in runs
    }
    for action in actions:
        if action.run_status == RUN_COMPLETE:
            # A clean completion carries its own terminal_at + cost (read from the
            # run's terminal artifacts), not the row's last-known running cost.
            at = action.terminated_at or now_iso
            cost = action.terminal_cost_usd or Decimal("0")
        else:
            at = now_iso
            raw_cost = cost_by_project.get(action.project_id)
            cost = raw_cost if isinstance(raw_cost, Decimal) else Decimal("0")
        registry.reconcile_run(
            action.project_id,
            action.run_status,
            terminated_at=at,
            terminal_cost_usd=cost,
        )
        registry.set_lifecycle_state(action.project_id, action.lifecycle_state)
    return actions


@dataclass(frozen=True)
class ScheduleConfig:
    """The §4.4 step-3 Schedule collaborators (Spec v1.3 §7 / §6 / §9).

    Bundles everything ``_schedule`` composes: the admission ports (the FR-016
    ``seed_validator``, the FR-021 ``spawn_port``, the FR-036 ``kill_switch``), the
    FR-037 ``concurrency_ceiling`` and FR-025 ``starvation_threshold``, the persisted
    :class:`RoundStateStore`, the FR-024 ``open_work_counts`` (the §13 FR-059
    work-registry open-counts, supplied as a figure this iteration — the live source
    is OLB-16), and the ``candidate_enricher`` that merges the seed-derived admission
    inputs onto a discovered ``projects`` row. All carry build-time defaults so a
    caller need only override what it exercises.
    """

    seed_validator: SeedValidatorPort
    spawn_port: SpawnPort
    kill_switch: KillSwitch = field(default_factory=KillSwitch)
    concurrency_ceiling: int = DEFAULT_CONCURRENCY_CEILING
    #: Fill-to-ceiling dispatch bound (concurrency improvement, 2026-06-09): the max
    #: number of Candidates :func:`run_schedule_fill_step` may spawn in ONE Schedule
    #: pass. Default 1 preserves the historical one-dispatch-per-cycle behaviour (and
    #: every existing single-dispatch test); production sets it to ``concurrency_ceiling``
    #: so a cold fleet ramps to full concurrency in a single cycle instead of one
    #: Project per ``--interval``. The live ``running_count`` / ceiling guard in the fill
    #: loop is always authoritative, so a value above the ceiling never overshoots
    #: (the §9.3 FR-019 / FR-037 hard floor).
    max_dispatches_per_cycle: int = 1
    #: Tier-2 usage-window pause hook (concurrency, 2026-06-09): returns ``True`` when new
    #: Dispatch is allowed, ``False`` to PAUSE dispatch this pass WITHOUT touching running Runs
    #: — reconcile / guard / attend still run and running Runs continue to completion (the
    #: pause-not-kill lever, distinct from the FR-036 Kill-Switch which also safe-stops running).
    #: Production wires it to the rolling usage-window guard (:mod:`supervisor.usage_window`) so
    #: the fleet self-paces under the Max session/weekly cap. Default always-allow → no pacing
    #: unless an operator configures a window ceiling.
    dispatch_gate: Callable[[], bool] = field(default_factory=lambda: (lambda: True))
    starvation_threshold: int = STARVATION_ROUND_THRESHOLD
    round_state_store: RoundStateStore = field(default_factory=RoundStateStore)
    open_work_counts: Mapping[str, int] = field(default_factory=dict)
    candidate_enricher: Callable[[RegistryRow], RegistryRow] = default_candidate_enricher
    clock: Callable[[], str] = _utc_now_iso
    #: FR-019 ceiling-held Projects to re-feed the scheduler (production wires
    #: ``Registry.read_admitted``). Default empty → a candidate-only cycle is unchanged.
    #: Without this an ``admitted`` Project is dispatched-eligible per the scheduler but
    #: never appears in its input, so a held Project would be orphaned.
    admitted_source: Callable[[], "Sequence[RegistryRow]"] = field(default=lambda: [])
    #: Item 1 cross-initiative dependency gating: the set of ``complete`` project_ids a
    #: Candidate's ``depends_on`` is checked against (production wires
    #: ``Registry.read_completed_project_ids``). A Candidate with any unmet prerequisite is
    #: filtered out of the dispatch pool this cycle (so it never consumes the single dispatch
    #: slot) AND held by admission as a safety net. Default empty → nothing is dependency-blocked
    #: (a single-initiative fleet is unchanged).
    completed_project_ids: Callable[[], frozenset[str]] = field(
        default=lambda: frozenset()
    )
    #: FR-013 recorded-half recorder: persists the spawned orchestrator's OS start-time
    #: into the Run's ``metadata.pid_start_time`` post-spawn (production wires
    #: ``Registry.record_pid_start_time``). An injected callable, NOT a RegistryPort
    #: method, so the seam stays free of test-double ripple. Default ``None`` →
    #: start-time recording is skipped.
    record_start_time: Callable[[str, str], None] | None = None
    # The fleet-scoping predicate applied to both the Candidate and running reads.
    # Defaults to accept-all — the production global fleet (the §3 Concurrency Ceiling
    # is fleet-wide). A bounded checkpoint supplies a predicate so it operates over,
    # and consumes ceiling against, ONLY its own disposable Projects, never another
    # initiative's live rows.
    project_filter: Callable[[RegistryRow], bool] = field(
        default_factory=lambda: (lambda row: True)
    )


@dataclass(frozen=True)
class AttendConfig:
    """The §4.4 step-4 Attend collaborators (Spec v1.3 §8 FR-028–031).

    Bundles what ``_attend`` composes: the persisted :class:`AttentionStateStore`,
    the FR-031 ``quiet_hours`` window (``None`` = no Quiet Hours), the FR-030
    ``batch_window``, the injected ``clock`` (no wall-clock read inside the pure
    attention layer), and the ``incoming`` source of newly-raised escalations to
    intake this pass (FR-028; empty by default).
    """

    attention_store: AttentionStateStore = field(default_factory=AttentionStateStore)
    quiet_hours: QuietHours | None = None
    batch_window: timedelta = timedelta(hours=1)
    clock: Callable[[], datetime] = field(
        default_factory=lambda: (lambda: datetime.now().astimezone())
    )
    incoming: Callable[[], Sequence[Escalation]] = field(
        default_factory=lambda: (lambda: ())
    )
    #: Item 3 delivery: the notification port the planned batches are dispatched to (SMTP when
    #: ``OL_SUPERVISOR_SMTP_*`` is configured, else the no-op :class:`NullNotificationPort`).
    #: Default no-op → planning stays observable-free unless a real port is wired.
    notification_port: NotificationPort = field(default_factory=NullNotificationPort)
    #: Item 3 dedup ledger: ``(project_id, gate_id, raised_at-iso)`` keys already delivered, so an
    #: unresolved escalation is paged once, not re-sent every cycle. An injected mutable set shared
    #: across cycles by the production wiring. ``None`` (the default) disables dedup — the plan is
    #: delivered as-is (back-compat for callers/tests that don't supply a ledger).
    delivered_keys: "set[tuple[str, str, str]] | None" = None


# --- RegistryRow -> ProjectRecord adaptation (the OLB-11 wiring step) ---------


def _priority(row: RegistryRow) -> int:
    """The Project's §5.2 ``priority`` weight (0 when absent / non-int)."""
    value = row.get("priority")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def to_project_records(
    candidates: Iterable[RegistryRow],
    running: Iterable[RegistryRow],
    *,
    open_work_counts: Mapping[str, int],
) -> list[ProjectRecord]:
    """Adapt the OLB-02 read rows into scheduler :class:`ProjectRecord`\\ s.

    The adaptation the OLB-09 ``ProjectRecord`` docstring reserves for the OLB-11
    wiring step. Each Candidate is presented as an :data:`~supervisor.scheduler.ADMITTED_STATE`
    record — it is dispatch-eligible (a spawn) and the live ceiling is enforced
    downstream by admission (FR-019 hold) + the §9 safety floor (FR-037), per §9.3.
    Each ``running`` Project is presented as a :data:`~supervisor.scheduler.RUNNING_STATE`
    record so the FR-027 in-flight exclusion is exercised against it. ``open_work_count``
    is taken from the supplied map (0 when unlisted), the FR-024 closest-to-done key.
    """
    def _record(row: RegistryRow, lifecycle_state: str) -> ProjectRecord:
        project_id = str(row["project_id"])
        return ProjectRecord(
            project_id=project_id,
            lifecycle_state=lifecycle_state,
            priority=_priority(row),
            open_work_count=open_work_counts.get(project_id, 0),
        )

    return [_record(row, ADMITTED_STATE) for row in candidates] + [
        _record(row, RUNNING_STATE) for row in running
    ]


# --- §4.4 step-3 Schedule composition ----------------------------------------


def run_schedule_step(
    registry: RegistryPort, config: ScheduleConfig
) -> SchedulerDecision | None:
    """Compose the OLB-09 scheduler + OLB-07 admission for one Schedule pass.

    Reads the fleet through the OLB-02 seam (scoped by ``config.project_filter`` —
    accept-all in production, the disposable fleet in a checkpoint), adapts it to
    scheduler records, selects
    the next Dispatch (FR-023 priority / FR-024 closest-to-done / FR-025 starvation,
    excluding FR-027 in-flight Projects), persists the updated round-state (FR-025 /
    FR-026), and — on a spawn selection — runs the chosen Candidate through the
    Admission Pipeline, whose FR-019 hold + §9 FR-037 ceiling enforce the live
    Concurrency Ceiling (the §9.3 hard floor). Returns the :class:`SchedulerDecision`
    (or ``None`` when nothing is runnable). Issues no write of its own beyond the
    round-state save and admission's own writes.

    The scheduler is fed ``ceiling_headroom = concurrency_ceiling`` (the nominal
    capacity, always >= 1) so a Candidate stays schedulable and the AUTHORITATIVE
    spawn-or-hold decision is admission's on the live ``running_count`` — the §9.3
    precedence that the safety floor, not the scheduler, is the hard ceiling.
    """
    completed = config.completed_project_ids()
    candidates = [
        row
        for row in registry.read_candidates()
        if config.project_filter(row) and not unmet_prerequisites(row, completed)
    ]
    admitted = [row for row in config.admitted_source() if config.project_filter(row)]
    running = [row for row in registry.read_running() if config.project_filter(row)]
    running_count = len(running)
    in_flight = [str(row["project_id"]) for row in running]

    # Candidates AND ceiling-held `admitted` Projects are both spawn-eligible (FR-019
    # hold → spawn once headroom frees); both are presented to the scheduler as
    # ADMITTED records and are the lookup pool for the spawn step. Item 1: dependency-blocked
    # Candidates were already excluded above, so a blocked Project never consumes the single
    # per-cycle dispatch slot (`admitted` Projects already cleared the gate, dependency
    # included, so they need no re-filter here).
    dispatchable = [*candidates, *admitted]
    records = to_project_records(
        dispatchable, running, open_work_counts=config.open_work_counts
    )
    decision = select_next_dispatch(
        records,
        in_flight_project_ids=in_flight,
        round_state=config.round_state_store.load(),
        ceiling_headroom=config.concurrency_ceiling,
        starvation_threshold=config.starvation_threshold,
    )
    if decision is None:
        return None

    # Persist the FR-025/FR-026 round-state for the next pass (gate-blocked Projects'
    # skip-counts carry forward untouched — they were never in `records`).
    config.round_state_store.save(decision.round_state)

    if decision.dispatch_kind == "spawn":
        _spawn_selected(
            registry, config, decision.project_id, dispatchable, running_count, completed
        )
    # The resume arm (a `running` Project's next iteration) is OLB-16; it is never
    # selected here because every `running` Project is FR-027 in-flight-excluded.
    return decision


def run_schedule_fill_step(
    registry: RegistryPort, config: ScheduleConfig
) -> list[SchedulerDecision]:
    """Fill the running fleet toward the Concurrency Ceiling in ONE Schedule pass.

    :func:`run_schedule_step` dispatches at most one Candidate per call (one
    ``select_next_dispatch`` -> at most one spawn). On the bounded ``--interval``
    cadence that ramps a cold fleet only one Project per cycle, so reaching a ceiling
    of N takes N cycles. This wrapper repeats the single-pass primitive WITHIN one
    cycle until the fleet is at the ceiling, nothing more is runnable, or
    ``config.max_dispatches_per_cycle`` attempts are spent — turning the proven
    concurrency headroom into actual fleet throughput (concurrency improvement,
    2026-06-09; the empirical N-way headroom measured 2026-06-09).

    Each iteration re-reads the live ``running`` set through the OLB-02 seam (a spawn
    in the previous iteration created a real ``ralph_runs`` ``running`` row the FR-027
    unique index gates), so the loop is driven by the AUTHORITATIVE running count, not
    a cached one. It terminates when:

    * ``running_count >= concurrency_ceiling`` BEFORE a dispatch — the §9.3 hard floor,
      so a ``max_dispatches_per_cycle`` larger than the ceiling never overshoots;
    * :func:`run_schedule_step` returns ``None`` — nothing runnable this pass;
    * an attempt did NOT grow the running set — an FR-019 ceiling HOLD (Candidate ->
      ``admitted``, nothing spawned) or a non-spawn admission terminal, so a held /
      rejected Candidate never spins the loop.

    Returns the dispatch decisions made this pass (possibly empty). With the default
    ``max_dispatches_per_cycle == 1`` this calls :func:`run_schedule_step` exactly once
    — behaviourally identical to the pre-improvement single-dispatch cycle, which is why
    every existing single-dispatch test holds unchanged.
    """
    decisions: list[SchedulerDecision] = []
    if not config.dispatch_gate():
        # Tier-2 usage-window pause (concurrency, 2026-06-09): the rolling Max session/weekly
        # cap is reached — pause NEW dispatch this pass. Running Runs are untouched (this hook
        # gates only the spawn path); they continue and the window frees as their usage ages out.
        return decisions
    attempts = max(config.max_dispatches_per_cycle, 1)
    for _ in range(attempts):
        before = sum(1 for row in registry.read_running() if config.project_filter(row))
        if before >= config.concurrency_ceiling:
            break  # at the ceiling — fill complete (§9.3 hard floor, never overshoot)
        decision = run_schedule_step(registry, config)
        if decision is None:
            break  # nothing runnable this pass
        decisions.append(decision)
        after = sum(1 for row in registry.read_running() if config.project_filter(row))
        if after <= before:
            # No running row was created (FR-019 hold / non-spawn terminal) — stop so a
            # held or rejected Candidate cannot spin the loop to the attempt bound.
            break
    return decisions


def _spawn_selected(
    registry: RegistryPort,
    config: ScheduleConfig,
    project_id: str,
    candidates: Sequence[RegistryRow],
    running_count: int,
    completed_project_ids: frozenset[str] = frozenset(),
) -> DispatchOutcome:
    """Run the scheduler-selected Candidate through the §6 Admission Pipeline.

    Looks up the selected Candidate row, enriches it with the seed-derived admission
    inputs, and calls ``admit_candidate`` — the only path Candidate -> ``running``
    (Spec v1.3 §6.1). Admission enforces the live ceiling: a spawn when headroom
    exists (a real ``ralph_runs`` ``running`` row the FR-027 unique index gates), or
    the FR-019 hold (Candidate -> ``admitted``, nothing spawned) at the ceiling.
    Raises ``KeyError`` if the selection is not among the read Candidates (a seam
    violation surfaced, never a silent skip).
    """
    selected = next(
        (row for row in candidates if str(row["project_id"]) == project_id), None
    )
    if selected is None:
        raise KeyError(
            f"scheduler selected {project_id!r} for spawn but it is not among the "
            f"read Candidates — RegistryPort read seam inconsistency"
        )
    candidate = config.candidate_enricher(selected)
    return admit_candidate(
        candidate,
        seed_validator=config.seed_validator,
        registry_port=registry,
        spawn_port=config.spawn_port,
        kill_switch=config.kill_switch,
        running_count=running_count,
        concurrency_ceiling=config.concurrency_ceiling,
        clock=config.clock,
        record_start_time=config.record_start_time,
        completed_project_ids=completed_project_ids,
    )


# --- §4.4 step-4 Attend composition ------------------------------------------


def run_attend_step(registry: RegistryPort, config: AttendConfig) -> NotificationPlan:
    """Compose the OLB-10 Operator-Attention layer for one Attend pass.

    Intakes any newly-raised escalations into the persisted :class:`AttentionState`
    (FR-028 — Attention Debt +1 and queued), persists the updated state, and plans
    the operator notifications (FR-029 top-tier-first / FR-030 routine batching /
    FR-031 Quiet-Hours loss-free deferral). Returns the :class:`NotificationPlan`;
    the live channel dispatch (``gmail_smtp``) and the live ``projects.attention_debt``
    column write are OLB-16, so this consults the registry for nothing and writes no
    substrate — the cycle host's Attend hook is live without a RegistryPort write.

    ``registry`` is accepted for hook-signature symmetry with ``run_schedule_step``
    (and the OLB-16 live ``attention_debt`` read/write that will use it); it is
    unread this iteration.
    """
    del registry  # OLB-16 live attention_debt read/write seam; unread at C3.
    state = config.attention_store.load()
    for escalation in config.incoming():
        state = intake_escalation(state, escalation)
    config.attention_store.save(state)
    plan = plan_notifications(
        state,
        now=config.clock(),
        quiet_hours=config.quiet_hours,
        batch_window=config.batch_window,
    )
    _deliver_notifications(config, plan)
    return plan


def _escalation_key(escalation: Escalation) -> tuple[str, str, str]:
    """The dedup ledger key for an escalation: project + gate + raised-at instant."""
    return (escalation.project_id, escalation.gate_id, escalation.raised_at.isoformat())


def _deliver_notifications(config: AttendConfig, plan: NotificationPlan) -> None:
    """Dispatch the planned batches through the notification port (Item 3).

    With no dedup ledger (``delivered_keys is None``) the plan is delivered as-is. With a ledger,
    each batch is filtered to escalations whose key is not already delivered; if any remain, the
    filtered plan is delivered and — only when the port reports a send (``> 0``, so the no-op
    NullNotificationPort never marks) — those keys are recorded so an unresolved escalation is
    paged once, not re-sent every cycle.
    """
    ledger = config.delivered_keys
    if ledger is None:
        config.notification_port.deliver(plan)
        return

    fresh_batches: list[NotificationBatch] = []
    fresh_keys: list[tuple[str, str, str]] = []
    for batch in plan.batches:
        fresh = tuple(e for e in batch.escalations if _escalation_key(e) not in ledger)
        if fresh:
            fresh_batches.append(replace(batch, escalations=fresh))
            fresh_keys.extend(_escalation_key(e) for e in fresh)
    if not fresh_batches:
        return
    filtered = NotificationPlan(batches=tuple(fresh_batches), deferred=plan.deferred)
    if config.notification_port.deliver(filtered) > 0:
        ledger.update(fresh_keys)


# --- §4.4 step-5 Guard composition (OLB-14 / Spec v1.3 §9 / §10 / §11) --------


@dataclass(frozen=True)
class StallSignal:
    """A detected stall for one running Project's Run (the Guard step's §11 input).

    Carries the candidate-repair ``kind`` discriminating the §11.3 repair class, the
    ``triggering_anomaly`` descriptor threaded into the :class:`RepairAction`, the
    ``confidence`` the Answerer attached, and the read-only-probed ``in_scope`` /
    ``safety_gate_refuses`` predicates the §11 policy consults (FR-045/048). The live
    source — a Run past ``hang_timeout`` with no ``orchestrator_pid`` progress — is the
    OLB-16 surface; the C4 anomaly drill supplies it deterministically (gate
    ``olb14-c4-anomaly-drill-substrate-and-fault-fidelity`` = A).
    """

    repair_kind: RepairKind
    triggering_anomaly: str
    confidence: float
    in_scope: bool = True
    safety_gate_refuses: bool = False


@dataclass(frozen=True)
class GuardConfig:
    """The §4.4 step-5 Guard collaborators (Spec v1.3 §9 / §10 / §11).

    Bundles what ``_guard`` composes: the FR-039 ``breaker_config`` thresholds and the
    ``spend_histories`` source the OLB-12 Cost Circuit-Breaker evaluates (per-Project,
    FR-043 isolation); the ``stall_signals`` source + ``confidence_threshold`` the
    OLB-13 Repair-Auto-OK Policy decides on (FR-044/045/046); the persisted
    :class:`AttentionStateStore` the FR-038 top-tier trip escalations are intaken into
    (FR-028/029); the injected ``clock`` (no wall-clock read inside the pure layers);
    and the ``project_filter`` scoping the running read. All carry build-time defaults,
    so a caller overrides only what it exercises; a Guard with no ``breaker_config`` and
    no stall signals is a complete no-op pass (the OLB-01 mechanical behaviour).
    """

    breaker_config: BreakerConfig | None = None
    spend_histories: Callable[[], Mapping[str, Sequence[IterationObservation]]] = field(
        default_factory=lambda: (lambda: {})
    )
    stall_signals: Callable[[], Mapping[str, StallSignal]] = field(
        default_factory=lambda: (lambda: {})
    )
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    attention_store: AttentionStateStore = field(default_factory=AttentionStateStore)
    clock: Callable[[], datetime] = field(
        default_factory=lambda: (lambda: datetime.now().astimezone())
    )
    project_filter: Callable[[RegistryRow], bool] = field(
        default_factory=lambda: (lambda row: True)
    )


@dataclass(frozen=True)
class GuardOutcome:
    """The decisions one :func:`run_guard_step` pass reached (Spec v1.3 §9 / §10 / §11).

    ``paused_projects`` are the Projects tripped to ``paused_safety`` this pass (FR-038 —
    never killed); ``breaker_trips`` the FR-039 Cost Circuit-Breaker trips applied;
    ``granted_repairs`` the FR-045 autonomously-granted reversible repairs (each Run
    continued, no trip); ``escalations`` the top-tier :class:`Escalation`\\ s intaken into
    the attention queue (FR-028/029). Returned for the checkpoint to assert against; the
    authoritative evidence is the live ``projects.lifecycle_state`` rows the trips wrote.
    """

    paused_projects: tuple[str, ...]
    breaker_trips: tuple[BreakerTrip, ...]
    granted_repairs: tuple[AutoRepairAuditRecord, ...]
    escalations: tuple[Escalation, ...]


def to_attention_escalation(
    safety_escalation: SafetyEscalation, *, raised_at: datetime
) -> Escalation:
    """Adapt an OLB-06 :class:`SafetyEscalation` into an OLB-10 :class:`Escalation`.

    The FR-038 trip returns a ``safety_gates.SafetyEscalation`` (``tier == "top_tier"``,
    ``lifecycle_state == "paused_safety"``); the §8 FR-028 intake consumes the
    ``attention.Escalation`` shape. This bridges them as a ``safety_gate``-kind
    escalation (one of :data:`~supervisor.attention.TOP_TIER_KINDS`, so it sorts top-tier
    and bypasses batching + Quiet Hours, FR-029) carrying no suggested option — a safety
    trip is the hard floor, not a one-confirm gate. ``raised_at`` is supplied (no
    wall-clock read here).
    """
    return Escalation(
        project_id=safety_escalation.project_id,
        gate_id=f"safety:{safety_escalation.project_id}",
        kind=ESCALATION_KIND_SAFETY_GATE,
        reversible=False,
        suggested_option=None,
        confidence=1.0,
        raised_at=raised_at,
    )


def _trip_and_intake(
    registry: RegistryPort,
    state: AttentionState,
    project_id: str,
    reason: str,
    raised_at: datetime,
) -> tuple[AttentionState, Escalation]:
    """Apply the single §9 FR-038 trip + FR-028 intake for one anomalous Project.

    Moves the Project to ``paused_safety`` through the OLB-06
    :func:`~supervisor.safety_gates.trip_to_paused_safety` write seam (the only
    substrate write the Guard performs — never a kill, never a terminal status), adapts
    the returned top-tier escalation, and intakes it into ``state`` (FR-028 — Attention
    Debt +1, queued). Returns the new :class:`AttentionState` and the intaken escalation.
    """
    safety_escalation = trip_to_paused_safety(registry, project_id, reason)
    escalation = to_attention_escalation(safety_escalation, raised_at=raised_at)
    return intake_escalation(state, escalation), escalation


def run_guard_step(registry: RegistryPort, config: GuardConfig) -> GuardOutcome:
    """Compose the OLB-12 breaker + OLB-13 repair policy + OLB-06 trip for one Guard pass.

    Reads the running fleet through the OLB-02 seam (scoped by ``config.project_filter``)
    and, in §9.3-precedence order:

    * **§10 Cost Circuit-Breaker (FR-039/043)** — evaluates each running Project's supplied
      spend history independently (:func:`~supervisor.cost_circuit_breaker.evaluate_fleet`);
      every trip moves that Project to ``paused_safety`` + a top-tier escalation (FR-038),
      and a clean sibling is left untouched (FR-043 isolation).
    * **§11 Repair-Auto-OK Policy (FR-044/045/046)** — for each still-running Project
      carrying a :class:`StallSignal`, classifies the candidate repair and decides
      grant-vs-escalate (:func:`~supervisor.repair_policy.evaluate_repair`): a reversible,
      in-scope, confidence-met repair the safety floor clears is granted autonomously
      (FR-045 — the Run continues, no trip, an FR-047 audit record is built); any other
      class (irreversible / out-of-scope / below-threshold / safety-refused) is escalated
      — the Project is tripped to ``paused_safety`` + a top-tier escalation (FR-046/038),
      never auto-executed.

    A Project already tripped by the breaker is never re-tripped by the repair arm (the
    illegal ``paused_safety -> paused_safety`` self-edge is structurally avoided, §5.3).
    No trip ever kills a Run or writes a terminal status — every trip is a pause pending
    operator decision (FR-038, the no-silent-kill invariant). Returns the
    :class:`GuardOutcome`; persists the updated attention state.
    """
    running = [row for row in registry.read_running() if config.project_filter(row)]
    running_ids = [str(row["project_id"]) for row in running]
    state = config.attention_store.load()
    paused: list[str] = []
    breaker_trips: list[BreakerTrip] = []
    granted: list[AutoRepairAuditRecord] = []
    escalations: list[Escalation] = []

    # --- §10 Cost Circuit-Breaker over the running fleet (FR-039 / FR-043) ---
    if config.breaker_config is not None:
        histories = {
            project_id: history
            for project_id, history in config.spend_histories().items()
            if project_id in running_ids
        }
        fleet_trips = evaluate_fleet(histories, config.breaker_config)
        for project_id in running_ids:
            trip = fleet_trips.get(project_id)
            if trip is not None and trip.tripped:
                breaker_trips.append(trip)
                state, escalation = _trip_and_intake(
                    registry, state, project_id, trip.detail, config.clock()
                )
                paused.append(project_id)
                escalations.append(escalation)

    # --- §11 Repair-Auto-OK Policy over the detected stalls (FR-044/045/046) ---
    signals = config.stall_signals()
    for project_id in running_ids:
        if project_id in paused:
            continue  # already tripped by the breaker — never double-trip (§5.3)
        signal = signals.get(project_id)
        if signal is None:
            continue
        action = RepairAction(
            kind=signal.repair_kind,
            project_id=project_id,
            triggering_anomaly=signal.triggering_anomaly,
            confidence=signal.confidence,
        )
        decision = evaluate_repair(
            action,
            confidence_threshold=config.confidence_threshold,
            in_scope=signal.in_scope,
            safety_gate_refuses=signal.safety_gate_refuses,
        )
        if decision.grant:
            # FR-045 autonomous reversible repair — the Run continues; record the FR-047
            # audit trail for the unattended action. No trip, no escalation.
            granted.append(build_audit_record(action, decision))
        else:
            # FR-046 — irreversible / out-of-scope / below-threshold / safety-refused:
            # escalate, never auto-execute. The stall is a safety condition, so the
            # Project is tripped to paused_safety with a top-tier escalation (FR-038).
            state, escalation = _trip_and_intake(
                registry, state, project_id, decision.rationale, config.clock()
            )
            paused.append(project_id)
            escalations.append(escalation)

    config.attention_store.save(state)
    return GuardOutcome(
        paused_projects=tuple(paused),
        breaker_trips=tuple(breaker_trips),
        granted_repairs=tuple(granted),
        escalations=tuple(escalations),
    )


# --- FR-036 fleet-wide Kill-Switch halt (OLB-16 / Spec v1.3 §9) ---------------


@dataclass(frozen=True)
class KillSwitchConfig:
    """The FR-036 fleet-wide Kill-Switch halt collaborators (Spec v1.3 §9.2).

    Bundles what :func:`run_kill_switch_halt` composes: the closed OLB-06
    :class:`~supervisor.safety_gates.KillSwitch` state primitive whose engaged flag
    decides the halt; the persisted :class:`AttentionStateStore` the per-Run
    safe-stop escalations are intaken into (FR-028/029); the ``reason`` recorded on
    each stop; the injected ``clock`` (no wall-clock read inside the pure layer); and
    the ``project_filter`` scoping the running read (accept-all in production; the
    disposable fleet in a checkpoint). All carry build-time defaults — a config with a
    disengaged KillSwitch is a complete no-op pass.
    """

    kill_switch: KillSwitch = field(default_factory=KillSwitch)
    attention_store: AttentionStateStore = field(default_factory=AttentionStateStore)
    reason: str = (
        "fleet-wide Kill-Switch engaged — Dispatch halted and every running Run "
        "signalled to a safe stop (FR-036)"
    )
    clock: Callable[[], datetime] = field(
        default_factory=lambda: (lambda: datetime.now().astimezone())
    )
    project_filter: Callable[[RegistryRow], bool] = field(
        default_factory=lambda: (lambda row: True)
    )


@dataclass(frozen=True)
class KillSwitchOutcome:
    """The result of one :func:`run_kill_switch_halt` pass (Spec v1.3 §9.2 FR-036).

    ``engaged`` is the Kill-Switch state this pass observed; ``dispatch_allowed`` is
    its negation — ``False`` while engaged means NO further Dispatch is issued
    fleet-wide (the §9.3 hard floor the OLB-06 :func:`check_dispatch_allowed` enforces
    at every spawn site). ``stopped_projects`` are the ``running`` Projects signalled
    to a safe stop this pass (each tripped to ``paused_safety`` — FR-038, never killed);
    ``escalations`` the top-tier :class:`Escalation`\\ s intaken for them. A disengaged
    pass returns ``engaged=False`` / ``dispatch_allowed=True`` and stops nothing.
    """

    engaged: bool
    dispatch_allowed: bool
    stopped_projects: tuple[str, ...]
    escalations: tuple[Escalation, ...]


def run_kill_switch_halt(
    registry: RegistryPort, config: KillSwitchConfig
) -> KillSwitchOutcome:
    """Apply the FR-036 fleet-wide Kill-Switch halt for one pass — composes closed primitives.

    When ``config.kill_switch`` is engaged (Spec v1.3 §9.2 FR-036 / §9.3 precedence):

    * **no further Dispatch fleet-wide** — the returned ``dispatch_allowed`` is
      ``False``; the live spawn block is the OLB-06 :func:`check_dispatch_allowed`
      already consulted by every admission spawn (an engaged KillSwitch refuses ALL
      Dispatch, overriding scheduler/admission state), so this halt issues no spawn and
      needs no new gate; and
    * **every running Run is signalled to a safe stop** — each ``running`` Project read
      through the OLB-02 seam (scoped by ``config.project_filter``) is tripped to
      ``paused_safety`` via the closed OLB-06 :func:`~supervisor.safety_gates.trip_to_paused_safety`
      write seam + intaken as a top-tier escalation (the same FR-038 trip the Guard
      uses). The trip is a PAUSE, never a kill and never a terminal ``failed`` status —
      the §9.2 no-silent-kill invariant (FR-038).

    Disengaged, it is a complete no-op: nothing is read-tripped, ``dispatch_allowed`` is
    ``True``. Composes the closed OLB-06 KillSwitch + FR-038 trip read-only; adds no new
    reconcile logic and reshapes no closed seam (gate ``olb16-c5-killswitch-fleet-halt-wiring-scope``
    = A). Persists the updated attention state; returns the :class:`KillSwitchOutcome`.
    """
    if not config.kill_switch.engaged:
        return KillSwitchOutcome(
            engaged=False,
            dispatch_allowed=True,
            stopped_projects=(),
            escalations=(),
        )

    running = [row for row in registry.read_running() if config.project_filter(row)]
    state = config.attention_store.load()
    stopped: list[str] = []
    escalations: list[Escalation] = []
    for row in running:
        project_id = str(row["project_id"])
        state, escalation = _trip_and_intake(
            registry, state, project_id, config.reason, config.clock()
        )
        stopped.append(project_id)
        escalations.append(escalation)
    config.attention_store.save(state)

    return KillSwitchOutcome(
        engaged=True,
        dispatch_allowed=False,
        stopped_projects=tuple(stopped),
        escalations=tuple(escalations),
    )


@dataclass(frozen=True)
class LearnConfig:
    """The §4.4 step-6 Learn collaborators (Spec v1.3 §12 FR-049–053 / D1).

    ``runs_source`` supplies the ``complete`` / ``failed`` :class:`RunRecord`s the
    cross-run audit reads (the production source reads the accumulated completed-Run
    corpus + Run Registry; default = no runs → the step is a no-op). ``audit_config``
    carries the FR-051/052 thresholds + the FR-049 cadence; ``report_sink`` receives
    the findings-only :class:`RunAuditReport` (default = drop; production logs/persists
    it). The Run-Auditor is strictly read-only (FR-053) — this step issues no registry
    write.
    """

    runs_source: Callable[[], "Sequence[AuditRunRecord]"]
    audit_config: AuditConfig = field(default_factory=AuditConfig)
    report_sink: Callable[["RunAuditReport"], None] = lambda _report: None


def stall_signals_from_actions(
    actions: "Sequence[ReconcileAction]",
    *,
    repair_kind: RepairKind = RepairKind.REATTACH_STALLED_RUN,
    confidence: float = 0.8,
    in_scope: bool = True,
    safety_gate_refuses: bool = False,
) -> dict[str, StallSignal]:
    """Bridge reconcile-detected STALLS into the Guard step's repair-intake (D3; FR-046).

    Converts each ``REASON_STALLED`` :class:`ReconcileAction` (a live Run past the hang
    budget, PID still alive) into the :class:`StallSignal` the OLB-13 Repair-Auto-OK
    policy evaluates — so the Guard step's stall source is fed from live reconcile
    detection instead of a supplied placeholder. Dead-PID actions are EXCLUDED (those
    are terminally reaped by the Reconcile step, not repair candidates). The
    classification fields default to a reversible re-attach posture; production supplies
    the Answerer-assessed ``confidence`` / ``in_scope`` / ``safety_gate_refuses``. The
    production wiring sets ``GuardConfig.stall_signals = lambda: stall_signals_from_actions(
    derive_reconcile_actions(...))``.
    """
    signals: dict[str, StallSignal] = {}
    for action in actions:
        if action.reason != REASON_STALLED:
            continue
        signals[action.project_id] = StallSignal(
            repair_kind=repair_kind,
            triggering_anomaly=action.reason,
            confidence=confidence,
            in_scope=in_scope,
            safety_gate_refuses=safety_gate_refuses,
        )
    return signals


def run_learn_step(config: LearnConfig) -> "RunAuditReport | None":
    """Compose the §4.4 step-6 Learn for one pass (D1; FR-049).

    Reads the supplied Runs, runs the read-only cross-run audit, and hands the
    findings-only report to the sink. A zero-run pass is a genuine no-op (returns
    None) — no audit, no write. The Run-Auditor never mutates the substrate (FR-053).
    """
    runs = list(config.runs_source())
    if not runs:
        return None
    report = run_audit_pass(runs, config=config.audit_config)
    config.report_sink(report)
    return report


__all__ = [
    "RoundStateStore",
    "AttentionStateStore",
    "ScheduleConfig",
    "AttendConfig",
    "GuardConfig",
    "GuardOutcome",
    "StallSignal",
    "KillSwitchConfig",
    "KillSwitchOutcome",
    "LearnConfig",
    "to_project_records",
    "to_attention_escalation",
    "run_schedule_step",
    "run_schedule_fill_step",
    "run_attend_step",
    "run_guard_step",
    "run_kill_switch_halt",
    "run_learn_step",
    "stall_signals_from_actions",
]
