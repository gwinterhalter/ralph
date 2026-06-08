"""Learning effect-measurement (Effect-Measurement Loop spec).

Measures whether an ADOPTED (applied) Run-Auditor finding actually helped, by comparing a per-kind
effect signal across the subject's runs BEFORE vs AFTER the adoption instant (the events table is
the evidence). Pure: no I/O, no DB, no wall-clock — the ``__main__`` wiring reads the runs/events,
scopes them to the subject's relevant projects, splits them at ``applied_at`` (the ``ralph_runs``
run window, D2), and this module computes the before/after metrics + the outcome.

Per-kind signal (one value per run; ``None`` = the run is not evidence and is excluded):

* ``answerer_dsl_candidate`` (gate): of runs where the gate FIRED, fraction escalated to a human →
  after a DSL rule it should auto-resolve, so after-rate ≈ 0. ``None`` when the gate did not fire.
* ``verification_binding:binding_defect`` (binding): the binding's pass-rate in the run → after a
  fix it should pass. ``None`` when the binding was not run.
* ``verification_binding:over_verification`` (binding): PRESENCE (1/0) of the binding → after removal
  it is absent (spend saved). Never ``None`` (absence is the measured goal). **Limitation (D3): a
  drop to 0 proves spend saved, NOT that no real defect now slips — recorded in ``detail``.**
* ``session_shape`` (shape): revision-rate for the shape → after a tune it should fall below the
  FR-052 fraction. ``None`` when the shape was not used.
* ``correction_pattern`` (item): PRESENCE (1/0) of a correction_attempt for the item → after a fix
  the item stops entering the loop. Never ``None`` (absence is the goal).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Event-type / payload literals (mirror the emitters; kept local so this module imports no privates).
_GATE_FIRE = "gate_fire"
_GATE_ESCALATE = "gate_escalate"
_VERIFICATION = "verification"
_REVISE_ROUND = "revise_round"
_CORRECTION_ATTEMPT = "correction_attempt"
_PASS_TOKENS = frozenset({"pass", "passed", "ok", "success", "true"})

#: Finding-kind values (mirror supervisor.run_auditor.FindingKind / BindingFindingClass).
_K_GATE = "answerer_dsl_candidate"
_K_BINDING = "verification_binding"
_K_SHAPE = "session_shape"
_K_CORRECTION = "correction_pattern"
_BC_OVER = "over_verification"
_BC_DEFECT = "binding_defect"

_EPS = 0.05
DEFAULT_MIN_POST_RUNS = 3

# Outcomes.
PENDING = "pending"
CONFIRMED = "confirmed"
NO_EFFECT = "no_effect"
REGRESSED = "regressed"


@dataclass(frozen=True)
class EffectRecord:
    """The measured effect of one applied finding (one recomputed row in run_audit_effects)."""

    finding_key: str
    kind: str
    subject: str
    applied_at: str | None
    before_metric: float | None
    after_metric: float | None
    post_adoption_runs: int
    outcome: str
    detail: str


def _payload(event: dict[str, object]) -> dict[str, object]:
    p = event.get("payload")
    return p if isinstance(p, dict) else {}


def _gate_id(event: dict[str, object]) -> object:
    return event.get("subject_id") or _payload(event).get("gate_id")


def _measure_kind(kind: str, binding_class: str | None) -> str:
    """Normalise a finding's kind+binding_class into the measurement key."""
    if kind == _K_BINDING and binding_class == _BC_OVER:
        return "over_verification"
    if kind == _K_BINDING and binding_class == _BC_DEFECT:
        return "binding_defect"
    return kind


def run_signal(measure_kind: str, subject: str, events: "list[dict[str, object]] | tuple[dict[str, object], ...]") -> float | None:
    """The per-run effect signal for one subject (``None`` = run is not evidence). Pure; never raises."""
    if measure_kind == _K_GATE:
        fired = escalated = False
        for e in events:
            if _gate_id(e) != subject:
                continue
            et = e.get("event_type")
            if et in (_GATE_FIRE, _GATE_ESCALATE):
                fired = True
            if et == _GATE_ESCALATE or (et == _GATE_FIRE and _payload(e).get("cls") == "gate_human"):
                escalated = True
        return (1.0 if escalated else 0.0) if fired else None

    if measure_kind == "binding_defect":
        passes = total = 0
        for e in events:
            if e.get("event_type") != _VERIFICATION:
                continue
            if (e.get("subject_id") or _payload(e).get("binding")) != subject:
                continue
            total += 1
            result = _payload(e).get("result")
            passed = _payload(e).get("passed")
            ok = passed is True or (isinstance(result, str) and result.strip().lower() in _PASS_TOKENS)
            passes += 1 if ok else 0
        return (passes / total) if total else None

    if measure_kind == "over_verification":
        present = any(
            e.get("event_type") == _VERIFICATION
            and (e.get("subject_id") or _payload(e).get("binding")) == subject
            for e in events
        )
        return 1.0 if present else 0.0  # presence — absence (0) is the measured goal

    if measure_kind == _K_SHAPE:
        used = needed = False
        for e in events:
            if e.get("event_type") != _REVISE_ROUND or _payload(e).get("shape") != subject:
                continue
            used = True
            pl = _payload(e)
            verdict, fb, fd = pl.get("verdict"), pl.get("findings_blocker"), pl.get("findings_drift")
            if (verdict not in (None, "converged")) or (isinstance(fb, int) and not isinstance(fb, bool) and fb > 0) or (isinstance(fd, int) and not isinstance(fd, bool) and fd > 0):
                needed = True
        return (1.0 if needed else 0.0) if used else None

    if measure_kind == _K_CORRECTION:
        present = any(
            e.get("event_type") == _CORRECTION_ATTEMPT
            and (e.get("subject_id") or _payload(e).get("item_id")) == subject
            for e in events
        )
        return 1.0 if present else 0.0  # presence — absence (0) is the measured goal

    return None


