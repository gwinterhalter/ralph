"""Tests for the per-run filesystem signals (progress-based stall + pending-gate)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor.run_signals import has_pending_gate, latest_progress_ts

pytestmark = pytest.mark.unit


def _seed_with_state(tmp_path: Path) -> str:
    """Create <tmp>/seed.source.md with a sibling state/ tree; return the seed path."""
    state = tmp_path / "state"
    (state / "logs").mkdir(parents=True)
    (state / "escalations").mkdir(parents=True)
    (state / "iterations" / "0001").mkdir(parents=True)
    seed = tmp_path / "seed.source.md"
    seed.write_text("seed", encoding="utf-8")
    return str(seed)


def _write_events(seed_path: str, *events: dict[str, object]) -> None:
    state = Path(seed_path).parent / "state"
    log = state / "logs" / "events.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


# --- latest_progress_ts -------------------------------------------------------------

def test_latest_progress_ts_returns_newest_heartbeat_event(tmp_path: Path) -> None:
    seed = _seed_with_state(tmp_path)
    _write_events(
        seed,
        {"project_id": "p", "event_type": "iteration_start", "ts_utc": "2026-06-10T09:00:00Z"},
        {"project_id": "p", "event_type": "llm_call", "ts_utc": "2026-06-10T09:30:00Z"},
        {"project_id": "p", "event_type": "iteration_end", "ts_utc": "2026-06-10T09:45:00Z"},
    )
    # newest HEARTBEAT event (iteration_end at 09:45) wins; iteration_start is not a beat
    assert latest_progress_ts(seed) == "2026-06-10T09:45:00+00:00"


def test_latest_progress_ts_none_when_no_log_or_no_seed(tmp_path: Path) -> None:
    seed = _seed_with_state(tmp_path)  # no events written
    assert latest_progress_ts(seed) is None
    assert latest_progress_ts(None) is None
    assert latest_progress_ts("") is None


def test_latest_progress_ts_is_fresh_on_each_call(tmp_path: Path) -> None:
    seed = _seed_with_state(tmp_path)
    _write_events(seed, {"project_id": "p", "event_type": "llm_call", "ts_utc": "2026-06-10T09:00:00Z"})
    assert latest_progress_ts(seed) == "2026-06-10T09:00:00+00:00"
    # a later progress event must be reflected immediately (no startup snapshot)
    _write_events(seed, {"project_id": "p", "event_type": "llm_call", "ts_utc": "2026-06-10T10:00:00Z"})
    assert latest_progress_ts(seed) == "2026-06-10T10:00:00+00:00"


# --- has_pending_gate ---------------------------------------------------------------

def test_pending_gate_true_when_request_without_response(tmp_path: Path) -> None:
    seed = _seed_with_state(tmp_path)
    esc = Path(seed).parent / "state" / "escalations"
    (esc / "gate_request_0001_0001.json").write_text("{}", encoding="utf-8")
    assert has_pending_gate(seed) is True


def test_pending_gate_false_when_response_in_iteration_dir(tmp_path: Path) -> None:
    seed = _seed_with_state(tmp_path)
    state = Path(seed).parent / "state"
    (state / "escalations" / "gate_request_0001_0001.json").write_text("{}", encoding="utf-8")
    (state / "iterations" / "0001" / "gate_response_0001_0001.json").write_text("{}", encoding="utf-8")
    assert has_pending_gate(seed) is False


def test_pending_gate_false_when_no_escalations(tmp_path: Path) -> None:
    seed = _seed_with_state(tmp_path)
    assert has_pending_gate(seed) is False
    assert has_pending_gate(None) is False


def test_pending_gate_true_if_any_request_unanswered(tmp_path: Path) -> None:
    seed = _seed_with_state(tmp_path)
    state = Path(seed).parent / "state"
    esc = state / "escalations"
    (esc / "gate_request_0001_0001.json").write_text("{}", encoding="utf-8")
    (state / "iterations" / "0001" / "gate_response_0001_0001.json").write_text("{}", encoding="utf-8")
    (esc / "gate_request_0002_0001.json").write_text("{}", encoding="utf-8")  # unanswered
    assert has_pending_gate(seed) is True
