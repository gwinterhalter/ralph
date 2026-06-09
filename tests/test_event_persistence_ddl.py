"""ol5 — fleet event-persistence migration declares the events table the Registry depends on."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DDL = (
    Path(__file__).resolve().parent.parent / "migrations" / "ol5_event_persistence.sql"
).read_text(encoding="utf-8")


def test_declares_events_table() -> None:
    assert "CREATE TABLE IF NOT EXISTS events" in _DDL
    for col in (
        "event_uuid",
        "schema_version",
        "project_id",
        "event_type",
        "ts_utc",
        "payload",
        "subject_id",
        "subject_kind",
    ):
        assert col in _DDL, f"events missing {col}"
    assert "event_uuid       uuid NOT NULL UNIQUE" in _DDL  # ingest idempotency key (canonical)


def test_additive_idempotent_and_indexed() -> None:
    upper = _DDL.upper()
    assert "DROP TABLE" not in upper
    assert "CREATE TABLE IF NOT EXISTS" in upper
    assert "CREATE INDEX IF NOT EXISTS" in upper
