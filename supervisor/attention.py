"""Operator-Attention Scheduler for the Outer Loop Supervisor (OLB-10).

A pure, DB-free decision/policy layer (Spec v1.3 §8) that governs *when and how*
the single operator is interrupted. Given the supplied set of unresolved
``gate_human`` escalations and an injected clock + config, it tiers escalations by
urgency, batches the routine ones, suppresses non-top-tier notifications during
Quiet Hours (deferring them loss-free), decides one-confirmation eligibility, and
signals return-to-runnable on resolution. It performs no I/O, touches no database,
sends no real notification, reads no wall-clock, and imports nothing from
``supervisor.registry``: it operates only on supplied inputs. Resolved per gates
``olb10-attention-scheduler-build-substrate`` (option A — DB-free) and
``olb10-attention-scheduler-build-scope`` (option A — pure layer, no closed-seam edit).

Spec mapping (§8.2):

* FR-028 Escalation intake — :func:`intake_escalation` increments the owning
  Project's Attention Debt (the §5.2 FR-004 ``attention_debt`` counter) by 1 and
  queues the escalation for operator attention.
* FR-029 Urgency tiering — :func:`assign_urgency_tier` assigns a tier, with a top
  tier reserved for kill-switch / safety-gate events; :func:`plan_notifications`
  orders top-tier first and surfaces it immediately, bypassing batching + Quiet Hours.
* FR-030 Batching — :func:`plan_notifications` collapses the non-top-tier escalations
  due in the current window into a single :class:`NotificationBatch`.
* FR-031 Quiet Hours — :func:`plan_notifications` suppresses all but top-tier during
  the supplied :class:`QuietHours` window and defers the suppressed escalations to the
  next active window without dropping them (they stay queued and surface in the next
  active-window batch).
* FR-032 Auto-pick-on-suggested — :func:`auto_pick_eligible` is true iff the
  escalation carries a suggested option at confidence ``>= confidence_threshold`` and
  is reversible; :func:`build_one_confirm_offer` builds the single-confirm accept payload.
* FR-033 Debt decrement on resolution — :func:`resolve_escalation` removes the
  resolved escalation, decrements the Project's Attention Debt (floored at 0), and
  signals return-to-runnable when the Debt reaches 0 and no other block applies.

§8.3 boundary: this module consumes escalations the orchestrator's Answerer has
already routed to ``gate_human`` (carrying its suggested option + confidence); it
never classifies or re-derives them.

Seam alignment (read-only probed at iter-0024, not imported here to keep the layer
standalone): the §5.2 FR-004 counter is the ``projects.attention_debt`` column
(``supervisor/registry.py`` allowlist, ``supervisor/status_surface.py``); a top-tier
escalation corresponds to ``safety_gates.SafetyEscalation`` (``tier == "top_tier"``,
the FR-038 trip) or the FR-036 Kill-Switch; the FR-033 return-to-runnable target is
the legal ``paused_gate -> running`` edge (``supervisor/transitions.py``).

Forward references (NOT this iteration): the live wiring of this module into the
``supervisor/cycle.py`` §4.4 step-4 Attend hook, persisted attention / Quiet-Hours
state, and live ``attention_debt`` reads/writes via the ``RegistryPort`` are the
C3/OLB-11 fleet-scheduling integration; the real ``gmail_smtp:default`` notification
dispatch + auto-refreshing surfacing is OLB-16; the repair-policy intake interplay
(§11 FR-046 -> FR-028) is OLB-13.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import IntEnum

# --- Constants ---------------------------------------------------------------

#: Escalation kinds — the discriminator :func:`assign_urgency_tier` keys on. A
#: ``safety_gate`` escalation is the FR-038 ``SafetyEscalation`` trip; ``kill_switch``
#: is the FR-036 global Kill-Switch event; everything else is a ``routine``
#: ``gate_human`` decision. The two top-tier kinds bypass batching + Quiet Hours.
ESCALATION_KIND_SAFETY_GATE = "safety_gate"
ESCALATION_KIND_KILL_SWITCH = "kill_switch"
ESCALATION_KIND_ROUTINE = "routine"

#: The kinds that map to the top urgency tier (Spec v1.3 §8 FR-029 — bypass
#: batching + Quiet Hours). Mirrors the ``safety_gates`` top-tier escalation
#: (``ESCALATION_TIER_TOP``) + the FR-036 Kill-Switch.
TOP_TIER_KINDS = frozenset({ESCALATION_KIND_SAFETY_GATE, ESCALATION_KIND_KILL_SWITCH})

#: The seed ``gate_policy.confidence_threshold`` (seed v1.6.2 = 0.7) the FR-032
#: one-confirm path is parameterised against. The default documented here; the live
#: value is always supplied by the caller (no seed read inside this module).
DEFAULT_CONFIDENCE_THRESHOLD = 0.7

#: The lifecycle state a Project returns to when its Attention Debt clears (Spec v1.3
#: §5.3 ``paused_gate -> running`` legal edge, ``supervisor/transitions.py``). This
#: module only *signals* the return (FR-033); the live transition is OLB-11's.
RETURN_TO_RUNNABLE_STATE = "running"


# --- Urgency tier (FR-029) ---------------------------------------------------


class UrgencyTier(IntEnum):
    """Operator-notification urgency tier (Spec v1.3 §8 FR-029).

    An :class:`enum.IntEnum` so the lower value sorts first: :attr:`TOP` (kill-switch
    / safety-gate — bypasses batching + Quiet Hours) precedes :attr:`ROUTINE`
    (batched, Quiet-Hours-deferrable) under the natural ordering used by
    :func:`plan_notifications`.
    """

    TOP = 0
    ROUTINE = 1


# --- Value objects -----------------------------------------------------------


@dataclass(frozen=True)
class Escalation:
    """A single unresolved ``gate_human`` escalation the operator owes a decision on.

    Carries the owning ``project_id`` and ``gate_id``, the ``kind`` discriminator
    (one of :data:`ESCALATION_KIND_SAFETY_GATE` / :data:`ESCALATION_KIND_KILL_SWITCH`
    / :data:`ESCALATION_KIND_ROUTINE`), the Answerer-supplied ``suggested_option`` +
    ``confidence`` and reversibility class the FR-032 one-confirm path reads, and the
    ``raised_at`` timestamp. Frozen — intake/resolution return new state, never mutate.
    """

    project_id: str
    gate_id: str
    kind: str
    reversible: bool
    suggested_option: str | None
    confidence: float
    raised_at: datetime


@dataclass(frozen=True)
class AttentionState:
    """The per-Project Attention Debt counters + the unresolved-escalation queue.

    ``attention_debt`` maps ``project_id -> debt`` (the §5.2 FR-004 count of unresolved
    ``gate_human`` escalations); ``queue`` is the FIFO of unresolved
    :class:`Escalation`\\ s. Both are read-only snapshots — :func:`intake_escalation`
    and :func:`resolve_escalation` return a new :class:`AttentionState` rather than
    mutating the supplied one.
    """

    attention_debt: Mapping[str, int]
    queue: tuple[Escalation, ...]

    @classmethod
    def empty(cls) -> AttentionState:
        """An :class:`AttentionState` with no debt and no queued escalations."""
        return cls(attention_debt={}, queue=())

    def debt_for(self, project_id: str) -> int:
        """The current Attention Debt for ``project_id`` (0 when unknown)."""
        return self.attention_debt.get(project_id, 0)


@dataclass(frozen=True)
class NotificationBatch:
    """One operator-facing notification (Spec v1.3 §8 FR-029 / FR-030).

    A top-tier batch carries exactly one escalation, surfaced immediately and alone
    (``window_start`` / ``window_end`` are ``None`` — it bypasses the batch window). A
    routine batch carries every routine escalation due in the ``[window_start,
    window_end]`` window collapsed into a single notification (FR-030 — one per window,
    not one per escalation).
    """

    tier: UrgencyTier
    escalations: tuple[Escalation, ...]
    window_start: datetime | None = None
    window_end: datetime | None = None


@dataclass(frozen=True)
class NotificationPlan:
    """The ordered notification plan the caller would later hand to the live channel.

    ``batches`` are ordered by tier (top-tier first, then the single routine batch if
    any); ``deferred`` carries the routine escalations suppressed by Quiet Hours
    (FR-031) — they remain queued in the :class:`AttentionState` and surface in the
    next active-window plan, so the plan never drops them.
    """

    batches: tuple[NotificationBatch, ...]
    deferred: tuple[Escalation, ...]


@dataclass(frozen=True)
class OneConfirmOffer:
    """The FR-032 single-confirmation accept payload for an eligible escalation.

    Identifies the ``gate_id`` / ``project_id`` and the ``suggested_option`` a single
    operator confirmation would resolve the gate with. This module *builds* the offer;
    the actual gate-write on confirm is the orchestrator / OLB-16 surface.
    """

    gate_id: str
    project_id: str
    suggested_option: str


@dataclass(frozen=True)
class QuietHours:
    """An operator-configured Quiet-Hours window (Spec v1.3 §3 / §8 FR-031).

    A clock-time ``[start, end)`` window. When ``start <= end`` the window is the same
    day; when ``start > end`` it wraps past midnight (e.g. 22:00 -> 07:00). Compared
    against the supplied ``now`` only — this object reads no wall-clock itself.
    """

    start: time
    end: time

    def contains(self, now: datetime) -> bool:
        """True iff ``now``'s clock time falls within the Quiet-Hours window."""
        current = now.time()
        if self.start <= self.end:
            return self.start <= current < self.end
        return current >= self.start or current < self.end


