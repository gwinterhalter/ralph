"""Item 1 — cross-initiative dependency gating at the Schedule step.

``run_schedule_step`` excludes a Candidate whose ``depends_on`` names a project that is not
yet ``complete`` from the dispatch pool, so a dependency-blocked Project never consumes the
single per-cycle dispatch slot (the "nicer" exclusion the continuation prompt flagged). The
admission gate's own ``DependencyHold`` is the authoritative safety net (covered in
``test_supervisor_admission.py``); here we assert the scheduler-level filter + the live
``completed_project_ids`` thread-through, against the same monkeypatched-admit pattern the
FR-019 admitted-pickup regression uses.
"""

from __future__ import annotations

import pytest

from supervisor import cycle_wiring
from supervisor.cycle_wiring import ScheduleConfig, run_schedule_step

pytestmark = pytest.mark.unit


class _Reg:
    """Registry double whose ``candidate`` set is supplied per test; no running rows."""

    def __init__(self, candidates: list[dict[str, object]]) -> None:
        self._candidates = candidates

    def read_candidates(self):  # type: ignore[no-untyped-def]
        return list(self._candidates)

    def read_running(self):  # type: ignore[no-untyped-def]
        return []


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    completed: frozenset[str],
    routed: list[str],
) -> ScheduleConfig:
    """Patch the admit seam (so the test asserts the scheduler's selection/filtering, not the
    separately-tested admission pipeline) and return a Schedule config wired with ``completed``."""

    def _fake_admit(candidate, **_kwargs):  # type: ignore[no-untyped-def]
        routed.append(str(candidate["project_id"]))

    monkeypatch.setattr(cycle_wiring, "admit_candidate", _fake_admit)
    return ScheduleConfig(
        seed_validator=object(),  # type: ignore[arg-type]
        spawn_port=object(),  # type: ignore[arg-type]
        candidate_enricher=lambda row: row,  # pass-through (no seed read)
        completed_project_ids=lambda: completed,
    )


def test_dependency_blocked_candidate_is_not_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sole Candidate depends on an incomplete prerequisite → filtered out → no dispatch."""
    routed: list[str] = []
    config = _setup(monkeypatch, completed=frozenset(), routed=routed)
    reg = _Reg([{"project_id": "B", "priority": 50, "folder_path": "x", "depends_on": ["A"]}])

    decision = run_schedule_step(reg, config)  # type: ignore[arg-type]

    assert decision is None  # blocked Candidate never enters the runnable set
    assert routed == []  # nothing routed to the spawn path


def test_dependency_unblocked_once_prerequisite_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same Candidate, once its prerequisite is ``complete``, is dispatched."""
    routed: list[str] = []
    config = _setup(monkeypatch, completed=frozenset({"A"}), routed=routed)
    reg = _Reg([{"project_id": "B", "priority": 50, "folder_path": "x", "depends_on": ["A"]}])

    decision = run_schedule_step(reg, config)  # type: ignore[arg-type]

    assert decision is not None
    assert decision.project_id == "B"
    assert routed == ["B"]


def test_blocked_candidate_does_not_consume_slot_for_runnable_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a higher-priority but dependency-blocked Candidate alongside a runnable one,
    the runnable Candidate is dispatched — the block does not waste the dispatch slot."""
    routed: list[str] = []
    config = _setup(monkeypatch, completed=frozenset(), routed=routed)
    reg = _Reg(
        [
            {"project_id": "B", "priority": 99, "folder_path": "x", "depends_on": ["A"]},
            {"project_id": "C", "priority": 10, "folder_path": "x"},
        ]
    )

    decision = run_schedule_step(reg, config)  # type: ignore[arg-type]

    assert decision is not None
    assert decision.project_id == "C"  # the unblocked Candidate wins despite lower priority
    assert routed == ["C"]
