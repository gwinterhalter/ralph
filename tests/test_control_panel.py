"""T4#8 — control panel core: command writers + event metrics."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from supervisor.control_panel import (
    EventMetrics,
    build_dispatch_command,
    read_events,
    render_correction_summary,
    render_effects,
    render_events,
    render_learning_banner,
    render_learnings,
    render_metrics,
    run_status_panel,
    summarize_effect_outcomes,
    summarize_events,
    summarize_finding_statuses,
    write_command,
)
from supervisor.full_status_surface import FullFleetSnapshot

pytestmark = pytest.mark.unit


def _snapshot(as_of: datetime) -> FullFleetSnapshot:
    return FullFleetSnapshot(
        rows=(),
        counts_by_lifecycle_state={},
        total_attention_debt=0,
        total_open_work_count=0,
        total_cumulative_cost_usd=Decimal(0),
        running_count=0,
        stalled_count=0,
        concurrency_ceiling=2,
        headroom=2,
        as_of=as_of,
        refresh_interval=timedelta(seconds=30),
    )


def test_write_pause_command_is_schema_conformant(tmp_path: Path) -> None:
    path = write_command(
        tmp_path, "pause", command_id="pause_abc", issued_by="greg", issued_at="2026-01-01T00:00:00Z",
        reason="maintenance",
    )
    assert path == tmp_path / "commands" / "pause_abc.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["command_type"] == "pause"
    assert {"command_type", "command_id", "issued_by", "issued_at"} <= doc.keys()
    assert doc["reason"] == "maintenance"


def test_write_bump_budget_requires_and_carries_cap(tmp_path: Path) -> None:
    path = write_command(
        tmp_path, "bump_budget", command_id="bb1", issued_by="greg",
        issued_at="2026-01-01T00:00:00Z", new_cap_usd="500",
    )
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["new_cap_usd"] == "500"


def test_bump_budget_without_cap_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="new_cap_usd"):
        write_command(tmp_path, "bump_budget", command_id="x", issued_by="g", issued_at="t")


def test_unknown_command_type_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown command_type"):
        write_command(tmp_path, "self_destruct", command_id="x", issued_by="g", issued_at="t")


def test_summarize_events_counts_cost_and_failures() -> None:
    events: list[dict[str, object]] = [
        {"event_type": "iteration_begin"},
        {"event_type": "llm_call", "cost_usd": "2.50"},
        {"subject_kind": "llm_call", "detail": {"cost_usd": 1.25}},
        {"event_type": "llm_call", "payload": {"cost_usd": 1.94}},  # live events.jsonl shape
        {"event_type": "iteration_failed"},
        {"event_type": "halt"},
    ]
    m = summarize_events(events)
    assert m.total == 6
    assert m.by_type["llm_call"] == 3
    assert m.total_cost_usd == Decimal("5.69")
    assert m.failures == 2  # iteration_failed + halt


def test_summarize_empty() -> None:
    m = summarize_events([])
    assert m == EventMetrics(total=0)


def test_read_events_skips_garbage(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text(
        '{"event_type":"a"}\n\nnot json\n{"event_type":"b","cost_usd":"1"}\n["not a dict"]\n',
        encoding="utf-8",
    )
    events = read_events(log)
    assert [e["event_type"] for e in events] == ["a", "b"]


def test_read_events_missing_file(tmp_path: Path) -> None:
    assert read_events(tmp_path / "nope.jsonl") == []


def test_render_metrics_is_stringy() -> None:
    out = render_metrics(summarize_events([{"event_type": "x", "cost_usd": "1.0"}]))
    assert "events: 1" in out
    assert "x: 1" in out


def test_run_status_panel_once_renders_a_single_snapshot() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    emitted: list[str] = []
    slept: list[float] = []
    rc = run_status_panel(
        lambda: _snapshot(t0),
        once=True,
        emit=emitted.append,
        sleep=slept.append,
        now=lambda: t0,
    )
    assert rc == 0
    assert len(emitted) == 1  # one render
    assert "Full Fleet Status" in emitted[0]  # the OLB-16 surface header
    assert slept == []  # `once` never sleeps


def test_run_status_panel_rejects_nonpositive_interval() -> None:
    # The RefreshScheduler bound must be positive (FR-061); the panel surfaces that.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="interval"):
        run_status_panel(
            lambda: _snapshot(t0), once=True, interval_seconds=0.0, emit=lambda _s: None
        )


def test_render_learnings_empty() -> None:
    assert "none captured yet" in render_learnings([])


def test_render_learnings_lists_findings() -> None:
    rows = [
        {
            "finding_key": "answerer_dsl_candidate:g1",
            "kind": "answerer_dsl_candidate",
            "subject": "g1",
            "binding_class": None,
            "recommendation": "add an Answerer rule for g1",
            "routes_to": "operator + cf-spec-writer",
            "runs_audited": 4,
        },
        {
            "finding_key": "verification_binding:cf-x:over_verification",
            "kind": "verification_binding",
            "subject": "cf-x",
            "binding_class": "over_verification",
            "recommendation": "drop cf-x",
            "routes_to": "operator + cf-seed-producer",
            "runs_audited": 3,
        },
    ]
    out = render_learnings(rows)
    assert "2 finding(s) captured" in out
    assert "[answerer_dsl_candidate] g1" in out
    assert "[verification_binding:over_verification] cf-x" in out
    assert "add an Answerer rule for g1" in out
    assert "adopt: operator + cf-seed-producer" in out


def test_render_correction_summary() -> None:
    assert "none captured yet" in render_correction_summary([])
    rows = [{"item_id": "OLB-07", "attempts": 5, "projects": 2, "max_level": "L4"}]
    out = render_correction_summary(rows)
    assert "OLB-07: 5 attempt(s) across 2 project(s), deepest L4" in out


def test_build_dispatch_command() -> None:
    finding = {
        "finding_key": "answerer_dsl_candidate:g1",
        "authoring_skill": "cf-spec-writer",
        "recommendation": "add a rule for g1",
    }
    argv = build_dispatch_command(finding, skills_dir="K:/skills")
    assert argv[0] == "claude"
    assert "K:/skills" in argv
    assert argv[-1] == "/cf-spec-writer add a rule for g1"


def test_build_dispatch_command_requires_skill() -> None:
    with pytest.raises(ValueError, match="no authoring_skill"):
        build_dispatch_command({"finding_key": "k", "recommendation": "r"}, skills_dir="x")


def test_render_events() -> None:
    assert "none in the fleet" in render_events([])
    rows = [
        {"ts_utc": "2026-06-07T10:00:00Z", "project_id": "p1", "role": "gate",
         "event_type": "gate_fire", "subject_id": "g1"},
    ]
    out = render_events(rows)
    assert "events: 1" in out
    assert "p1  gate/gate_fire g1" in out


def test_dispatch_succeeded_guards_false_success() -> None:
    from supervisor.control_panel import _dispatch_succeeded

    # exit 0 + real skill output → applied
    assert _dispatch_succeeded(0, '{"is_error":false,"result":"done"}') is True
    # exit 0 but the slash command did not resolve → NOT applied (the L3 drill defect)
    assert _dispatch_succeeded(0, '{"is_error":false,"result":"Unknown command: /cf-doc-reviewer"}') is False
    # non-zero exit → NOT applied
    assert _dispatch_succeeded(1, '{"is_error":false}') is False
    # error envelope → NOT applied
    assert _dispatch_succeeded(0, '{"is_error":true,"result":"boom"}') is False
    # empty output, exit 0 → applied (no evidence of failure)
    assert _dispatch_succeeded(0, "") is True


def test_render_effects() -> None:
    assert "none measured yet" in render_effects([])
    rows = [{"finding_key":"answerer_dsl_candidate:g1","outcome":"confirmed",
             "before_metric":1.0,"after_metric":0.0,"post_adoption_runs":4,"detail":"d",
             "applied_at":"2026-06-08T10:00:00+00:00"}]
    out = render_effects(rows)
    assert "[confirmed] answerer_dsl_candidate:g1" in out
    assert "1.000 → 0.000 over 4 post-run(s)" in out
    assert "adopted 2026-06-08T10:00:00+00:00" in out  # applied_at surfaced


def test_render_learnings_shows_status_and_actionable_hint() -> None:
    rows = [
        {"finding_key": "k:proposed", "kind": "answerer_dsl_candidate", "subject": "g1",
         "status": "proposed", "recommendation": "r", "routes_to": "operator + cf-spec-writer",
         "authoring_skill": "cf-spec-writer", "runs_audited": 3},
        {"finding_key": "k:accepted", "kind": "session_shape", "subject": "s",
         "status": "accepted", "recommendation": "r", "routes_to": "operator", "runs_audited": 2},
        {"finding_key": "k:applied", "kind": "session_shape", "subject": "s2",
         "status": "applied", "recommendation": "r", "routes_to": "operator", "runs_audited": 2},
    ]
    out = render_learnings(rows)
    assert "1 proposed, 1 accepted, 1 applied" in out  # rollup in workflow order
    assert "[proposed] [answerer_dsl_candidate] g1" in out
    assert "(skill: cf-spec-writer)" in out
    assert "control_panel promote k:proposed" in out   # proposed -> promote hint
    assert "control_panel apply k:accepted" in out      # accepted -> apply hint
    assert "control_panel promote k:applied" not in out  # applied: no action hint


def test_summarize_finding_statuses_defaults_missing_to_proposed() -> None:
    counts = summarize_finding_statuses([{"finding_key": "k"}, {"status": "applied"}])
    assert counts == {"proposed": 1, "applied": 1}


def test_render_effects_rollup_worst_first() -> None:
    rows = [
        {"finding_key": "a", "outcome": "confirmed", "before_metric": 1.0, "after_metric": 0.0,
         "post_adoption_runs": 3},
        {"finding_key": "b", "outcome": "regressed", "before_metric": 0.0, "after_metric": 1.0,
         "post_adoption_runs": 3},
        {"finding_key": "c", "outcome": "pending", "before_metric": None, "after_metric": None,
         "post_adoption_runs": 1},
    ]
    out = render_effects(rows)
    assert out.splitlines()[0].index("regressed") < out.splitlines()[0].index("confirmed")
    assert summarize_effect_outcomes(rows) == {"confirmed": 1, "regressed": 1, "pending": 1}


def test_render_learning_banner_flags_actionable() -> None:
    findings = [{"status": "proposed"}, {"status": "applied"}]
    effects = [{"outcome": "regressed"}, {"outcome": "confirmed"}]
    out = render_learning_banner(findings, effects)
    assert "Learnings: 1 proposed, 1 applied" in out
    assert "Effects: 1 regressed, 1 confirmed" in out
    assert "[!]" in out and "awaiting a decision" in out and "not confirmed" in out


def test_render_learning_banner_quiet_when_nothing_actionable() -> None:
    out = render_learning_banner([{"status": "applied"}], [{"outcome": "confirmed"}])
    assert "[!]" not in out  # no proposed findings, no non-confirmed effects
    out_empty = render_learning_banner([], [])
    assert "Learnings: none" in out_empty and "Effects: none measured" in out_empty
