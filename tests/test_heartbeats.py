"""D4 — heartbeat-pointer reader (supervisor.heartbeats, FR-005/062)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from supervisor.heartbeats import (
    latest_heartbeats,
    read_heartbeats_from_log,
)

pytestmark = pytest.mark.unit


def test_keeps_newest_progress_per_project() -> None:
    events: list[dict[str, object]] = [
        {"project_id": "p1", "event_type": "phase_complete", "ts_utc": "2026-06-05T04:00:00Z"},
        {"project_id": "p1", "event_type": "llm_call", "ts_utc": "2026-06-05T05:30:00Z"},  # newer
        {"project_id": "p2", "subject_kind": "role_complete", "ts_utc": "2026-06-05T04:10:00Z"},
    ]
    hb = latest_heartbeats(events)
    assert hb["p1"] == datetime(2026, 6, 5, 5, 30, tzinfo=UTC)
    assert hb["p2"] == datetime(2026, 6, 5, 4, 10, tzinfo=UTC)


def test_non_heartbeat_events_and_bad_rows_ignored() -> None:
    events: list[dict[str, object]] = [
        {"project_id": "p1", "event_type": "gate_fire", "ts_utc": "2026-06-05T09:00:00Z"},  # not progress
        {"project_id": "p1", "event_type": "phase_complete", "ts_utc": "bad-ts"},  # unparseable
        {"event_type": "phase_complete", "ts_utc": "2026-06-05T09:00:00Z"},  # no project_id
        {"project_id": "p1", "event_type": "phase_complete", "ts_utc": "2026-06-05T03:00:00Z"},  # the only valid
    ]
    hb = latest_heartbeats(events)
    assert hb == {"p1": datetime(2026, 6, 5, 3, 0, tzinfo=UTC)}


def test_empty() -> None:
    assert latest_heartbeats([]) == {}


def test_read_from_log(tmp_path) -> None:  # type: ignore[no-untyped-def]
    log = tmp_path / "events.jsonl"
    log.write_text(
        '{"project_id":"ol_build","event_type":"phase_complete","ts_utc":"2026-06-05T23:47:00Z"}\n'
        "\ngarbage\n"
        '{"project_id":"ol_build","event_type":"llm_call","ts_utc":"2026-06-05T23:50:00Z"}\n',
        encoding="utf-8",
    )
    hb = read_heartbeats_from_log(log)
    assert hb == {"ol_build": datetime(2026, 6, 5, 23, 50, tzinfo=UTC)}


def test_read_from_missing_log() -> None:
    assert read_heartbeats_from_log("/no/such/events.jsonl") == {}
