"""Opt-in hard emergency spend ceiling — the last-resort backstop (robustness T3#6).

The OLB-12 Cost Circuit-Breaker is deliberately a *detector*, not a cap (§2.3 /
§10.1 — it never blocks on a planned dollar figure). That is the right default, but
it leaves no hard floor against a runaway the anomaly detector misreads (e.g. a
gradual climb that never trips a delta threshold). This module is that floor — and
it is **OFF by default**: with ``ceiling_usd = None`` it never fires, so the
breaker's no-cap invariant is preserved unless an operator explicitly opts in.

When a ceiling is configured and cumulative spend reaches it, the backstop returns a
top-tier :class:`SafetyEscalation` with ``killed = True`` — the FR-036 kill-switch
signal (a genuine emergency halt, distinct from the breaker's ``paused_safety`` trip
which leaves ``killed = False``). The Guard step's production wiring, on such an
escalation, engages the :class:`KillSwitch` and trips the Project to
``paused_safety`` through the OLB-02 write seam. Money is exact ``Decimal`` (NFR-007).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from supervisor.safety_gates import ESCALATION_TIER_TOP, PAUSED_SAFETY, SafetyEscalation


@dataclass(frozen=True)
class EmergencySpendConfig:
    """Opt-in emergency spend ceiling. ``ceiling_usd = None`` (default) = OFF.

    Set ``ceiling_usd`` to a hard fleet/Project cumulative-spend figure beyond which
    dispatch must be killed regardless of anomaly classification. This is a safety
    backstop sized well above expected spend, NOT a budget cap — the seed
    ``budget.tokens_usd`` and the OLB-12 detector remain the primary controls.
    """

    ceiling_usd: Decimal | None = None


def evaluate_spend_backstop(
    cumulative_usd: Decimal,
    config: EmergencySpendConfig,
    *,
    project_id: str,
) -> SafetyEscalation | None:
    """Return a kill-switch escalation iff the opt-in ceiling is reached, else None.

    No-op (``None``) when ``config.ceiling_usd`` is None (the default — the backstop
    is disabled and the breaker's no-cap design stands) or when cumulative spend is
    below the ceiling. Fires at ``>=`` the ceiling.
    """
    ceiling = config.ceiling_usd
    if ceiling is None or cumulative_usd < ceiling:
        return None
    return SafetyEscalation(
        project_id=project_id,
        reason=(
            f"emergency_spend_ceiling_exceeded: cumulative ${cumulative_usd} "
            f">= hard ceiling ${ceiling} (T3#6 opt-in backstop — FR-036 kill)"
        ),
        lifecycle_state=PAUSED_SAFETY,
        tier=ESCALATION_TIER_TOP,
        killed=True,
    )


__all__ = ["EmergencySpendConfig", "evaluate_spend_backstop"]
