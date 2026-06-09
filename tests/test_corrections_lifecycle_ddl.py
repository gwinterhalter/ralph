"""ol4 — correction-capture + finding-lifecycle migration declares what the Registry depends on."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DDL = (
    Path(__file__).resolve().parent.parent / "migrations" / "ol4_corrections_and_lifecycle.sql"
).read_text(encoding="utf-8")


def test_declares_correction_attempts_table() -> None:
    assert "CREATE TABLE IF NOT EXISTS correction_attempts" in _DDL
    for col in ("event_uuid", "project_slug", "attempt", "level", "item_id"):
        assert col in _DDL, f"correction_attempts missing {col}"
    assert "event_uuid       text PRIMARY KEY" in _DDL


def test_declares_lifecycle_columns() -> None:
    for col in ("status", "authoring_skill", "decided_by", "decided_at"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in _DDL or f"IF NOT EXISTS {col}" in _DDL, col
    for state in ("proposed", "accepted", "applied", "rejected"):
        assert f"'{state}'" in _DDL


def test_additive_and_idempotent() -> None:
    upper = _DDL.upper()
    assert "DROP TABLE" not in upper and "DROP COLUMN" not in upper
    assert "CREATE TABLE IF NOT EXISTS" in upper
    assert "ADD COLUMN IF NOT EXISTS" in upper
