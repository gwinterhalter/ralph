"""D6 — production cycle assembly (supervisor.__main__.build_production_cycle)."""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from typing import Self

import pytest

from supervisor.__main__ import build_production_cycle
from supervisor.registry import Registry
from supervisor.safety_gates import KillSwitch

pytestmark = pytest.mark.unit


class _FakeRegistry:
    """Zero-row RegistryPort double that records any WRITE call (NFR-009 probe)."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    def read_candidates(self):  # type: ignore[no-untyped-def]
        return []

    def read_running(self):  # type: ignore[no-untyped-def]
        return []

    def set_lifecycle_state(self, *a, **k):  # type: ignore[no-untyped-def]
        self.writes.append("set_lifecycle_state")

    def record_run(self, *a, **k):  # type: ignore[no-untyped-def]
        self.writes.append("record_run")

    def update_run_status(self, *a, **k):  # type: ignore[no-untyped-def]
        self.writes.append("update_run_status")

    def reconcile_run(self, *a, **k):  # type: ignore[no-untyped-def]
        self.writes.append("reconcile_run")

    def set_run_orchestrator_pid(self, *a, **k):  # type: ignore[no-untyped-def]
        self.writes.append("set_run_orchestrator_pid")


class _FakeSeedValidator:
    def validate_seed(self, candidate):  # type: ignore[no-untyped-def]
        return []


class _FakeSpawnPort:
    def spawn(self, seed_path, blast_radius_scope):  # type: ignore[no-untyped-def]
        raise AssertionError("spawn must not be called over a zero-candidate fleet")


def _build() -> tuple[_FakeRegistry, object]:
    reg = _FakeRegistry()
    cycle = build_production_cycle(
        reg,  # type: ignore[arg-type]
        seed_validator=_FakeSeedValidator(),
        spawn_port=_FakeSpawnPort(),
        active_runs_source=list,
    )
    return reg, cycle


def test_assembly_sets_all_five_step_configs() -> None:
    _reg, cycle = _build()
    assert cycle._reconcile_config is not None  # type: ignore[attr-defined]
    assert cycle._schedule_config is not None  # type: ignore[attr-defined]
    assert cycle._attend_config is not None  # type: ignore[attr-defined]
    assert cycle._guard_config is not None  # type: ignore[attr-defined]
    assert cycle._learn_config is not None  # type: ignore[attr-defined]


def test_run_once_over_zero_row_fleet_is_a_clean_no_op() -> None:
    reg, cycle = _build()
    cycle.run_once()  # type: ignore[attr-defined]  # all six steps run; must not raise
    # NFR-009 / OLB-01 invariant: a zero-row pass issues no registry write.
    assert reg.writes == []


def test_run_once_is_idempotent_across_passes() -> None:
    reg, cycle = _build()
    for _ in range(3):
        cycle.run_once()  # type: ignore[attr-defined]
    assert reg.writes == []


# --- operator drill knobs: kill-switch + hang-timeout thread into the configs ---


def test_kill_switch_is_threaded_into_schedule_config() -> None:
    """FR-036: an engaged Kill-Switch passed to the assembly reaches the Schedule step's
    admission gate (which refuses all new dispatch while engaged)."""
    cycle = build_production_cycle(
        _FakeRegistry(),  # type: ignore[arg-type]
        seed_validator=_FakeSeedValidator(),
        spawn_port=_FakeSpawnPort(),
        active_runs_source=list,
        kill_switch=KillSwitch(engaged=True),
    )
    assert cycle._schedule_config.kill_switch.engaged is True  # type: ignore[attr-defined]


def test_default_kill_switch_is_disengaged() -> None:
    _reg, cycle = _build()
    assert cycle._schedule_config.kill_switch.engaged is False  # type: ignore[attr-defined]


def test_hang_timeout_is_threaded_into_reconcile_config() -> None:
    """The stall-drill knob: the supplied hang budget reaches the Reconcile config."""
    cycle = build_production_cycle(
        _FakeRegistry(),  # type: ignore[arg-type]
        seed_validator=_FakeSeedValidator(),
        spawn_port=_FakeSpawnPort(),
        active_runs_source=list,
        hang_timeout_seconds=60.0,
    )
    assert cycle._reconcile_config.hang_timeout_seconds == 60.0  # type: ignore[attr-defined]


# --- concurrency improvement (2026-06-09): tunable ceiling + fill-to-ceiling dispatch ---


def test_default_ceiling_is_the_safe_library_default() -> None:
    """Unset → the Schedule config keeps DEFAULT_CONCURRENCY_CEILING, so the status surfaces
    (which default to the same constant) stay consistent with the dispatcher."""
    from supervisor.safety_gates import DEFAULT_CONCURRENCY_CEILING

    _reg, cycle = _build()
    sc = cycle._schedule_config  # type: ignore[attr-defined]
    assert sc.concurrency_ceiling == DEFAULT_CONCURRENCY_CEILING
    # max_dispatches defaults to the ceiling → a cold fleet fills in one cycle.
    assert sc.max_dispatches_per_cycle == DEFAULT_CONCURRENCY_CEILING


def test_concurrency_ceiling_threads_into_schedule_config() -> None:
    """OL_SUPERVISOR_CONCURRENCY_CEILING (read in main, passed here) reaches the Schedule
    step's FR-037 ceiling and, by default, its fill-to-ceiling bound."""
    cycle = build_production_cycle(
        _FakeRegistry(),  # type: ignore[arg-type]
        seed_validator=_FakeSeedValidator(),
        spawn_port=_FakeSpawnPort(),
        active_runs_source=list,
        concurrency_ceiling=7,
    )
    sc = cycle._schedule_config  # type: ignore[attr-defined]
    assert sc.concurrency_ceiling == 7
    assert sc.max_dispatches_per_cycle == 7  # defaults to the ceiling (fill in one pass)


