"""Repair-Auto-OK Policy for the Outer Loop Supervisor (OLB-13).

A pure, DB-free decision/policy layer (Spec v1.3 §11) that governs *whether* a
detected anomaly's candidate repair may be carried out autonomously or must be
escalated to the single operator as a ``gate_human`` decision. Given a supplied
:class:`RepairAction` (the candidate repair + its triggering anomaly + the
confidence the Answerer attached) plus an injected ``confidence_threshold`` and the
read-only-probed in-scope / safety-gate-refusal predicates, it classifies the
repair's Reversibility Class, decides grant-vs-escalate, builds the FR-047
auto-repair audit record, and constructs the FR-046 ``gate_human`` escalation
payload. It performs no I/O, touches no database, sends no real escalation, reads no
wall-clock, and imports nothing from ``supervisor.registry``: it operates only on
supplied inputs. Resolved per gates ``olb13-repair-teardown-build-substrate``
(option A — DB-free) and ``olb13-repair-teardown-build-scope`` (option A — pure
layer, no closed-seam edit).

Spec mapping (§11.2):

* FR-044 Reversibility classification — :func:`classify_reversibility` assigns each
  §11.3 v1.0 repair class its default Reversibility Class BEFORE any execution
  decision (re-attach / reconcile / resume / re-spawn -> ``reversible``;
  discard-or-rewrite committed work-registry closures or ``state\\`` content ->
  ``irreversible``).
* FR-045 Autonomous grant — :func:`evaluate_repair` grants a repair autonomously
  IFF it is ``reversible`` AND ``in_scope`` AND its ``confidence`` is at least the
  supplied ``confidence_threshold`` (all three) AND the safety gate does not refuse
  it.
* FR-046 ``gate_human`` escalation — :func:`evaluate_repair` escalates (never
  auto-executes) any repair that is ``irreversible`` OR out-of-scope OR below the
  confidence threshold; :func:`build_repair_escalation` constructs the
  :class:`supervisor.attention.Escalation` the §8 FR-028 intake consumes.
* FR-047 Auto-repair audit trail — :func:`build_audit_record` records the action,
  its Reversibility Class, the triggering anomaly, and the rationale so an operator
  can audit any unattended (granted) repair after the fact.
* FR-048 Safety-gate precedence (§9.3) — the supplied ``safety_gate_refuses``
  predicate (a read-only ``safety_gates`` result the caller supplies) overrides an
  otherwise-grantable action: a reversible, in-scope, high-confidence repair the
  safety floor refuses is escalated, never granted.

§11 boundary: this module DECIDES; it performs no repair, spawns nothing, sends no
escalation, and writes no lifecycle state. The live wiring of these decisions into
the ``supervisor/cycle.py`` §4.4 step-5 Guard / step-6 Learn hooks, the live
``supervisor.attention.intake_escalation`` route, and the live ``safety_gates``
consult are the C4/OLB-14 anomaly-drills integration; the auto-refreshing surfacing
is OLB-16.

Seam alignment (read-only probed at iter-0027): the FR-046 escalation reuses the
OLB-10 :class:`supervisor.attention.Escalation` shape unchanged (so the §8 FR-028
intake consumes it as it would any ``gate_human`` escalation); a triggering anomaly
descriptor traces to an OLB-12 :class:`supervisor.cost_circuit_breaker.BreakerTrip`
or an OLB-06 :class:`supervisor.safety_gates.SafetyEscalation`, threaded in as a
supplied string and never re-derived here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from supervisor.attention import ESCALATION_KIND_ROUTINE, Escalation

# --- Constants ---------------------------------------------------------------

#: The seed ``gate_policy.confidence_threshold`` (seed v1.6.2 = 0.7) the FR-045 /
#: FR-046 grant boundary is parameterised against. Documented here as the default;
#: the live value is always supplied by the caller (no seed read inside this module).
DEFAULT_CONFIDENCE_THRESHOLD = 0.7


# --- FR-044: reversibility classification ------------------------------------


class ReversibilityClass(Enum):
    """A repair's Reversibility Class (Spec v1.3 §11.2 FR-044 / §11.3).

    ``REVERSIBLE`` repairs (re-attach / reconcile / resume / re-spawn) restore a
    Run to a known prior state and may be granted autonomously; ``IRREVERSIBLE``
    repairs (discarding or rewriting committed work-registry closures or ``state\\``
    content) destroy committed state and must always be escalated.
    """

    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class RepairKind(Enum):
    """The §11.3 v1.0 in-scope repair classes the policy classifies.

    The first four restore a Run to a prior known state (default ``reversible``); the
    last destroys committed state (default ``irreversible``). :func:`classify_reversibility`
    keys on this discriminator via :data:`_DEFAULT_REVERSIBILITY`.
    """

    REATTACH_STALLED_RUN = "reattach_stalled_run"
    RECONCILE_EXITED_UNRECONCILED_RUN = "reconcile_exited_unreconciled_run"
    RESUME_PAUSED_RUN = "resume_paused_run"
    RESPAWN_FAILED_AT_SPAWN_RUN = "respawn_failed_at_spawn_run"
    DISCARD_OR_REWRITE_COMMITTED = "discard_or_rewrite_committed"


#: The §11.3 default Reversibility Class per repair class. Assigned BEFORE any
#: grant/escalate decision (FR-044): the four restore-to-prior-state repairs are
#: ``reversible``; discarding or rewriting committed closures / ``state\\`` content
#: is ``irreversible``.
_DEFAULT_REVERSIBILITY: dict[RepairKind, ReversibilityClass] = {
    RepairKind.REATTACH_STALLED_RUN: ReversibilityClass.REVERSIBLE,
    RepairKind.RECONCILE_EXITED_UNRECONCILED_RUN: ReversibilityClass.REVERSIBLE,
    RepairKind.RESUME_PAUSED_RUN: ReversibilityClass.REVERSIBLE,
    RepairKind.RESPAWN_FAILED_AT_SPAWN_RUN: ReversibilityClass.REVERSIBLE,
    RepairKind.DISCARD_OR_REWRITE_COMMITTED: ReversibilityClass.IRREVERSIBLE,
}


# --- Value objects -----------------------------------------------------------


@dataclass(frozen=True)
class RepairAction:
    """A candidate repair the policy decides on (Spec v1.3 §11.2).

    Carries the ``kind`` discriminating the §11.3 repair class, the owning
    ``project_id``, the ``triggering_anomaly`` descriptor (a supplied string tracing
    to an OLB-12 :class:`~supervisor.cost_circuit_breaker.BreakerTrip` ``kind`` /
    detail or an OLB-06 :class:`~supervisor.safety_gates.SafetyEscalation` reason —
    never re-derived here), and the ``confidence`` the Answerer attached to the
    suggested repair. Frozen — the policy derives decisions, never mutates the action.
    """

    kind: RepairKind
    project_id: str
    triggering_anomaly: str
    confidence: float


@dataclass(frozen=True)
class RepairDecision:
    """The policy's decision for one :class:`RepairAction` (Spec v1.3 §11.2).

    ``grant`` true is an autonomous-execution grant (FR-045 — all of reversible,
    in-scope, confidence met, and safety floor clear). ``escalate`` true is a §11
    FR-046 ``gate_human`` escalation (irreversible, out-of-scope, below-threshold, or
    safety-refused, FR-048). The two are mutually exclusive. ``reversibility`` is the
    FR-044 class; ``rationale`` is the human-readable reason; ``escalation`` carries
    the FR-046 :class:`~supervisor.attention.Escalation` payload when an escalating
    decision was built with a ``raised_at`` timestamp (``None`` otherwise — the
    caller may build it separately via :func:`build_repair_escalation`). Truthy iff
    granted, so callers may write ``if decision:``.
    """

    grant: bool
    escalate: bool
    reversibility: ReversibilityClass
    rationale: str
    escalation: Escalation | None = None

    def __bool__(self) -> bool:
        return self.grant


@dataclass(frozen=True)
class AutoRepairAuditRecord:
    """The FR-047 audit-trail record for an autonomously-executed repair.

    Carries the granted ``action``, its ``reversibility`` class, the
    ``triggering_anomaly`` that prompted it, and the ``rationale`` — the four facts
    an operator needs to audit an unattended action after the fact. Built only for a
    granted repair (:func:`build_audit_record`).
    """

    action: RepairAction
    reversibility: ReversibilityClass
    triggering_anomaly: str
    rationale: str


# --- FR-044: classification --------------------------------------------------


def classify_reversibility(action: RepairAction) -> ReversibilityClass:
    """Return the §11.3 default Reversibility Class for ``action`` (Spec v1.3 FR-044).

    Assigned purely from the repair ``kind`` BEFORE any grant/escalate decision: the
    four restore-to-prior-state repairs are :attr:`ReversibilityClass.REVERSIBLE`;
    discarding or rewriting committed work-registry closures or ``state\\`` content is
    :attr:`ReversibilityClass.IRREVERSIBLE`.
    """
    return _DEFAULT_REVERSIBILITY[action.kind]


# --- FR-045 / FR-046 / FR-048: grant-vs-escalate decision --------------------


def evaluate_repair(
    action: RepairAction,
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    in_scope: bool,
    safety_gate_refuses: bool,
    raised_at: datetime | None = None,
) -> RepairDecision:
    """Decide whether ``action`` may be carried out autonomously (Spec v1.3 §11.2).

    Grants autonomously (FR-045) IFF the repair is
    :attr:`ReversibilityClass.REVERSIBLE` AND ``in_scope`` AND ``action.confidence >=
    confidence_threshold`` AND NOT ``safety_gate_refuses``. Otherwise escalates as a
    ``gate_human`` decision (FR-046) — irreversible, out-of-scope, below-threshold,
    or refused by the §9 safety floor (FR-048 precedence, §9.3) — and never
    auto-executes.

    ``safety_gate_refuses`` is a supplied read-only ``safety_gates`` result; it is
    evaluated as a hard override so a refused repair is escalated even when it would
    otherwise be grantable (§9.3). ``confidence_threshold`` defaults to the seed value
    but is supplied by the caller (no seed read here). When ``raised_at`` is supplied
    and the decision escalates, the FR-046 :class:`~supervisor.attention.Escalation`
    payload is attached; otherwise the caller builds it via
    :func:`build_repair_escalation`. DECIDES only — performs no repair and sends no
    escalation.
    """
    reversibility = classify_reversibility(action)
    confidence_met = action.confidence >= confidence_threshold
    would_grant = (
        reversibility is ReversibilityClass.REVERSIBLE and in_scope and confidence_met
    )

    if safety_gate_refuses:
        decision = RepairDecision(
            grant=False,
            escalate=True,
            reversibility=reversibility,
            rationale=(
                f"safety gate refuses repair {action.kind.value!r} for "
                f"{action.project_id!r}; escalating to gate_human despite "
                "reversible/in-scope/high-confidence status (§9.3 precedence, FR-048)."
            ),
        )
    elif would_grant:
        return RepairDecision(
            grant=True,
            escalate=False,
            reversibility=reversibility,
            rationale=(
                f"granting reversible repair {action.kind.value!r} for "
                f"{action.project_id!r}: in scope and confidence "
                f"{action.confidence} >= {confidence_threshold} (FR-045)."
            ),
        )
    else:
        reasons = []
        if reversibility is ReversibilityClass.IRREVERSIBLE:
            reasons.append("irreversible")
        if not in_scope:
            reasons.append("out of scope")
        if not confidence_met:
            reasons.append(
                f"confidence {action.confidence} < {confidence_threshold}"
            )
        decision = RepairDecision(
            grant=False,
            escalate=True,
            reversibility=reversibility,
            rationale=(
                f"escalating repair {action.kind.value!r} for {action.project_id!r} "
                f"to gate_human ({'; '.join(reasons)}, FR-046)."
            ),
        )

    if raised_at is not None:
        escalation = build_repair_escalation(action, decision, raised_at=raised_at)
        return RepairDecision(
            grant=decision.grant,
            escalate=decision.escalate,
            reversibility=decision.reversibility,
            rationale=decision.rationale,
            escalation=escalation,
        )
    return decision


# --- FR-047: auto-repair audit trail -----------------------------------------


def build_audit_record(
    action: RepairAction, decision: RepairDecision
) -> AutoRepairAuditRecord:
    """Build the FR-047 audit-trail record for a granted repair (Spec v1.3 §11.2).

    Produces an :class:`AutoRepairAuditRecord` carrying the action, its Reversibility
    Class, the triggering anomaly, and the rationale so an operator can audit the
    unattended action after the fact. Intended for an autonomously-granted repair —
    raises :class:`ValueError` for a decision that did not grant (an escalated repair
    is never executed unattended, so it has no auto-repair audit record).
    """
    if not decision.grant:
        raise ValueError(
            "cannot build an auto-repair audit record for a repair that was not "
            f"granted (project_id={action.project_id!r}, kind={action.kind.value!r})"
        )
    return AutoRepairAuditRecord(
        action=action,
        reversibility=decision.reversibility,
        triggering_anomaly=action.triggering_anomaly,
        rationale=decision.rationale,
    )


# --- FR-046: gate_human escalation payload -----------------------------------


def build_repair_escalation(
    action: RepairAction, decision: RepairDecision, *, raised_at: datetime
) -> Escalation:
    """Build the FR-046 ``gate_human`` escalation for an escalated repair (Spec §11.2).

    Constructs a :class:`supervisor.attention.Escalation` the §8 FR-028 intake
    consumes unchanged — a ``routine`` ``gate_human`` decision carrying the repair as
    the ``suggested_option`` (so the attention scheduler's one-confirm path can offer
    it only when reversible), the repair ``confidence``, and the FR-044 reversibility
    threaded into ``reversible``. ``raised_at`` is supplied (this module reads no
    wall-clock). Raises :class:`ValueError` for a decision that did not escalate (a
    granted repair has no escalation). Builds the payload only — does NOT call
    ``intake_escalation`` (that live route is OLB-14).
    """
    if not decision.escalate:
        raise ValueError(
            "cannot build a repair escalation for a granted (non-escalated) repair "
            f"(project_id={action.project_id!r}, kind={action.kind.value!r})"
        )
    return Escalation(
        project_id=action.project_id,
        gate_id=f"repair:{action.kind.value}:{action.project_id}",
        kind=ESCALATION_KIND_ROUTINE,
        reversible=decision.reversibility is ReversibilityClass.REVERSIBLE,
        suggested_option=action.kind.value,
        confidence=action.confidence,
        raised_at=raised_at,
    )


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "AutoRepairAuditRecord",
    "RepairAction",
    "RepairDecision",
    "RepairKind",
    "ReversibilityClass",
    "build_audit_record",
    "build_repair_escalation",
    "classify_reversibility",
    "evaluate_repair",
]
