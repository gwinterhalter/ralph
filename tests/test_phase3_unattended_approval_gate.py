"""Phase 3 (daemonize / unattended operation) — the safety invariant that makes a RESIDENT
supervisor safe to run logged-off: it dispatches ONLY operator-approved projects.

Operator policy (2026-07-09): no Sub_Projects RL project runs without the operator's direct
per-project permission. Enforcement: projects sit at lifecycle_state='pending_approval'; the
Schedule step only ever sees 'candidate' rows (read_candidates), so a parked project can never
be dispatched — no matter how many ~30s cycles a daemonized supervisor turns. Approval
(pending_approval -> candidate) is the ONLY release, and it releases exactly the approved one.

Hermetic (fake registry modelling the lifecycle_state gate); no DB, no spawn, no spend.
"""
from __future__ import annotations

import pytest

from supervisor import cycle_wiring
from supervisor.cycle_wiring import ScheduleConfig, run_schedule_fill_step

pytestmark = pytest.mark.unit


class _Fleet:
    """Registry double modelling lifecycle_state. ``read_candidates`` applies the REAL gate —
    only ``candidate`` rows are dispatchable; ``pending_approval`` is excluded (mirrors the live
    ``WHERE lifecycle_state='candidate'``). A spawned candidate joins ``running`` and thereby
    drops out of the candidate pool (FR-027 in-flight exclusion). ``approve`` is the operator's
    only lever: pending_approval -> candidate."""

    def __init__(self, states: dict[str, str]) -> None:
        self.states = dict(states)  # project_id -> lifecycle_state
        self.running_ids: list[str] = []

    def read_candidates(self):  # type: ignore[no-untyped-def]
        return [
            {"project_id": pid, "priority": 10, "folder_path": "x"}
            for pid, st in self.states.items()
            if st == "candidate" and pid not in self.running_ids
        ]

    def read_running(self):  # type: ignore[no-untyped-def]
        return [{"project_id": pid} for pid in self.running_ids]

    def approve(self, pid: str) -> None:  # operator Approve in the control panel
        assert self.states.get(pid) == "pending_approval", "approve only from pending_approval"
        self.states[pid] = "candidate"


def _spawning_admit(fleet: _Fleet):  # type: ignore[no-untyped-def]
    """Fake admit_candidate: simulates a real spawn by moving the picked candidate to running."""

    def _admit(candidate, **_kwargs):  # type: ignore[no-untyped-def]
        fleet.running_ids.append(str(candidate["project_id"]))
        return None

    return _admit


def _config(fleet: _Fleet, *, ceiling: int = 12, max_dispatches: int = 12) -> ScheduleConfig:
    return ScheduleConfig(
        seed_validator=object(),  # type: ignore[arg-type]
        spawn_port=object(),  # type: ignore[arg-type]
        concurrency_ceiling=ceiling,
        max_dispatches_per_cycle=max_dispatches,
        candidate_enricher=lambda row: row,  # pass-through
    )


def test_resident_loop_dispatches_nothing_when_whole_fleet_is_pending_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 15 parked projects — mirrors the live fleet after the 2026-07-09 gating.
    fleet = _Fleet({f"p{i}": "pending_approval" for i in range(1, 16)})
    monkeypatch.setattr(cycle_wiring, "admit_candidate", _spawning_admit(fleet))
    config = _config(fleet)

    dispatched = []
    for _tick in range(6):  # a daemonized supervisor turning many ~30s cycles
        dispatched += run_schedule_fill_step(fleet, config)  # type: ignore[arg-type]

    assert dispatched == []  # NOTHING dispatched across every cycle
    assert fleet.running_ids == []  # no project ran without approval


def test_approving_exactly_one_releases_exactly_that_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet = _Fleet({"a": "pending_approval", "b": "pending_approval", "c": "pending_approval"})
    monkeypatch.setattr(cycle_wiring, "admit_candidate", _spawning_admit(fleet))
    config = _config(fleet)

    assert run_schedule_fill_step(fleet, config) == []  # type: ignore[arg-type]  # nothing before approval

    fleet.approve("b")  # operator approves ONE
    decisions = run_schedule_fill_step(fleet, config)  # type: ignore[arg-type]

    assert fleet.running_ids == ["b"]  # only the approved project dispatched
    assert len(decisions) == 1
    # the two still-parked projects never dispatched
    assert "a" not in fleet.running_ids and "c" not in fleet.running_ids


def test_approved_project_dispatches_once_not_every_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet = _Fleet({"a": "pending_approval"})
    monkeypatch.setattr(cycle_wiring, "admit_candidate", _spawning_admit(fleet))
    config = _config(fleet)

    fleet.approve("a")
    run_schedule_fill_step(fleet, config)  # type: ignore[arg-type]  # tick 1: dispatch a
    run_schedule_fill_step(fleet, config)  # type: ignore[arg-type]  # tick 2: a is running -> excluded
    run_schedule_fill_step(fleet, config)  # type: ignore[arg-type]  # tick 3: still excluded

    assert fleet.running_ids == ["a"]  # dispatched exactly once, not re-dispatched each cycle
