"""Fleet event-log ingest (Fleet Analytics spec §1).

The bash hooks write each initiative's events to a local ``<state>/logs/events.jsonl`` and could
never sync to the DB (no DB API from the RL/bash side). The OUTER LOOP can: the supervisor holds a
psycopg connection, so each cycle it ships every project's event log into the ``events`` table
(idempotent by ``event_uuid``). This module resolves the per-project log paths (pure); the read +
``Registry.upsert_events`` happen in the ``__main__`` wiring.

Pure + fault-tolerant: :func:`events_file_for` resolves a path, never reads or raises.
"""

from __future__ import annotations

import os
from pathlib import Path

from supervisor.ports import RegistryRow

WORKSPACE_ROOT_ENV = "OL_SUPERVISOR_WORKSPACE_ROOT"


def events_file_for(
    row: RegistryRow, *, workspace_root: str | os.PathLike[str] | None = None
) -> Path | None:
    """The ``events.jsonl`` path for a project row, or ``None`` when its folder is unknown.

    Resolves ``projects.folder_path`` (absolute as-is, else under the workspace root — the
    ``OL_SUPERVISOR_WORKSPACE_ROOT`` env when not supplied) to ``<folder>/state/logs/events.jsonl``,
    the same sibling-``state`` convention the completion probe + learn assembly use."""
    folder = row.get("folder_path")
    if not isinstance(folder, str) or not folder:
        return None
    root = workspace_root if workspace_root is not None else os.environ.get(WORKSPACE_ROOT_ENV)
    folder_path = Path(folder)
    if not folder_path.is_absolute():
        if not root:
            return None
        folder_path = Path(str(root)) / folder_path
    return folder_path / "state" / "logs" / "events.jsonl"


__all__ = ["WORKSPACE_ROOT_ENV", "events_file_for"]
