"""D1 — §4.4 step-6 Learn wiring (run_learn_step + SupervisionCycle._learn)."""

from __future__ import annotations

import pytest

from supervisor.cycle import SupervisionCycle
from supervisor.cycle_wiring import LearnConfig, run_learn_step
from supervisor.run_auditor import RunAuditReport, RunRecord

pytestmark = pytest.mark.unit


def _runs() -> list[RunRecord]:
    return [
        RunRecord(run_id="r1", project_slug="p", status="complete"),
        RunRecord(run_id="r2", project_slug="p", status="failed"),
    ]


def test_run_learn_step_noop_on_empty_source() -> None:
    sink: list[RunAuditReport] = []
    cfg = LearnConfig(runs_source=lambda: [], report_sink=sink.append)
    assert run_learn_step(cfg) is None
    assert sink == []  # no audit, no sink call


def test_run_learn_step_audits_and_sinks() -> None:
    sink: list[RunAuditReport] = []
    cfg = LearnConfig(runs_source=_runs, report_sink=sink.append)
    report = run_learn_step(cfg)
    assert report is not None
    assert report.runs_audited == 2
    assert sink == [report]  # findings-only report handed to the sink


class _ZeroRowRegistry:
    """Minimal RegistryPort double — the no-op steps never call it; Learn doesn't either."""

    def read_candidates(self):  # type: ignore[no-untyped-def]
        return []

    def read_running(self):  # type: ignore[no-untyped-def]
        return []


def test_cycle_learn_delegates_when_configured() -> None:
    sink: list[RunAuditReport] = []
    cycle = SupervisionCycle(
        _ZeroRowRegistry(),  # type: ignore[arg-type]
        learn_config=LearnConfig(runs_source=_runs, report_sink=sink.append),
    )
    cycle.run_once()  # the six steps run; only Learn is configured
    assert len(sink) == 1
    assert sink[0].runs_audited == 2


def test_cycle_learn_noop_when_unconfigured() -> None:
    # A config-less cycle stays the OLB-01 no-op pass — no audit, no raise.
    cycle = SupervisionCycle(_ZeroRowRegistry())  # type: ignore[arg-type]
    cycle.run_once()  # must not raise
