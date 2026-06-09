"""Cost Circuit-Breaker — a per-Project spend-*shape* anomaly detector (OLB-12).

A pure, DB-free decision layer implementing Spec v1.3 §10. It is explicitly **not**
a dollar cap (§2.3 / §10.1): it never blocks a Dispatch because cumulative or
per-iteration spend reached some planned number, and exposes **no** ``cap`` /
``max_usd`` / ``budget_limit`` / HALT-at-$N parameter anywhere. Instead it observes
the *shape* of a Project's spend across iterations and trips only on pathological
behaviour — a single-iteration blow-out, spend accruing with no closures, or the
same work-registry target looping without ever closing.

It performs no I/O, touches no database, reads no wall-clock, calls no
``set_lifecycle_state``, and imports nothing from ``supervisor.registry``: it
operates only on supplied :class:`IterationObservation` records plus a supplied
:class:`BreakerConfig`. All money is :class:`~decimal.Decimal` (NFR-007), never
float. Resolved per gates ``olb12-cost-circuit-breaker-build-substrate`` (option A —
DB-free) and ``olb12-cost-circuit-breaker-build-scope`` (option A — pure layer, no
closed-seam edit).

Spec mapping (§10.2):

* FR-039 single-iteration spend-delta — :func:`detect_spend_delta_anomaly` trips when
  the latest iteration's spend exceeds ``spend_delta_multiple`` times the trailing-
  median iteration spend (:func:`trailing_median_spend`).
* FR-040 spend-without-closure — :func:`detect_spend_without_closure` trips when the
  ``open`` work-registry count does not decrease across ``spend_without_closure_k``
  consecutive iterations while spend accrues in each of them.
* FR-041 target-loop — :func:`detect_target_loop` trips when the same work-registry
  ``target_id`` is re-attempted across ``target_loop_j`` iterations without ever
  closing, and names the looping target on the trip (``looping_target``).
* FR-042 non-binding cost surfacing — :func:`summarize_cost` returns cumulative +
  per-iteration cost as operator information ONLY; nothing in this module blocks a
  Dispatch on a dollar threshold.
* FR-043 per-Project isolation — :func:`evaluate_fleet` evaluates each Project's
  history independently, so a trip on one Project can never affect another's result.

A trip returns a :class:`BreakerTrip` *decision* carrying the intent to move the
Project to ``paused_safety`` plus a top-tier escalation payload — reusing the OLB-06
:class:`supervisor.safety_gates.SafetyEscalation` (``tier == "top_tier"``,
``lifecycle_state == "paused_safety"``, ``killed is False``), the §10.1 mirror of the
§9 FR-038 safety-gate trip — so the eventual live intake consumes it unchanged.

Forward references (NOT this iteration): the live wiring of this module into the
``supervisor/cycle.py`` §4.4 step-5 Guard hook, the live
``set_lifecycle_state('paused_safety')`` trip-write, the escalation routing into the
``supervisor/attention.py`` FR-029 top-tier intake, and the live spend / open-count
source (real ``ralph_runs`` cost rows + work-registry reads) are the C4/OLB-14
anomaly-drills integration; the repair-policy / teardown interplay is OLB-13. This
module returns decisions; it acts on none of them.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from supervisor.safety_gates import PAUSED_SAFETY, SafetyEscalation

# --- Value objects -----------------------------------------------------------


@dataclass(frozen=True)
class IterationObservation:
    """One observed iteration of a single Project's Run (Spec v1.3 §10.2).

    The breaker's sole input record: the ``project_id`` the iteration belongs to, its
    ``iteration_index``, the ``spend_usd`` it cost (:class:`~decimal.Decimal`, NFR-007),
    the Project's ``open`` work-registry count at the end of the iteration
    (``open_count``, the §5.2 attention/open-work figure FR-040 reads), the
    work-registry ``target_id`` the iteration attempted (``None`` when no single target
    was in play), and whether that target closed (``target_closed``). Frozen — the
    detectors derive decisions, never mutate observations.
    """

    project_id: str
    iteration_index: int
    spend_usd: Decimal
    open_count: int
    target_id: str | None = None
    target_closed: bool = False


@dataclass(frozen=True)
class BreakerConfig:
    """The supplied thresholds the detectors are parameterised against (Spec §10.3).

    Every threshold is **supplied** by the caller — the breaker reads no seed and sets
    no default of its own (the documented values trace to operator/seed config but are
    passed in, not read here, so §10.3 holds: the breaker neither reads
    ``seed.budget`` as a cap nor overrides it). There is deliberately **no**
    ``cap`` / ``max_usd`` / ``budget_limit`` field — the breaker trips on spend
    *shape*, never on spend reaching a number (§2.3 / §10.1).

    * ``spend_delta_multiple`` — FR-039: how many times the trailing-median spend the
      latest iteration must exceed to be a single-iteration anomaly.
    * ``trailing_window`` — FR-039: how many preceding iterations form the trailing
      median baseline.
    * ``spend_without_closure_k`` — FR-040: the count of consecutive iterations with no
      open-count decrease (while spending) that constitutes a stall.
    * ``target_loop_j`` — FR-041: the count of iterations re-attempting one target
      without closing it that constitutes a loop.
    """

    spend_delta_multiple: Decimal
    trailing_window: int
    spend_without_closure_k: int
    target_loop_j: int


class TripKind(Enum):
    """Which §10.2 anomaly a :class:`BreakerTrip` fired on."""

    SPEND_DELTA_ANOMALY = "spend_delta_anomaly"
    SPEND_WITHOUT_CLOSURE = "spend_without_closure"
    TARGET_LOOP = "target_loop"


@dataclass(frozen=True)
class BreakerTrip:
    """The breaker's decision for one Project (Spec v1.3 §10.1).

    ``tripped`` true with a non-null ``kind`` is an anomaly decision: the breaker's
    *intent* is to move the Project to ``paused_safety`` and raise the top-tier
    ``escalation`` (the §10.1 mirror of the §9 FR-038 trip). ``tripped`` false is a
    clean result (``kind``/``escalation`` ``None``). ``looping_target`` names the
    offending work-registry target on a FR-041 :attr:`TripKind.TARGET_LOOP` trip and is
    ``None`` otherwise. Truthy iff tripped, so callers may write ``if trip:``.

    No live ``set_lifecycle_state`` is performed here — the decision is returned for the
    eventual C4/OLB-14 Guard-hook to apply.
    """

    tripped: bool
    kind: TripKind | None = None
    project_id: str | None = None
    detail: str = ""
    looping_target: str | None = None
    escalation: SafetyEscalation | None = None

    def __bool__(self) -> bool:
        return self.tripped

    @classmethod
    def no_trip(cls, project_id: str | None = None) -> BreakerTrip:
        """A clean (no-anomaly) decision for ``project_id``."""
        return cls(tripped=False, project_id=project_id)

    @classmethod
    def trip(
        cls,
        *,
        kind: TripKind,
        project_id: str,
        detail: str,
        looping_target: str | None = None,
    ) -> BreakerTrip:
        """An anomaly decision carrying the ``paused_safety`` top-tier escalation.

        The ``escalation`` reuses the OLB-06 :class:`SafetyEscalation` unchanged
        (``lifecycle_state == "paused_safety"``, ``tier == "top_tier"``,
        ``killed is False``) so the eventual live intake (C4/OLB-14) consumes it as it
        would any §9 FR-038 safety-gate trip.
        """
        return cls(
            tripped=True,
            kind=kind,
            project_id=project_id,
            detail=detail,
            looping_target=looping_target,
            escalation=SafetyEscalation(project_id=project_id, reason=detail),
        )


@dataclass(frozen=True)
class CostSummary:
    """Non-binding cumulative + per-iteration cost for one Project (Spec §10.2 FR-042).

    Operator information ONLY. The breaker surfaces these figures for visibility; **no
    function in this module ever blocks or refuses a Dispatch on a dollar threshold**
    (§2.3 / §10.1 — the no-dollar-cap invariant). ``cumulative_usd`` is the sum of
    every observed iteration's spend; ``per_iteration_usd`` is the per-iteration series
    in observation order. Money is :class:`~decimal.Decimal` (NFR-007).
    """

    project_id: str
    cumulative_usd: Decimal
    per_iteration_usd: tuple[Decimal, ...] = field(default_factory=tuple)


# --- Shared window helper -----------------------------------------------------


def _recent_window(
    history: Sequence[IterationObservation], window: int
) -> Sequence[IterationObservation] | None:
    """The trailing ``window`` observations of ``history``, or ``None``.

    Returns ``None`` for a non-positive ``window`` or a history shorter than it —
    the "insufficient history, no trip" precondition the K/J detectors share.
    """
    if window <= 0 or len(history) < window:
        return None
    return history[-window:]


# --- FR-039: single-iteration spend-delta anomaly ----------------------------


def trailing_median_spend(
    history: Sequence[IterationObservation], *, window: int
) -> Decimal:
    """Median spend over the trailing ``window`` observations of ``history``.

    The FR-039 baseline. Uses the last ``window`` observations of the supplied slice
    (callers pass the history *excluding* the iteration under test). Returns
    ``Decimal(0)`` for a non-positive ``window`` or an empty slice — a degenerate
    baseline the caller treats as "insufficient history, no anomaly" (so there is
    never a divide-by-zero or a false trip against a zero median).
    """
    if window <= 0 or not history:
        return Decimal(0)
    trailing = sorted(obs.spend_usd for obs in history[-window:])
    count = len(trailing)
    mid = count // 2
    if count % 2 == 1:
        return trailing[mid]
    return (trailing[mid - 1] + trailing[mid]) / Decimal(2)


def detect_spend_delta_anomaly(
    history: Sequence[IterationObservation], config: BreakerConfig
) -> BreakerTrip | None:
    """FR-039: trip when the latest iteration's spend blows out the trailing median.

    Trips iff there are at least ``config.trailing_window`` iterations *preceding* the
    latest one, that trailing median is positive, and the latest iteration's
    ``spend_usd`` is strictly greater than ``config.spend_delta_multiple`` times it. A
    spend within the threshold — or insufficient history, or a zero baseline — does not
    trip (returns ``None``).
    """
    preceding = history[:-1]
    if len(preceding) < config.trailing_window:
        return None
    median = trailing_median_spend(preceding, window=config.trailing_window)
    if median <= 0:
        return None
    latest = history[-1]
    threshold = config.spend_delta_multiple * median
    if latest.spend_usd > threshold:
        return BreakerTrip.trip(
            kind=TripKind.SPEND_DELTA_ANOMALY,
            project_id=latest.project_id,
            detail=(
                f"iteration {latest.iteration_index} spend {latest.spend_usd} "
                f"exceeds {config.spend_delta_multiple}x trailing median {median} "
                f"(> {threshold}) over the trailing {config.trailing_window} "
                f"iterations; pausing to {PAUSED_SAFETY}."
            ),
        )
    return None


# --- FR-040: spend accruing without any closure ------------------------------


def detect_spend_without_closure(
    history: Sequence[IterationObservation], config: BreakerConfig
) -> BreakerTrip | None:
    """FR-040: trip when spend accrues across K iterations with no open-count decrease.

    Examines the last ``config.spend_without_closure_k`` iterations. Trips iff the
    ``open_count`` never decreases across them (every consecutive pair holds or grows —
    a decrease would be progress) AND each iteration spent (``spend_usd > 0``). A window
    in which the open count decreases at any step, or any iteration with no spend, does
    not trip (returns ``None``). Insufficient history returns ``None``.
    """
    window = config.spend_without_closure_k
    recent = _recent_window(history, window)
    if recent is None:
        return None
    open_never_decreases = all(
        recent[i + 1].open_count >= recent[i].open_count for i in range(len(recent) - 1)
    )
    spend_accrues = all(obs.spend_usd > 0 for obs in recent)
    if open_never_decreases and spend_accrues:
        latest = recent[-1]
        accrued = sum((obs.spend_usd for obs in recent), Decimal(0))
        return BreakerTrip.trip(
            kind=TripKind.SPEND_WITHOUT_CLOSURE,
            project_id=latest.project_id,
            detail=(
                f"spend {accrued} accrued across {window} consecutive iterations "
                f"(ending {latest.iteration_index}) while open work-count did not "
                f"decrease (held at >= {recent[0].open_count}); pausing to "
                f"{PAUSED_SAFETY}."
            ),
        )
    return None


# --- FR-041: same target re-attempted without closing ------------------------


def detect_target_loop(
    history: Sequence[IterationObservation], config: BreakerConfig
) -> BreakerTrip | None:
    """FR-041: trip when one target is re-attempted across J iterations without closing.

    Examines the last ``config.target_loop_j`` iterations. Trips iff they all attempted
    the **same** non-``None`` ``target_id`` and none of them closed it
    (``target_closed`` is false throughout). The trip names that target in
    ``looping_target``. A window mixing targets, carrying a ``None`` target, or in which
    the target closes does not trip (returns ``None``). Insufficient history returns
    ``None``.
    """
    window = config.target_loop_j
    recent = _recent_window(history, window)
    if recent is None:
        return None
    target = recent[0].target_id
    if target is None:
        return None
    same_target = all(obs.target_id == target for obs in recent)
    never_closed = not any(obs.target_closed for obs in recent)
    if same_target and never_closed:
        latest = recent[-1]
        return BreakerTrip.trip(
            kind=TripKind.TARGET_LOOP,
            project_id=latest.project_id,
            detail=(
                f"target {target!r} re-attempted across {window} iterations "
                f"(through {latest.iteration_index}) without closing; pausing to "
                f"{PAUSED_SAFETY}."
            ),
            looping_target=target,
        )
    return None


# --- Composite per-Project + fleet evaluation --------------------------------

#: The deterministic order the composite :func:`evaluate` applies the detectors in;
#: the first to trip wins. Single-iteration blow-outs are the most acute signal, then
#: a spending stall, then a target loop.
_DETECTOR_PRECEDENCE = (
    detect_spend_delta_anomaly,
    detect_spend_without_closure,
    detect_target_loop,
)


def evaluate(
    history: Sequence[IterationObservation], config: BreakerConfig
) -> BreakerTrip:
    """Evaluate ONE Project's iteration history (Spec v1.3 §10.2).

    Runs the three detectors in :data:`_DETECTOR_PRECEDENCE` order and returns the
    first trip, or a clean :meth:`BreakerTrip.no_trip` when none fires. Operates on a
    single Project's history only; an empty history is a clean no-trip.
    """
    project_id = history[-1].project_id if history else None
    for detector in _DETECTOR_PRECEDENCE:
        trip = detector(history, config)
        if trip is not None:
            return trip
    return BreakerTrip.no_trip(project_id)


def evaluate_fleet(
    histories_by_project: Mapping[str, Sequence[IterationObservation]],
    config: BreakerConfig,
) -> dict[str, BreakerTrip]:
    """Evaluate every Project independently (Spec v1.3 §10.2 FR-043).

    Returns ``{project_id: BreakerTrip}`` where each entry is computed from that
    Project's history alone (via :func:`evaluate`). A trip on one Project therefore
    yields a trip only for its own key — no other Project's result is affected, the
    per-Project isolation FR-043 requires.
    """
    return {
        project_id: evaluate(history, config)
        for project_id, history in histories_by_project.items()
    }


# --- FR-042: non-binding cost surfacing --------------------------------------


def summarize_cost(history: Sequence[IterationObservation]) -> CostSummary:
    """Surface cumulative + per-iteration cost for one Project (Spec §10.2 FR-042).

    Returns a :class:`CostSummary` for operator visibility ONLY — this is **not** a
    gate: no caller path blocks, refuses, or raises on the returned figures, and there
    is no dollar threshold anywhere in this module (§2.3 / §10.1). An empty history
    yields a zero summary with an empty series (no exception). Money stays
    :class:`~decimal.Decimal` (NFR-007).
    """
    project_id = history[0].project_id if history else ""
    per_iteration = tuple(obs.spend_usd for obs in history)
    cumulative = sum(per_iteration, Decimal(0))
    return CostSummary(
        project_id=project_id,
        cumulative_usd=cumulative,
        per_iteration_usd=per_iteration,
    )