# --- FR-029: urgency tiering --------------------------------------------------


def assign_urgency_tier(escalation: Escalation) -> UrgencyTier:
    """Return the urgency tier for ``escalation`` (Spec v1.3 §8 FR-029).

    :attr:`UrgencyTier.TOP` iff the escalation is a kill-switch / safety-gate event
    (its ``kind`` is in :data:`TOP_TIER_KINDS`); otherwise :attr:`UrgencyTier.ROUTINE`.
    """
    if escalation.kind in TOP_TIER_KINDS:
        return UrgencyTier.TOP
    return UrgencyTier.ROUTINE


# --- FR-028: escalation intake ------------------------------------------------


def intake_escalation(state: AttentionState, escalation: Escalation) -> AttentionState:
    """Intake one ``gate_human`` escalation (Spec v1.3 §8 FR-028).

    Returns a new :class:`AttentionState` with the owning Project's Attention Debt
    incremented by 1 (the §5.2 FR-004 counter) and the escalation appended to the
    operator-attention queue. Pure — the supplied ``state`` is not mutated; the live
    ``attention_debt`` write via the ``RegistryPort`` is OLB-11.
    """
    updated_debt = dict(state.attention_debt)
    updated_debt[escalation.project_id] = updated_debt.get(escalation.project_id, 0) + 1
    return AttentionState(
        attention_debt=updated_debt,
        queue=state.queue + (escalation,),
    )


