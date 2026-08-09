"""F-4: the Reconcile step honors each run's seed ``budget.hang_timeout_seconds``.

Resolves the seed-param review finding that ``budget.hang_timeout_seconds`` was declared
in every seed but read nowhere — so per-project stall budgets were silently overruled by
the fleet default (1800s). The value now flows: seed -> ``candidate_enrichment.
seed_hang_timeout_seconds`` -> ``derive_reconcile_actions(hang_timeout_of=...)`` (wired in
``__main__.build_production_cycle``). Hermetic: pure reconcile + a tmp seed file.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from supervisor.candidate_enrichment import seed_hang_timeout_seconds
from supervisor.reconcile import (
    LIFECYCLE_PAUSED_GATE,
    REASON_STALLED,
    RUN_HALTED,
    derive_reconcile_actions,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)


def _row(minutes_ago: float) -> dict[str, object]:
    ts = (_NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {"project_id": "p", "orchestrator_pid": 1234, "spawned_at": ts}


def _alive(_pid: int) -> bool:
    return True


def test_per_run_override_stalls_below_the_scalar_default() -> None:
    """Progress 5 min old: the fleet default (1800s) would NOT stall, but a per-run 60s
    budget (the seed's) does — proving the per-project budget is honored."""
    actions = derive_reconcile_actions(
        [_row(5)], pid_alive=_alive, now=_NOW, hang_timeout_seconds=1800.0,
        hang_timeout_of=lambda _r: 60.0,
    )
    assert len(actions) == 1
    assert actions[0].run_status == RUN_HALTED
    assert actions[0].lifecycle_state == LIFECYCLE_PAUSED_GATE
    assert actions[0].reason == REASON_STALLED


def test_override_none_falls_back_to_scalar() -> None:
    """A per-run override of None uses the scalar default (1800s) → 5-min-old run is fine."""
    actions = derive_reconcile_actions(
        [_row(5)], pid_alive=_alive, now=_NOW, hang_timeout_seconds=1800.0,
        hang_timeout_of=lambda _r: None,
    )
    assert actions == []


def test_default_probe_unchanged_behaviour() -> None:
    """No hang_timeout_of supplied → the scalar applies to all rows (backward compatible)."""
    actions = derive_reconcile_actions(
        [_row(40)], pid_alive=_alive, now=_NOW, hang_timeout_seconds=1800.0,
    )
    assert len(actions) == 1 and actions[0].run_status == RUN_HALTED


def test_seed_reader_reads_budget_hang_timeout(tmp_path: Path) -> None:
    seed = tmp_path / "seed.md"
    seed.write_text(
        "---\nbudget:\n  tokens_usd: 8.0\n  hang_timeout_seconds: 600\n---\nbody\n",
        encoding="utf-8",
    )
    assert seed_hang_timeout_seconds(seed) == 600.0


def test_seed_reader_none_when_absent_nonnumeric_or_missing(tmp_path: Path) -> None:
    no_field = tmp_path / "a.md"
    no_field.write_text("---\nbudget:\n  tokens_usd: 8.0\n---\n", encoding="utf-8")
    assert seed_hang_timeout_seconds(no_field) is None

    no_front = tmp_path / "b.md"
    no_front.write_text("just text\n", encoding="utf-8")
    assert seed_hang_timeout_seconds(no_front) is None

    assert seed_hang_timeout_seconds(tmp_path / "missing.md") is None
