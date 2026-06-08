"""Operator Action-Inbox aggregation (Control Panel GUI — Home screen core).

Pure, decision-free re-shaping of the signals the control panel already exposes (fleet snapshot,
Run-Auditor findings, measured effects, correction churn, an optional budget breach) into ONE
priority-ordered list of :class:`InboxCard` — the "Needs You" queue. This module is the GUI's home
screen logic; it adds NO new decisions, only presentation: every card maps to an already-existing
signal and its actions map to already-existing write seams (pause / promote / reject / apply /
revert). No I/O, no DB, no wall-clock — the wiring reads the substrate and passes the rows in.

Urgency tiers (lower = surfaced first), mirroring the §8 attention scheduler's intent that a
safety/budget signal outranks routine work:

* ``0`` budget breach   — spend ceiling tripped (the supervisor may already be auto-pausing)
* ``1`` gate            — a Project is paused awaiting an operator decision
* ``2`` stall           — a running Run's heartbeat is stale
* ``3`` regressed       — an ADOPTED learning measured worse / no-effect (surface-only; offer revert)
* ``4`` learning        — a proposed finding is ready for an adoption decision
* ``5`` churn           — an item keeps re-entering the correction loop (chronic-defect indicator)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from supervisor.full_status_surface import HEARTBEAT_STALLED

if TYPE_CHECKING:
    from supervisor.full_status_surface import ProjectFullStatusRow

# Card kinds.
KIND_BUDGET = "budget"
KIND_GATE = "gate"
KIND_STALL = "stall"
KIND_REGRESSED = "regressed"
KIND_LEARNING = "learning"
KIND_CHURN = "churn"

#: Urgency tier per kind (lower surfaces first).
_URGENCY: dict[str, int] = {
    KIND_BUDGET: 0,
    KIND_GATE: 1,
    KIND_STALL: 2,
    KIND_REGRESSED: 3,
    KIND_LEARNING: 4,
    KIND_CHURN: 5,
}

#: The lifecycle state a Project sits in while a gate awaits an operator decision.
_PAUSED_GATE_STATE = "paused_gate"

#: Effect outcomes that warrant operator attention (mirror effect_measure).
_NON_CONFIRMED = frozenset({"regressed", "no_effect"})

#: Default correction-attempt count at/above which an item is flagged as chronic churn.
DEFAULT_CHURN_THRESHOLD = 3


@dataclass(frozen=True)
class InboxCard:
    """One actionable item in the operator's Needs-You queue (pure value object)."""

    kind: str
    urgency: int
    title: str
    subject: str
    detail: str
    actions: tuple[str, ...]
    recommended: str | None = None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def build_inbox(
    *,
    fleet_rows: Sequence[ProjectFullStatusRow] = (),
    findings: Sequence[Mapping[str, object]] = (),
    effects: Sequence[Mapping[str, object]] = (),
    corrections: Sequence[Mapping[str, object]] = (),
    budget_breach: Mapping[str, object] | None = None,
    churn_threshold: int = DEFAULT_CHURN_THRESHOLD,
) -> list[InboxCard]:
    """Aggregate the fleet/learning/effect/churn signals into a priority-ordered Needs-You queue.

    Pure: every argument is already-read substrate; returns the cards sorted by ``(urgency, subject)``
    so the most urgent (and, within a tier, a stable subject order) leads. A clean fleet with no
    proposed findings and no non-confirmed effects yields ``[]`` (the home screen reads 'all clear')."""
    cards: list[InboxCard] = []

    if budget_breach is not None:
        cards.append(
            InboxCard(
                kind=KIND_BUDGET,
                urgency=_URGENCY[KIND_BUDGET],
                title="Budget ceiling tripped",
                subject=str(budget_breach.get("project_id") or "*fleet*"),
                detail=str(budget_breach.get("detail") or "cumulative spend reached the ceiling"),
                actions=("review", "bump_budget", "pause"),
                recommended="review",
            )
        )

    for row in fleet_rows:
        if row.lifecycle_state == _PAUSED_GATE_STATE:
            cards.append(
                InboxCard(
                    kind=KIND_GATE,
                    urgency=_URGENCY[KIND_GATE],
                    title=f"Gate awaiting decision · {row.display_name}",
                    subject=row.project_id,
                    detail="a Project is paused on a gate; open it to decide",
                    actions=("proceed", "hold", "details"),
                )
            )
        elif row.heartbeat_state == HEARTBEAT_STALLED:
            cards.append(
                InboxCard(
                    kind=KIND_STALL,
                    urgency=_URGENCY[KIND_STALL],
                    title=f"Stalled run · {row.display_name}",
                    subject=row.project_id,
                    detail="running Run's heartbeat is stale",
                    actions=("investigate", "pause", "force_reap"),
                )
            )

    for finding in findings:
        if str(finding.get("status") or "proposed") != "proposed":
            continue
        kind_label = str(finding.get("kind", "?"))
        bclass = finding.get("binding_class")
        if bclass:
            kind_label = f"{kind_label}:{bclass}"
        skill = finding.get("authoring_skill")
        detail = str(finding.get("recommendation") or "review this learning")
        if skill:
            detail += f"  → {skill}"
        cards.append(
            InboxCard(
                kind=KIND_LEARNING,
                urgency=_URGENCY[KIND_LEARNING],
                title=f"Learning ready · {kind_label} {finding.get('subject', '?')}",
                subject=str(finding.get("finding_key", "?")),
                detail=detail,
                actions=("adopt", "reject", "why"),
            )
        )

    for effect in effects:
        if str(effect.get("outcome") or "") not in _NON_CONFIRMED:
            continue
        before = effect.get("before_metric")
        after = effect.get("after_metric")
        b = f"{before:.3f}" if isinstance(before, (int, float)) else "?"
        a = f"{after:.3f}" if isinstance(after, (int, float)) else "?"
        cards.append(
            InboxCard(
                kind=KIND_REGRESSED,
                urgency=_URGENCY[KIND_REGRESSED],
                title=f"Adopted learning {effect.get('outcome')} · {effect.get('finding_key', '?')}",
                subject=str(effect.get("finding_key", "?")),
                detail=f"{b} → {a} over {_int(effect.get('post_adoption_runs'))} post-run(s)",
                actions=("revert", "keep", "details"),
                recommended="details",
            )
        )

    for item in corrections:
        attempts = _int(item.get("attempts"))
        if attempts < churn_threshold:
            continue
        cards.append(
            InboxCard(
                kind=KIND_CHURN,
                urgency=_URGENCY[KIND_CHURN],
                title=f"Chronic correction churn · {item.get('item_id', '?')}",
                subject=str(item.get("item_id", "?")),
                detail=(
                    f"{attempts} attempt(s) across {_int(item.get('projects'))} project(s), "
                    f"deepest {item.get('max_level', '?')}"
                ),
                actions=("details",),
            )
        )

    cards.sort(key=lambda c: (c.urgency, c.subject))
    return cards


__all__ = [
    "InboxCard",
    "build_inbox",
    "DEFAULT_CHURN_THRESHOLD",
    "KIND_BUDGET",
    "KIND_GATE",
    "KIND_STALL",
    "KIND_REGRESSED",
    "KIND_LEARNING",
    "KIND_CHURN",
]
