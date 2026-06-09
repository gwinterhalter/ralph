"""Regression: FR-019 ceiling-held `admitted` Projects are re-fed to Schedule.

Surfaced by the live multi-project fleet run: ``read_candidates`` returns only
``candidate`` rows and ``to_project_records`` took only ``(candidates, running)``, so a
Project the ceiling-hold moved to ``admitted`` was in neither set and was never
re-dispatched — orphaned in ``admitted`` limbo. ``run_schedule_step`` now also pulls
``admitted`` Projects via the injected ``admitted_source`` and routes them to the spawn
path once headroom frees.
"""

from __future__ import annotations

import pytest

from supervisor import cycle_wiring
from supervisor.cycle_wiring import ScheduleConfig, run_schedule_step

pytestmark = pytest.mark.unit


class _Reg:
    """Zero-candidate / zero-running registry double (the held Project is supplied
    only via ``admitted_source``, proving that source is consulted)."""

    def read_candidates(self):  # type: ignore[no-untyped-def]
        return []

    def read_running(self):  # type: ignore[no-untyped-def]
        return []


def test_admitted_project_is_redispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    routed: list[str] = []

    def _fake_admit(candidate, **_kwargs):  # type: ignore[no-untyped-def]
        routed.append(str(candidate["project_id"]))
        return None

    monkeypatch.setattr(cycle_wiring, "admit_candidate", _fake_admit)
    held = {"project_id": "held", "priority": 20, "folder_path": "x"}
    config = ScheduleConfig(
        seed_validator=object(),  # type: ignore[arg-type]
        spawn_port=object(),  # type: ignore[arg-type]
        admitted_source=lambda: [held],
        candidate_enricher=lambda row: row,  # pass-through (no seed read needed)
    )

    decision = run_schedule_step(_Reg(), config)  # type: ignore[arg-type]

    assert decision is not None
    assert decision.project_id == "held"
    assert decision.dispatch_kind == "spawn"
    assert routed == ["held"]  # the admitted Project was routed to the spawn path


def test_no_candidates_no_admitted_is_noop() -> None:
    config = ScheduleConfig(seed_validator=object(), spawn_port=object())  # type: ignore[arg-type]
    assert run_schedule_step(_Reg(), config) is None  # type: ignore[arg-type]
