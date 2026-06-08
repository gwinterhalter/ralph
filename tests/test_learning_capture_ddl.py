"""Item 2 DB capture — the ol3 learning-capture migration declares the tables the Registry writes.

DB-free consistency check (mirrors test_migration_ddl.py): the ol3 .sql must CREATE the two
learning-capture tables additively + idempotently, with the columns/keys the Registry's
upsert/read methods depend on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DDL = (
    Path(__file__).resolve().parent.parent / "migrations" / "ol3_learning_capture.sql"
).read_text(encoding="utf-8")


def test_declares_learning_records_table() -> None:
    assert "CREATE TABLE IF NOT EXISTS learning_records" in _DDL
    for col in ("run_id", "project_slug", "status", "cost_usd", "duration_seconds"):
        assert col in _DDL, f"learning_records missing column {col}"
    assert "run_id            text PRIMARY KEY" in _DDL  # UPSERT key


def test_declares_run_audit_findings_table() -> None:
    assert "CREATE TABLE IF NOT EXISTS run_audit_findings" in _DDL
    for col in (
        "finding_key",
        "kind",
        "subject",
        "binding_class",
        "evidence",
        "recommendation",
        "routes_to",
        "runs_audited",
    ):
        assert col in _DDL, f"run_audit_findings missing column {col}"
    assert "finding_key       text PRIMARY KEY" in _DDL  # UPSERT + dedup key


def test_additive_and_idempotent() -> None:
    upper = _DDL.upper()
    assert "DROP TABLE" not in upper
    assert "DROP COLUMN" not in upper
    assert upper.count("CREATE TABLE IF NOT EXISTS") == 2
