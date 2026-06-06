"""D6 — production cycle assembly (supervisor.__main__.build_production_cycle)."""

from __future__ import annotations

import pytest

from supervisor.__main__ import build_production_cycle

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
        active_runs_source=lambda: [],
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