# --- FR-029 / FR-030 / FR-031: notification planning --------------------------


def plan_notifications(
    state: AttentionState,
    *,
    now: datetime,
    quiet_hours: QuietHours | None,
    batch_window: timedelta,
) -> NotificationPlan:
    """Plan operator notifications for the queued escalations (Spec v1.3 §8 FR-029–031).

    * FR-029 — top-tier escalations (kill-switch / safety-gate) are ordered first and
      each surfaced immediately and alone, bypassing both batching and Quiet Hours.
    * FR-030 — the non-top-tier (routine) escalations due in the current
      ``[now - batch_window, now]`` window collapse into a single
      :class:`NotificationBatch`, not one per escalation.
    * FR-031 — when ``quiet_hours`` is supplied and ``now`` falls inside it, the routine
      escalations are suppressed (no routine batch) and returned in ``deferred``; they
      remain queued in ``state`` and surface in the next active-window plan, so none is
      lost. Top-tier is never deferred.

    Returns a :class:`NotificationPlan`; an empty queue yields empty batches + empty
    deferred (no exception). Writes nothing and sends nothing — the live dispatch is OLB-16.
    """
    top: list[Escalation] = []
    routine: list[Escalation] = []
    for escalation in state.queue:
        bucket = top if assign_urgency_tier(escalation) is UrgencyTier.TOP else routine
        bucket.append(escalation)

    batches: list[NotificationBatch] = [
        NotificationBatch(tier=UrgencyTier.TOP, escalations=(escalation,))
        for escalation in sorted(top, key=lambda e: (e.raised_at, e.gate_id))
    ]

    in_quiet_hours = quiet_hours is not None and quiet_hours.contains(now)
    deferred: tuple[Escalation, ...] = ()
    if routine:
        if in_quiet_hours:
            deferred = tuple(routine)
        else:
            batches.append(
                NotificationBatch(
                    tier=UrgencyTier.ROUTINE,
                    escalations=tuple(routine),
                    window_start=now - batch_window,
                    window_end=now,
                )
            )

    return NotificationPlan(batches=tuple(batches), deferred=deferred)


