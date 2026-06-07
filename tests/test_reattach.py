"""D5 — FR-013 Run re-attach decisions (supervisor.reattach)."""

from __future__ import annotations

import pytest

from supervisor.reattach import (
    DECISION_ORPHAN_DEAD,
    DECISION_ORPHAN_REUSED,
    DECISION_REATTACH,
    derive_reattach_decisions,
)

pytestmark = pytest.mark.unit


def _run(pid: object, *, project_id: str = "p", start: str | None = None) -> dict[str, object]:
    row: dict[str, object] = {"project_id": project_id}
    if pid is not None:
        row["orchestrator_pid"] = pid
    if start is not None:
        row["pid_start_time"] = start
    return row


def _decide(rows, *, alive, start):  # type: ignore[no-untyped-def]
    return derive_reattach_decisions(
        rows, pid_alive=lambda _p: alive, pid_start_time=lambda _p: start
    )


def test_dead_pid_is_orphan() -> None:
    d = _decide([_run(100, start="2026-06-05T04:00:00Z")], alive=False, start=None)
    assert d[0].decision == DECISION_ORPHAN_DEAD
    assert d[0].is_orphan and not d[0].is_reattach


def test_alive_matching_start_reattaches() -> None:
    d = _decide([_run(100, start="2026-06-05T04:00:00Z")], alive=True, start="2026-06-05T04:00:00Z")
    assert d[0].decision == DECISION_REATTACH
    assert d[0].is_reattach


def test_alive_mismatched_start_is_orphan_reused() -> None:
    # pid recycled: alive, but the live process started at a different time than recorded.
    d = _decide([_run(100, start="2026-06-05T04:00:00Z")], alive=True, start="2026-06-06T09:00:00Z")
    assert d[0].decision == DECISION_ORPHAN_REUSED
    assert d[0].is_orphan


def test_alive_unknown_start_reattaches_conservatively() -> None:
    # No recorded/live start-time to disambiguate → never reap a live pid.
    d = _decide([_run(100)], alive=True, start=None)
    assert d[0].decision == DECISION_REATTACH


def test_no_pid_row_skipped() -> None:
    assert _decide([_run(None)], alive=True, start="x") == []
    assert _decide([_run("not-an-int")], alive=True, start="x") == []
