"""Rolling usage-window guard — pace Dispatch against the Max subscription's session/weekly
caps (Tier-2 concurrency, 2026-06-09).

The empirical 2026-06-09 measurement showed instantaneous concurrency is NOT the limiter on
a Max 200 subscription — one account sustained >=12 concurrent heavy runs with no throttle.
The real governor is the **rolling 5-hour session cap and the weekly cap**: cumulative usage
over hours, which resets on a fixed window, not a per-request rate. The emergency spend
backstop (:mod:`supervisor.spend_backstop`) is the wrong instrument for it — it keys on an
ALL-TIME cumulative dollar figure and HARD-KILLS. This module is the right instrument: a
ROLLING-window usage budget that **pauses new Dispatch** (running Runs untouched) and reports
when the window frees, so the fleet self-paces under the cap instead of slamming into it.

Honesty about the unit (NFR — anti-confabulation): the Max subscription's internal quota unit
is **not queryable** by us. This guard meters our own recorded ``cost_usd`` — the API-equivalent
figure ``claude`` reports even under a subscription — as a consistent USAGE PROXY. The operator
sets a per-window budget in those proxy dollars; the guard enforces it. It does NOT read
Anthropic's actual remaining quota, and never claims to. Money is exact ``Decimal`` (NFR-007).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class UsageEvent:
    """One cost-bearing ``llm_call`` event: when it happened and its proxy ``cost_usd``."""

    ts: datetime
    cost_usd: Decimal


@dataclass(frozen=True)
class UsageWindow:
    """A rolling usage budget: ``budget_usd`` of proxy spend allowed within the trailing
    ``duration`` (e.g. 5 hours, 7 days). ``name`` is the operator-facing label."""

    name: str
    duration: timedelta
    budget_usd: Decimal


@dataclass(frozen=True)
class WindowBreach:
    """A window whose trailing usage has reached its budget — Dispatch is paused until
    ``resets_at`` (the earliest instant the trailing sum drops back below ``budget_usd`` as
    the oldest in-window usage ages out, assuming no further spend)."""

    name: str
    used_usd: Decimal
    budget_usd: Decimal
    resets_at: datetime


@dataclass(frozen=True)
class UsageDecision:
    """The guard's verdict for one evaluation. ``dispatch_allowed`` is ``False`` iff ANY
    window is breached; ``breaches`` lists every breached window (with its reset instant);
    ``used_by_window`` is the trailing usage per window (for the operator surface)."""

    dispatch_allowed: bool
    breaches: tuple[WindowBreach, ...]
    used_by_window: tuple[tuple[str, Decimal], ...]


def evaluate_usage_windows(
    events: Sequence[UsageEvent],
    windows: Sequence[UsageWindow],
    *,
    now: datetime,
) -> UsageDecision:
    """Evaluate the trailing usage of each window and decide whether new Dispatch is allowed.

    For each window, sums the proxy ``cost_usd`` of events in ``[now - duration, now]``. A
    window is breached when that trailing sum is ``>=`` its budget (Dispatch paused). For a
    breached window, ``resets_at`` is computed by ageing the oldest in-window events out one
    at a time (oldest leaves at ``its ts + duration``) until the remaining trailing sum would
    fall below budget — i.e. the soonest the window frees if no further spend occurs. ``now``
    and every event ``ts`` must be timezone-aware and comparable. With no windows configured
    (the opt-in default) this returns ``dispatch_allowed=True`` and no breaches.
    """
    breaches: list[WindowBreach] = []
    used_by_window: list[tuple[str, Decimal]] = []
    for window in windows:
        cutoff = now - window.duration
        in_window = sorted(
            (e for e in events if e.ts >= cutoff), key=lambda e: e.ts
        )
        used = sum((e.cost_usd for e in in_window), Decimal(0))
        used_by_window.append((window.name, used))
        if used < window.budget_usd:
            continue
        # Breached: age the oldest events out until the trailing sum drops below budget.
        # The k-th oldest event leaves the window at (its ts + duration); after it (and all
        # older) have left, the trailing sum is `remaining` — the first time that is below
        # budget is the reset instant.
        remaining = used
        resets_at = (in_window[-1].ts + window.duration) if in_window else now
        for event in in_window:
            remaining -= event.cost_usd
            if remaining < window.budget_usd:
                resets_at = event.ts + window.duration
                break
        breaches.append(
            WindowBreach(
                name=window.name,
                used_usd=used,
                budget_usd=window.budget_usd,
                resets_at=resets_at,
            )
        )
    return UsageDecision(
        dispatch_allowed=not breaches,
        breaches=tuple(breaches),
        used_by_window=tuple(used_by_window),
    )


__all__ = [
    "UsageDecision",
    "UsageEvent",
    "UsageWindow",
    "WindowBreach",
    "evaluate_usage_windows",
]
