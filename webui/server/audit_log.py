"""Operator action log — an append-only record of every action taken through the control panel.

Accountability gap from the design (§7): nothing recorded who paused / adopted / resolved what,
when. Each API action appends one JSON line to ``<state_dir>/operator_actions.jsonl``; the GUI reads
the tail. The clock is the caller's (injected ``now_iso``) so the writer stays deterministic in
tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_LOG_NAME = "operator_actions.jsonl"


def append_action(
    state_dir: str | Path,
    *,
    action: str,
    target: str,
    by: str,
    now_iso: str,
    detail: str = "",
) -> dict[str, Any]:
    """Append one operator action to the log and return the written record."""
    record: dict[str, Any] = {"ts": now_iso, "action": action, "target": target, "by": by, "detail": detail}
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    with (d / _LOG_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def read_actions(state_dir: str | Path, *, limit: int = 100) -> list[dict[str, Any]]:
    """Most-recent-first tail of the operator action log (skips blank/garbled lines)."""
    path = Path(state_dir) / _LOG_NAME
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    records.reverse()
    return records[:limit]


__all__ = ["append_action", "read_actions"]