# --- FR-032: auto-pick-on-suggested ------------------------------------------


def auto_pick_eligible(
    escalation: Escalation,
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> bool:
    """True iff ``escalation`` qualifies for the FR-032 one-confirmation accept path.

    Eligible iff it carries an Answerer-suggested option (``suggested_option is not
    None``) AND its ``confidence >= confidence_threshold`` AND it is ``reversible``.
    The threshold defaults to the seed value (:data:`DEFAULT_CONFIDENCE_THRESHOLD`) but
    is supplied by the caller — no seed read here.
    """
    return (
        escalation.suggested_option is not None
        and escalation.confidence >= confidence_threshold
        and escalation.reversible
    )


def build_one_confirm_offer(escalation: Escalation) -> OneConfirmOffer:
    """Build the FR-032 single-confirm accept payload for ``escalation``.

    Constructs the :class:`OneConfirmOffer` a single operator confirmation would resolve
    the gate with. Requires a suggested option — raises :class:`ValueError` when
    ``escalation.suggested_option is None`` (call :func:`auto_pick_eligible` first). This
    builds the offer only; the gate-write on confirm is the orchestrator / OLB-16 surface.
    """
    if escalation.suggested_option is None:
        raise ValueError(
            "cannot build a one-confirm offer for an escalation with no "
            f"suggested_option (gate_id={escalation.gate_id!r})"
        )
    return OneConfirmOffer(
        gate_id=escalation.gate_id,
        project_id=escalation.project_id,
        suggested_option=escalation.suggested_option,
    )


# --- FR-033: debt decrement + return-to-runnable signal -----------------------


def resolve_escalation(
    state: AttentionState,
    *,
    project_id: str,
    gate_id: str,
    project_otherwise_blocked: bool,
) -> tuple[AttentionState, bool]:
    """Resolve one escalation (Spec v1.3 §8 FR-033).

    Removes the escalation matching ``(project_id, gate_id)`` from the queue, decrements
    that Project's Attention Debt by 1 (floored at 0 — never negative), and returns
    ``(new_state, returns_runnable)`` where ``returns_runnable`` is ``True`` iff an
    escalation was actually resolved AND the Project's Debt has reached 0 AND
    ``project_otherwise_blocked`` is ``False`` (no other block applies). Resolving an
    unknown escalation is a no-op floored at 0 (no negative debt, no exception,
    ``returns_runnable`` ``False``).

    Pure — the supplied ``state`` is not mutated. The caller (OLB-11) performs the live
    ``paused_gate -> running`` (:data:`RETURN_TO_RUNNABLE_STATE`) transition; this module
    only signals it.
    """
    remaining = tuple(
        e
        for e in state.queue
        if not (e.project_id == project_id and e.gate_id == gate_id)
    )
    resolved_count = len(state.queue) - len(remaining)

    updated_debt = dict(state.attention_debt)
    current = updated_debt.get(project_id, 0)
    new_debt = max(0, current - resolved_count)
    updated_debt[project_id] = new_debt

    new_state = AttentionState(attention_debt=updated_debt, queue=remaining)
    returns_runnable = (
        resolved_count > 0 and new_debt == 0 and not project_otherwise_blocked
    )
    return new_state, returns_runnable


# --- Construction helper ------------------------------------------------------


def build_state(
    *,
    attention_debt: Mapping[str, int] | None = None,
    queue: Iterable[Escalation] = (),
) -> AttentionState:
    """Build an :class:`AttentionState` from supplied debt + queued escalations.

    A small convenience for callers (and tests) that assemble an initial state; the
    runtime path uses :func:`intake_escalation` to accrue debt one escalation at a time.
    """
    return AttentionState(
        attention_debt=dict(attention_debt or {}),
        queue=tuple(queue),
    )
