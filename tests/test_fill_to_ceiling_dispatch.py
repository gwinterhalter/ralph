"""Concurrency improvement (2026-06-09): fill-to-ceiling dispatch in one Schedule pass.

``run_schedule_step`` dispatches one Candidate per call; ``run_schedule_fill_step`` repeats
it within a single cycle until the fleet is at the Concurrency Ceiling, nothing is runnable,
or the ``max_dispatches_per_cycle`` bound is reached — without ever overshooting the ceiling.
The empirical N-way headroom (one Max account sustains >=12 concurrent heavy runs, measured
2026-06-09) makes the ceiling, not the API, the only governor — so ramping a cold fleet to
full concurrency in one cycle is the win this turns on.
"""

from __future__ import annotations

import pytest

from supervisor import cycle_wiring
from supervisor.cycle_wiring import ScheduleConfig, run_schedule_fill_step

pytestmark = pytest.mark.unit


class _GrowingReg:
    """Registry double whose ``read_running`` grows by one each time a Candidate is
    'spawned' — so the fill loop sees the live running count climb toward the ceiling and
    stops exactly there. The Candidate pool is fixed; ``running`` starts empty. A spawned
    Candidate is dropped from ``read_candidates`` (it is now running), mirroring the live
    lifecycle move out of ``candidate``."""

    def __init__(self, candidate_ids: list[str]) -> None:
        self._candidates = [
            {"project_id": pid, "priority": 10, "folder_path": "x"} for pid in candidate_ids
        ]
        self.running_ids: list[str] = []

    def read_candidates(self):  # type: ignore[no-untyped-def]
        return [c for c in self._candidates if c["project_id"] not in self.running_ids]

    def read_running(self):  # type: ignore[no-untyped-def]
        return [{"project_id": pid} for pid in self.running_ids]


def _spawning_admit(reg: _GrowingReg):  # type: ignore[no-untyped-def]
    """A fake ``admit_candidate`` that simulates a real spawn: appends the selected
    Candidate's project_id to the registry's running set (a real ``ralph_runs`` row)."""

    def _admit(candidate, **_kwargs):  # type: ignore[no-untyped-def]
        reg.running_ids.append(str(candidate["project_id"]))

    return _admit


def _config(reg: _GrowingReg, *, ceiling: int, max_dispatches: int) -> ScheduleConfig:
    return ScheduleConfig(
        seed_validator=object(),  # type: ignore[arg-type]
        spawn_port=object(),  # type: ignore[arg-type]
        concurrency_ceiling=ceiling,
        max_dispatches_per_cycle=max_dispatches,
        candidate_enricher=lambda row: row,  # pass-through (no seed read needed)
    )


def test_fills_running_to_ceiling_in_one_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _GrowingReg(["p1", "p2", "p3", "p4", "p5"])
    monkeypatch.setattr(cycle_wiring, "admit_candidate", _spawning_admit(reg))
    config = _config(reg, ceiling=3, max_dispatches=3)

    decisions = run_schedule_fill_step(reg, config)  # type: ignore[arg-type]

    assert len(decisions) == 3  # exactly the ceiling spawned this pass
    assert len(reg.running_ids) == 3  # fleet filled to the ceiling in ONE cycle
    # Three DISTINCT projects — the FR-027 in-flight exclusion never re-picks a running one.
    assert len(set(reg.running_ids)) == 3


def test_never_overshoots_ceiling_when_bound_exceeds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg = _GrowingReg(["p1", "p2", "p3", "p4", "p5"])
    monkeypatch.setattr(cycle_wiring, "admit_candidate", _spawning_admit(reg))
    # max_dispatches deliberately larger than the ceiling — the live running guard caps it.
    config = _config(reg, ceiling=2, max_dispatches=10)

    decisions = run_schedule_fill_step(reg, config)  # type: ignore[arg-type]

    assert len(reg.running_ids) == 2  # the §9.3 hard floor, not max_dispatches
    assert len(decisions) == 2


def test_default_max_one_dispatches_exactly_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back-compat: the default ``max_dispatches_per_cycle == 1`` reproduces the historical
    one-dispatch-per-cycle behaviour even with headroom for more."""
    reg = _GrowingReg(["p1", "p2", "p3"])
    monkeypatch.setattr(cycle_wiring, "admit_candidate", _spawning_admit(reg))
    config = ScheduleConfig(
        seed_validator=object(),  # type: ignore[arg-type]
        spawn_port=object(),  # type: ignore[arg-type]
        concurrency_ceiling=3,  # headroom for 3 ...
        candidate_enricher=lambda row: row,
    )  # ... but max_dispatches_per_cycle defaults to 1

    decisions = run_schedule_fill_step(reg, config)  # type: ignore[arg-type]

    assert len(decisions) == 1
    assert len(reg.running_ids) == 1


def test_stops_when_nothing_runnable(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _GrowingReg([])  # no candidates
    monkeypatch.setattr(cycle_wiring, "admit_candidate", _spawning_admit(reg))
    config = _config(reg, ceiling=5, max_dispatches=5)

    assert run_schedule_fill_step(reg, config) == []  # type: ignore[arg-type]


def test_dispatch_gate_false_pauses_all_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier-2 pause-not-kill: a False ``dispatch_gate`` (rolling usage window breached) spawns
    NOTHING this pass — and never touches running (this gate is the spawn path only)."""
    reg = _GrowingReg(["p1", "p2", "p3"])
    monkeypatch.setattr(cycle_wiring, "admit_candidate", _spawning_admit(reg))
    config = ScheduleConfig(
        seed_validator=object(),  # type: ignore[arg-type]
        spawn_port=object(),  # type: ignore[arg-type]
        concurrency_ceiling=3,
        max_dispatches_per_cycle=3,
        candidate_enricher=lambda row: row,
        dispatch_gate=lambda: False,  # usage window breached → pause new dispatch
    )

    decisions = run_schedule_fill_step(reg, config)  # type: ignore[arg-type]

    assert decisions == []
    assert reg.running_ids == []  # nothing dispatched; the gate short-circuited the pass


def test_hold_does_not_spin_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """An admission HOLD (no running row created) stops the fill loop after one attempt —
    a held / rejected Candidate cannot spin to the attempt bound (FR-019)."""
    reg = _GrowingReg(["p1", "p2", "p3"])
    calls = {"n": 0}

    def _holding_admit(candidate, **_kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1  # never grows running — the FR-019 ceiling hold

    monkeypatch.setattr(cycle_wiring, "admit_candidate", _holding_admit)
    config = _config(reg, ceiling=5, max_dispatches=5)

    decisions = run_schedule_fill_step(reg, config)  # type: ignore[arg-type]

    assert calls["n"] == 1  # one attempt only — the no-progress guard stopped it
    assert len(decisions) == 1
    assert reg.running_ids == []
