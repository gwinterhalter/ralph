"""Fleet event ingest — per-project events.jsonl path resolution (Fleet Analytics §1)."""
from __future__ import annotations

from pathlib import Path

import pytest

from supervisor.event_ingest import events_file_for

pytestmark = pytest.mark.unit


def test_absolute_folder() -> None:
    path = events_file_for({"folder_path": r"K:\proj"})
    assert path == Path(r"K:\proj") / "state" / "logs" / "events.jsonl"


def test_relative_folder_under_root() -> None:
    path = events_file_for({"folder_path": "sub"}, workspace_root=r"K:\root")
    assert path == Path(r"K:\root") / "sub" / "state" / "logs" / "events.jsonl"


def test_relative_folder_without_root_is_none() -> None:
    assert events_file_for({"folder_path": "sub"}, workspace_root=None) is None


def test_missing_folder_is_none() -> None:
    assert events_file_for({"project_id": "p"}) is None