def test_max_dispatches_override_threads_through() -> None:
    """OL_SUPERVISOR_MAX_DISPATCHES_PER_CYCLE can stagger spawns below the ceiling."""
    cycle = build_production_cycle(
        _FakeRegistry(),  # type: ignore[arg-type]
        seed_validator=_FakeSeedValidator(),
        spawn_port=_FakeSpawnPort(),
        active_runs_source=list,
        concurrency_ceiling=7,
        max_dispatches_per_cycle=2,
    )
    sc = cycle._schedule_config  # type: ignore[attr-defined]
    assert sc.concurrency_ceiling == 7
    assert sc.max_dispatches_per_cycle == 2


# --- Tier-2 concurrency (2026-06-09): usage-window dispatch gate threads into Schedule ---


def test_dispatch_gate_threads_into_schedule_config() -> None:
    """The Tier-2 usage-window pause hook reaches the Schedule step (pauses new dispatch)."""
    gate = lambda: False

    cycle = build_production_cycle(
        _FakeRegistry(),  # type: ignore[arg-type]
        seed_validator=_FakeSeedValidator(),
        spawn_port=_FakeSpawnPort(),
        active_runs_source=list,
        dispatch_gate=gate,
    )
    assert cycle._schedule_config.dispatch_gate is gate  # type: ignore[attr-defined]


def test_default_dispatch_gate_allows() -> None:
    _reg, cycle = _build()
    assert cycle._schedule_config.dispatch_gate() is True  # type: ignore[attr-defined]


# --- Tier-2: Registry.read_usage_events_since parses cost from the event payload jsonb ---


class _RowsCursor:
    def __init__(self, rows: object) -> None:
        self._rows = rows

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: object = ()) -> None:
        self.query = query

    def fetchall(self) -> object:
        return self._rows


class _RowsConn:
    def __init__(self, rows: object) -> None:
        self._rows = rows

    def cursor(self) -> _RowsCursor:
        return _RowsCursor(self._rows)

    def commit(self) -> None:  # pragma: no cover - reads don't commit
        pass


def test_read_usage_events_parses_payload_cost() -> None:
    from datetime import datetime

    t1 = datetime(2026, 6, 9, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 6, 9, 11, 0, tzinfo=UTC)
    rows = [
        (t1, {"cost_usd": "1.25"}),  # dict payload, primary key
        (t2, '{"total_cost_usd": 2.5}'),  # str payload, alternate key
        (t2, {"role": "planner"}),  # no cost → skipped (contributes nothing)
    ]
    got = Registry(_RowsConn(rows)).read_usage_events_since("2026-06-09T00:00:00+00:00")
    assert got == [(t1, Decimal("1.25")), (t2, Decimal("2.5"))]


# --- Registry.read_cumulative_spend_usd (the spend-backstop input) ---


class _SumCursor:
    def __init__(self, result: object) -> None:
        self._result = result

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: object = ()) -> None:
        self.query = query

    def fetchone(self) -> object:
        return self._result


class _SumConn:
    def __init__(self, result: object) -> None:
        self._result = result

    def cursor(self) -> _SumCursor:
        return _SumCursor(self._result)

    def commit(self) -> None:  # pragma: no cover - reads don't commit
        pass


def test_read_cumulative_spend_sums_as_decimal() -> None:
    got = Registry(_SumConn((Decimal("7.5781"),))).read_cumulative_spend_usd()
    assert got == Decimal("7.5781")
    assert isinstance(got, Decimal)


def test_read_cumulative_spend_empty_table_is_zero() -> None:
    # COALESCE(SUM, 0) → 0 on an empty table; coerced to an exact Decimal.
    assert Registry(_SumConn((0,))).read_cumulative_spend_usd() == Decimal(0)
