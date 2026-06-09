"""Tier-2 concurrency (2026-06-09): the rolling usage-window guard (supervisor.usage_window).

A self-paced budget against the Max session/weekly cap — the real governor once instantaneous
concurrency is shown free (measured 2026-06-09). Pauses new Dispatch when trailing proxy-cost
usage reaches a window budget, and reports when the window frees (running Runs untouched).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from supervisor.usage_window import (
    UsageEvent,
    UsageWindow,
    evaluate_usage_windows,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _evt(hours_ago: float, cost: str) -> UsageEvent:
    return UsageEvent(ts=NOW - timedelta(hours=hours_ago), cost_usd=Decimal(cost))


def test_under_budget_allows_dispatch() -> None:
    windows = [UsageWindow("5h", timedelta(hours=5), Decimal("10"))]
    events = [_evt(1, "3"), _evt(2, "4")]  # 7 < 10
    decision = evaluate_usage_windows(events, windows, now=NOW)
    assert decision.dispatch_allowed is True
    assert decision.breaches == ()
    assert decision.used_by_window == (("5h", Decimal("7")),)


def test_at_budget_pauses_dispatch() -> None:
    windows = [UsageWindow("5h", timedelta(hours=5), Decimal("10"))]
    events = [_evt(1, "6"), _evt(2, "4")]  # 10 >= 10 → breached
    decision = evaluate_usage_windows(events, windows, now=NOW)
    assert decision.dispatch_allowed is False
    assert len(decision.breaches) == 1
    assert decision.breaches[0].name == "5h"
    assert decision.breaches[0].used_usd == Decimal("10")


def test_events_outside_the_window_do_not_count() -> None:
    windows = [UsageWindow("5h", timedelta(hours=5), Decimal("10"))]
    # 8 is just outside the 5h window; only the 6 (1h ago) counts → 6 < 10, allowed.
    events = [_evt(8, "100"), _evt(1, "6")]
    decision = evaluate_usage_windows(events, windows, now=NOW)
    assert decision.dispatch_allowed is True
    assert decision.used_by_window == (("5h", Decimal("6")),)


def test_reset_time_is_when_enough_oldest_usage_ages_out() -> None:
    # budget 10; three 5-cost events at 4h, 3h, 1h ago → used 15. To fall below 10 we must
    # expire the two oldest (4h: 15->10 still not <10; 3h: ->5 <10). The 3h-ago event leaves
    # the 5h window at (NOW-3h)+5h = NOW+2h → that is resets_at.
    windows = [UsageWindow("5h", timedelta(hours=5), Decimal("10"))]
    events = [_evt(4, "5"), _evt(3, "5"), _evt(1, "5")]
    decision = evaluate_usage_windows(events, windows, now=NOW)
    assert decision.dispatch_allowed is False
    assert decision.breaches[0].resets_at == NOW + timedelta(hours=2)


def test_windows_are_independent() -> None:
    # 5h budget high (not breached), weekly budget low (breached by the same events).
    windows = [
        UsageWindow("5h", timedelta(hours=5), Decimal("100")),
        UsageWindow("weekly", timedelta(days=7), Decimal("5")),
    ]
    events = [_evt(1, "4"), _evt(48, "4")]  # 8 total in the week; 4 in 5h
    decision = evaluate_usage_windows(events, windows, now=NOW)
    assert decision.dispatch_allowed is False
    assert [b.name for b in decision.breaches] == ["weekly"]


def test_no_windows_is_a_clean_allow() -> None:
    decision = evaluate_usage_windows([_evt(1, "1000")], [], now=NOW)
    assert decision.dispatch_allowed is True
    assert decision.breaches == ()


def test_no_events_is_a_clean_allow() -> None:
    windows = [UsageWindow("weekly", timedelta(days=7), Decimal("5"))]
    decision = evaluate_usage_windows([], windows, now=NOW)
    assert decision.dispatch_allowed is True
