"""T3#5 — preflight schema/env gate (supervisor.preflight, pure verdict)."""

from __future__ import annotations

import pytest

from supervisor.preflight import (
    REQUIRED_ACTIVE_RUN_INDEX,
    REQUIRED_PROJECT_COLUMNS,
    REQUIRED_RALPH_RUNS_COLUMNS,
    SchemaSnapshot,
    evaluate_preflight,
)

pytestmark = pytest.mark.unit


def _good_snapshot(**overrides: object) -> SchemaSnapshot:
    base: dict[str, object] = {
        "can_connect": True,
        "project_columns": frozenset(REQUIRED_PROJECT_COLUMNS),
        "ralph_runs_columns": frozenset(REQUIRED_RALPH_RUNS_COLUMNS),
        "ralph_runs_not_null": frozenset({"seed_path"}),
        "indexes": frozenset({REQUIRED_ACTIVE_RUN_INDEX}),
    }
    base.update(overrides)
    return SchemaSnapshot(**base)  # type: ignore[arg-type]


def test_healthy_substrate_passes() -> None:
    result = evaluate_preflight(_good_snapshot())
    assert result.ok is True
    assert result.failures == ()


def test_cannot_connect_fails() -> None:
    result = evaluate_preflight(_good_snapshot(can_connect=False))
    assert result.ok is False
    assert any("connect" in f for f in result.failures)


def test_missing_project_column_fails() -> None:
    result = evaluate_preflight(
        _good_snapshot(project_columns=REQUIRED_PROJECT_COLUMNS - {"lifecycle_state"})
    )
    assert result.ok is False
    assert any("projects missing columns" in f and "lifecycle_state" in f for f in result.failures)


def test_missing_ralph_runs_column_fails() -> None:
    result = evaluate_preflight(
        _good_snapshot(ralph_runs_columns=REQUIRED_RALPH_RUNS_COLUMNS - {"seed_path"})
    )
    assert result.ok is False
    assert any("ralph_runs missing columns" in f for f in result.failures)


def test_seed_path_nullable_fails() -> None:
    # The exact iter-0017 production-shape regression: seed_path must be NOT NULL.
    result = evaluate_preflight(_good_snapshot(ralph_runs_not_null=frozenset()))
    assert result.ok is False
    assert any("NOT NULL" in f and "seed_path" in f for f in result.failures)


def test_missing_active_run_index_fails() -> None:
    result = evaluate_preflight(_good_snapshot(indexes=frozenset()))
    assert result.ok is False
    assert any(REQUIRED_ACTIVE_RUN_INDEX in f for f in result.failures)


def test_multiple_failures_all_reported() -> None:
    result = evaluate_preflight(
        SchemaSnapshot(
            can_connect=False,
            project_columns=frozenset(),
            ralph_runs_columns=frozenset(),
            ralph_runs_not_null=frozenset(),
            indexes=frozenset(),
        )
    )
    assert result.ok is False
    assert len(result.failures) >= 4