# Per-measure-kind direction + goal predicate.
_KIND_RULES: dict[str, tuple[bool, Callable[[float], bool]]] = {
    _K_GATE: (False, lambda a: a <= _EPS),               # lower better; goal: no longer escalates
    "binding_defect": (True, lambda a: a >= 0.5),         # higher better; goal: now passing
    "over_verification": (False, lambda a: a <= _EPS),    # lower better; goal: binding gone
    _K_SHAPE: (False, lambda a: a < 0.5),                 # lower better; goal: below FR-052 fraction
    _K_CORRECTION: (False, lambda a: a <= _EPS),          # lower better; goal: stops needing correction
}


def _event_references_subject(measure_kind: str, subject: str, event: dict[str, object]) -> bool:
    """True iff this single event is ABOUT the subject (used to scope relevant projects)."""
    et = event.get("event_type")
    if measure_kind == _K_GATE:
        return et in (_GATE_FIRE, _GATE_ESCALATE) and _gate_id(event) == subject
    if measure_kind in ("binding_defect", "over_verification"):
        return et == _VERIFICATION and (event.get("subject_id") or _payload(event).get("binding")) == subject
    if measure_kind == _K_SHAPE:
        return et == _REVISE_ROUND and _payload(event).get("shape") == subject
    if measure_kind == _K_CORRECTION:
        return et == _CORRECTION_ATTEMPT and (event.get("subject_id") or _payload(event).get("item_id")) == subject
    return False


def relevant_project_ids(
    kind: str,
    subject: str,
    events: "list[dict[str, object]] | tuple[dict[str, object], ...]",
    *,
    binding_class: str | None = None,
) -> set[str]:
    """Projects whose events reference the subject (scopes measurement; avoids dilution by unrelated runs)."""
    measure_kind = _measure_kind(kind, binding_class)
    return {
        pid
        for e in events
        if isinstance((pid := e.get("project_id")), str) and pid
        and _event_references_subject(measure_kind, subject, e)
    }


def measure_effect(
    kind: str,
    subject: str,
    *,
    before_runs: "list[list[dict[str, object]]]",
    after_runs: "list[list[dict[str, object]]]",
    binding_class: str | None = None,
    finding_key: str = "",
    applied_at: str | None = None,
    min_post_runs: int = DEFAULT_MIN_POST_RUNS,
) -> EffectRecord:
    """Compute the before/after effect of one applied finding (pure).

    ``before_runs`` / ``after_runs`` are the subject's relevant runs (each a list of that run's
    events), split at ``applied_at`` by the wiring. Returns an :class:`EffectRecord` with the metrics
    + outcome (``pending`` until ``>= min_post_runs`` relevant post-adoption runs)."""
    mk = _measure_kind(kind, binding_class)
    before_vals = [s for ev in before_runs if (s := run_signal(mk, subject, ev)) is not None]
    after_vals = [s for ev in after_runs if (s := run_signal(mk, subject, ev)) is not None]
    before = (sum(before_vals) / len(before_vals)) if before_vals else None
    after = (sum(after_vals) / len(after_vals)) if after_vals else None
    post = len(after_vals)

    higher_better, goal = _KIND_RULES.get(mk, (False, lambda a: False))
    if post < min_post_runs or before is None or after is None:
        outcome = PENDING
        detail = f"pending: {post} post-adoption run(s) (need {min_post_runs}); before={before} after={after}"
    else:
        improved = (after > before) if higher_better else (after < before)
        worse = (after < before - _EPS) if higher_better else (after > before + _EPS)
        if goal(after) and improved:
            outcome = CONFIRMED
        elif worse:
            outcome = REGRESSED
        else:
            outcome = NO_EFFECT
        detail = f"{mk}: before={before:.3f} after={after:.3f} over {post} post-run(s)"
        if mk == "over_verification" and outcome == CONFIRMED:
            detail += " — spend saved; cannot prove no defect now slips (D3 limitation)"
    return EffectRecord(
        finding_key=finding_key,
        kind=kind,
        subject=subject,
        applied_at=applied_at,
        before_metric=before,
        after_metric=after,
        post_adoption_runs=post,
        outcome=outcome,
        detail=detail,
    )


__all__ = [
    "EffectRecord",
    "PENDING",
    "CONFIRMED",
    "NO_EFFECT",
    "REGRESSED",
    "DEFAULT_MIN_POST_RUNS",
    "run_signal",
    "relevant_project_ids",
    "measure_effect",
]
