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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from supervisor.admission import (
    AdmissionRejection,
    AdmittedHold,
    ReconciledFailure,
    RunRecord,
    SeedValidatorPort,
    SpawnPort,
    admit_candidate,
)
from supervisor.attention import (
    AttentionState,
    Escalation,
    NotificationPlan,
    QuietHours,
    intake_escalation,
    plan_notifications,
)
from supervisor.safety_gates import DEFAULT_CONCURRENCY_CEILING, KillSwitch
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
DispatchOutcome = RunRecord | ReconciledFailure | AdmittedHold | AdmissionRejection


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
    """Default candidate enricher — returns the row unchanged.

    The live ``_schedule`` spawn arm needs the §6 admission-input fields
    (``seed_path`` / ``open_item_count`` / ``writable_paths`` / ``mcp_roots`` /
    ``read_only_paths``) that ``read_candidates`` does not surface; the caller
    supplies an enricher that merges them in (the C2 ``_enriched_candidate``
    pattern). The identity default is only ever used when no spawn occurs.
    """
    return row


def _utc_now_iso() -> str:
    """Default admission ``spawned_at`` clock — an ISO-8601 UTC timestamp."""
    return datetime.now().astimezone().isoformat()


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
    starvation_threshold: int = STARVATION_ROUND_THRESHOLD
    round_state_store: RoundStateStore = field(default_factory=RoundStateStore)
    open_work_counts: Mapping[str, int] = field(default_factory=dict)
    candidate_enricher: Callable[[RegistryRow], RegistryRow] = _identity_enricher
    clock: Callable[[], str] = _utc_now_iso
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
    candidates = [row for row in registry.read_candidates() if config.project_filter(row)]
    running = [row for row in registry.read_running() if config.project_filter(row)]
    running_count = len(running)
    in_flight = [str(row["project_id"]) for row in running]

    records = to_project_records(
        candidates, running, open_work_counts=config.open_work_counts
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
        _spawn_selected(registry, config, decision.project_id, candidates, running_count)
    # The resume arm (a `running` Project's next iteration) is OLB-16; it is never
    # selected here because every `running` Project is FR-027 in-flight-excluded.
    return decision


def _spawn_selected(
    registry: RegistryPort,
    config: ScheduleConfig,
    project_id: str,
    candidates: Sequence[RegistryRow],
    running_count: int,
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
    return plan_notifications(
        state,
        now=config.clock(),
        quiet_hours=config.quiet_hours,
        batch_window=config.batch_window,
    )


__all__ = [
    "RoundStateStore",
    "AttentionStateStore",
    "ScheduleConfig",
    "AttendConfig",
    "to_project_records",
    "run_schedule_step",
    "run_attend_step",
]
