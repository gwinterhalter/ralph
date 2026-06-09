"""Cross-Project Scheduler for the Outer Loop Supervisor (OLB-09).

A pure, DB-free decision/policy layer (Spec v1.3 §7). Given the fleet's supplied
Project records and the set of Projects with an in-flight Dispatch, it selects the
next runnable Project to receive an orchestrator Dispatch — priority-weighted,
closest-to-done-biased, and starvation-guarded — and returns the selection plus an
updated round-state. It performs no I/O, touches no database, and imports nothing
from ``supervisor.registry``: it operates only on supplied inputs. Resolved per
gates ``olb09-scheduler-build-substrate`` (option A — DB-free) and
``olb09-scheduler-build-scope`` (option A — pure layer, no closed-seam edit).

Spec mapping (§7.2):

* FR-022 Runnable-set derivation — :func:`derive_runnable` keeps only Projects in a
  runnable lifecycle state (``admitted`` with ceiling headroom to spawn, or
  ``running`` awaiting their next iteration) and drops any in ``paused_gate`` /
  ``paused_budget`` / ``paused_safety`` / ``complete`` / ``failed``.
* FR-027 Dispatch idempotency — :func:`derive_runnable` also excludes any Project
  whose ``project_id`` is in ``in_flight_project_ids``, preserving the
  at-most-one-active-Run invariant (FR-007).
* FR-023 Priority-weighted selection — :func:`select_next_dispatch` prefers the
  highest ``priority`` (the §5.2 FR-002 weight).
* FR-024 Closest-to-done bias — a tie on the top priority breaks toward the fewest
  ``open_work_count`` (the §13 FR-059 work-registry open-count, threaded in as a
  supplied record field, NOT a live query this iteration).
* FR-025 Starvation guard — a runnable Project skipped for at least
  ``starvation_threshold`` rounds is promoted ahead of higher-priority peers for one
  Dispatch; its skip-count then resets to 0.
* FR-026 Gate-blocked skip without demotion — ``priority`` is a read-only input the
  scheduler never mutates; a gate-blocked Project simply drops out of the runnable
  set (FR-022) and re-appears at its unchanged priority once runnable again. Its
  round-state skip-count is left untouched while it is blocked — the counter tracks
  *runnable* skips, not gate-blocked rounds, so a block never demotes it.

§7.3 non-goals: the scheduler allocates whole Dispatches between Projects; it does
not preempt a mid-iteration Run, parallelise within a single Project, or make
completion decisions (those are the orchestrator's, via INITIATIVE_COMPLETE).

Forward references (C3/OLB-11 fleet-scheduling integration, NOT this iteration): the
live wiring of this module into the ``supervisor/cycle.py`` §4.4 step-3 Schedule
hook, persistence of ``round_state`` across rounds, the live ``work_registry``
open-count source feeding ``open_work_count``, and seed/config wiring of
:data:`STARVATION_ROUND_THRESHOLD`.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# --- Constants ---------------------------------------------------------------

#: FR-025 — the configured number of scheduling rounds a runnable Project may be
#: skipped before the Starvation Guard promotes it ahead of higher-priority peers
#: for one Dispatch. A sensible default; seed/config wiring is deferred to OLB-11.
STARVATION_ROUND_THRESHOLD = 5

#: The two lifecycle states a Project may be dispatched from (Spec v1.3 §7.2
#: FR-022). An ``admitted`` Project is spawned (needs ceiling headroom for a new
#: concurrency slot); a ``running`` Project awaiting its next iteration is resumed
#: (consumes no new slot). Every other state — ``paused_gate`` / ``paused_budget`` /
#: ``paused_safety`` / ``complete`` / ``failed`` (the OLB-02 ``transitions`` set) —
#: is non-runnable and excluded from selection.
ADMITTED_STATE = "admitted"
RUNNING_STATE = "running"
RUNNABLE_LIFECYCLE_STATES = frozenset({ADMITTED_STATE, RUNNING_STATE})

#: Dispatch kinds (§7.1): an ``admitted`` Project is spawned into a new Run; a
#: ``running`` Project is resumed for its next iteration.
DISPATCH_SPAWN = "spawn"
DISPATCH_RESUME = "resume"

#: Selection reason codes — why a particular Project was chosen this round.
REASON_PRIORITY = "priority"
REASON_CLOSEST_TO_DONE = "closest_to_done"
REASON_STARVATION = "starvation_promotion"
#: The closed set of reasons a :class:`SchedulerDecision` may carry.
SELECTION_REASONS = frozenset(
    {REASON_PRIORITY, REASON_CLOSEST_TO_DONE, REASON_STARVATION}
)


# --- Value objects -----------------------------------------------------------


@dataclass(frozen=True)
class ProjectRecord:
    """A supplied Project record the scheduler ranks (Spec v1.3 §5.2 / §7).

    A frozen value object carrying exactly the fields §7 selection keys on. The
    field names mirror the Registry ``projects`` columns the OLB-02 ``RegistryPort``
    reads (``project_id`` / ``lifecycle_state`` / ``priority``) plus the FR-024 /
    §13 FR-059 work-registry open-count (``open_work_count``), threaded in as a
    supplied figure rather than read live this iteration. The OLB-11 wiring step
    adapts each ``RegistryRow`` mapping into one of these.
    """

    project_id: str
    lifecycle_state: str
    priority: int
    open_work_count: int


@dataclass(frozen=True)
class SchedulerDecision:
    """The scheduler's selection for one Dispatch (Spec v1.3 §7).

    Carries the selected ``project_id``, the ``reason`` it was chosen (one of
    :data:`SELECTION_REASONS`), the ``dispatch_kind`` (:data:`DISPATCH_SPAWN` for an
    ``admitted`` Project, :data:`DISPATCH_RESUME` for a ``running`` one), and the
    ``round_state`` updated for the next round (selected Project reset to 0; each
    other runnable, not-selected Project incremented by 1; gate-blocked Projects'
    counters left untouched per FR-026).
    """

    project_id: str
    reason: str
    dispatch_kind: str
    round_state: Mapping[str, int]


# --- FR-022 / FR-027: runnable-set derivation --------------------------------


def derive_runnable(
    projects: Iterable[ProjectRecord],
    *,
    in_flight_project_ids: Iterable[str],
    ceiling_headroom: int,
) -> list[ProjectRecord]:
    """Return the Projects eligible for the next Dispatch (Spec v1.3 §7.2 FR-022 + FR-027).

    Keeps only runnable Projects: a ``running`` Project awaiting its next iteration
    (resumed — consumes no new concurrency slot), or an ``admitted`` Project when
    there is ceiling headroom to spawn one (``ceiling_headroom >= 1``). Every Project
    in ``paused_gate`` / ``paused_budget`` / ``paused_safety`` / ``complete`` /
    ``failed`` is excluded (FR-022). Any Project whose ``project_id`` is in
    ``in_flight_project_ids`` is also excluded, preserving the at-most-one-active-Run
    invariant (FR-027 / FR-007). Input order is preserved.
    """
    in_flight = frozenset(in_flight_project_ids)
    runnable: list[ProjectRecord] = []
    for project in projects:
        if project.project_id in in_flight:
            continue
        if project.lifecycle_state == RUNNING_STATE:
            runnable.append(project)
        elif project.lifecycle_state == ADMITTED_STATE and ceiling_headroom >= 1:
            runnable.append(project)
    return runnable


# --- Selection ranking + round-state helpers ---------------------------------


def _priority_rank_key(project: ProjectRecord) -> tuple[int, int, str]:
    """Total ordering for FR-023/FR-024 selection: highest ``priority`` first, then
    fewest ``open_work_count`` (closest-to-done), then ``project_id`` for a stable,
    deterministic tie-break. The smaller tuple sorts first, so ``priority`` is
    negated to put the highest priority at the front."""
    return (-project.priority, project.open_work_count, project.project_id)


def _starvation_rank_key(
    project: ProjectRecord, round_state: Mapping[str, int]
) -> tuple[int, int, int, str]:
    """Total ordering among starved Projects (FR-025): most-skipped first, then the
    normal priority / closest-to-done / id ordering for a deterministic choice."""
    skipped = round_state.get(project.project_id, 0)
    return (-skipped, -project.priority, project.open_work_count, project.project_id)


def _dispatch_kind(project: ProjectRecord) -> str:
    """:data:`DISPATCH_SPAWN` for an ``admitted`` Project, else :data:`DISPATCH_RESUME`."""
    return DISPATCH_SPAWN if project.lifecycle_state == ADMITTED_STATE else DISPATCH_RESUME


def _next_round_state(
    runnable: Sequence[ProjectRecord],
    selected_id: str,
    round_state: Mapping[str, int],
) -> dict[str, int]:
    """Return the round-state for the next round (FR-025 / FR-026).

    Starts from a copy of ``round_state`` so gate-blocked (non-runnable) Projects'
    skip-counts carry forward untouched (FR-026 — a block never demotes). The
    selected Project resets to 0; every other runnable Project is incremented by 1
    (it was skipped this round). The input mapping is not mutated.
    """
    updated = dict(round_state)
    for project in runnable:
        if project.project_id == selected_id:
            updated[project.project_id] = 0
        else:
            updated[project.project_id] = updated.get(project.project_id, 0) + 1
    return updated


# --- FR-023 / FR-024 / FR-025: next-Dispatch selection -----------------------


def select_next_dispatch(
    projects: Iterable[ProjectRecord],
    *,
    in_flight_project_ids: Iterable[str],
    round_state: Mapping[str, int],
    ceiling_headroom: int,
    starvation_threshold: int = STARVATION_ROUND_THRESHOLD,
) -> SchedulerDecision | None:
    """Select the next Project to Dispatch, or ``None`` when none is runnable.

    Derives the runnable set (FR-022 + FR-027), then selects:

    * FR-025 first — if any runnable Project has been skipped for at least
      ``starvation_threshold`` rounds, the most-skipped such Project is promoted
      ahead of higher-priority peers for this one Dispatch (reason
      :data:`REASON_STARVATION`).
    * FR-023 otherwise — the highest-``priority`` runnable Project (reason
      :data:`REASON_PRIORITY`).
    * FR-024 — a tie on the top priority breaks toward the fewest ``open_work_count``
      (reason :data:`REASON_CLOSEST_TO_DONE`).

    Returns a :class:`SchedulerDecision` carrying the selection, its reason, the
    dispatch kind, and the updated ``round_state`` (selected Project reset to 0; each
    other runnable Project incremented; gate-blocked Projects untouched, FR-026).
    Returns ``None`` when the runnable set is empty — no Dispatch, no exception.
    """
    runnable = derive_runnable(
        projects,
        in_flight_project_ids=in_flight_project_ids,
        ceiling_headroom=ceiling_headroom,
    )
    if not runnable:
        return None

    starved = [
        project
        for project in runnable
        if round_state.get(project.project_id, 0) >= starvation_threshold
    ]
    if starved:
        selected = min(starved, key=lambda p: _starvation_rank_key(p, round_state))
        reason = REASON_STARVATION
    else:
        selected = min(runnable, key=_priority_rank_key)
        # The selection was a closest-to-done tie-break iff more than one runnable
        # Project shares the winning top priority; otherwise it won on priority alone.
        contended = sum(1 for p in runnable if p.priority == selected.priority)
        reason = REASON_CLOSEST_TO_DONE if contended > 1 else REASON_PRIORITY

    return SchedulerDecision(
        project_id=selected.project_id,
        reason=reason,
        dispatch_kind=_dispatch_kind(selected),
        round_state=_next_round_state(runnable, selected.project_id, round_state),
    )
