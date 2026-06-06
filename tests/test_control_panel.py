"""T4#8 — control panel core: command writers + event metrics."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from supervisor.control_panel import (
    EventMetrics,
    read_events,
    render_metrics,
    summarize_events,
    write_command,
)

pytestmark = pytest.mark.unit


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
