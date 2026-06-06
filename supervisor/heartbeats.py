"""Heartbeat-pointer reader — the live FR-005 / FR-062 staleness source (D4).

The §13 status surface judges FR-062 stale-heartbeat state from an injected
``heartbeats`` map, and the §4.4(1) Reconcile step judges a stall from an injected
``progress_at`` (default ``spawned_at``); both deferred the *live* source. Per
FUP-0830 the canonical progress signal is the event stream's ``phase_complete``
(plus the other per-iteration progress events). This module folds the event log into
``{project_id: last_progress_instant}`` — the single live source that feeds BOTH the
surface's ``heartbeats`` map and reconcile's ``progress_at`` lookup.

Pure: ``latest_heartbeats`` reads only the supplied event dicts (no wall-clock, no
DB); ``read_heartbeats_from_log`` is the thin JSONL adapter over ``logs/events.jsonl``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

#: Event types that count as a Run making progress (a heartbeat). Anything outside
#: this set (e.g. gate_fire, run_start) is not by itself a liveness signal.
HEARTBEAT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "phase_complete",
        "llm_call",
        "role_complete",
        "iteration_end",
        "heartbeat",
    }
)


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp (``…Z`` or offset) to a datetime, else None."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _event_type(event: dict[str, object]) -> str | None:
    for key in ("event_type", "subject_kind", "type"):
        val = event.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def latest_heartbeats(
    events: Iterable[dict[str, object]],
    *,
    event_types: frozenset[str] = HEARTBEAT_EVENT_TYPES,
) -> dict[str, datetime]:
    """Fold events into ``{project_id: latest progress instant}`` (pure; FR-005/062).

    Considers only events whose type is in ``event_types`` and that carry a
    ``project_id`` + a parseable ``ts_utc`` (or ``ts``); keeps the newest per project.
    An empty ``event_types`` set considers every event. Never raises.
    """
    latest: dict[str, datetime] = {}
    for event in events:
        project_id = event.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            continue
        if event_types:
            etype = _event_type(event)
            if etype is None or etype not in event_types:
                continue
        ts = _parse_ts(event.get("ts_utc")) or _parse_ts(event.get("ts"))
        if ts is None:
            continue
        current = latest.get(project_id)
        if current is None or ts > current:
            latest[project_id] = ts
    return latest


def read_heartbeats_from_log(events_path: str | Path) -> dict[str, datetime]:
    """Thin JSONL adapter: read ``logs/events.jsonl`` → :func:`latest_heartbeats`."""
    path = Path(events_path)
    if not path.is_file():
        return {}
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return latest_heartbeats(events)


__all__ = [
    "HEARTBEAT_EVENT_TYPES",
    "latest_heartbeats",
    "read_heartbeats_from_log",
]
