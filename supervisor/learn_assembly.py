"""Item 2 — live assembly for the §4.4 step-6 Learn pass.

The OLB-15 Run-Auditor (:mod:`supervisor.run_auditor`) and its wiring
(:func:`supervisor.cycle_wiring.run_learn_step`) were built read-only over *supplied*
:class:`~supervisor.run_auditor.RunRecord`s; the production ``runs_source`` was a
``lambda: []`` stub, so the step audited nothing. This module is the production adapter
that turns the live terminal ``ralph_runs`` rows (``Registry.read_completed_runs``) into
the Learn step's inputs:

* :func:`completed_run_records` — maps each terminal row into a Run-Auditor
  :class:`~supervisor.run_auditor.RunRecord`. The gate / verification-binding / session-shape
  *facts* the FR-050/051/052 findings key on are NOT carried on a ``ralph_runs`` row — they
  live in the per-Run event stream; assembling them is the remaining OLB-16/C5 work the
  Run-Auditor docstring reserves. Until then the records carry their terminal identity with
  empty fact tuples (the audit runs and persists, fabricating nothing).
* :func:`learning_records` / :func:`render_learning_corpus` — the small persisted
  cost+duration+status corpus (one snapshot row per completed Run, keyed by ``run_id``) that
  feeds Phase-1 milestone sizing, the scheduler's closest-to-done bias, and a future
  cost-forecast surface. Derived straight from the ``ralph_runs`` columns.

Pure + fault-tolerant (mirrors :mod:`supervisor.candidate_enrichment`): no I/O, no wall-clock,
no ``supervisor.registry`` import; a row whose fields cannot be parsed contributes what it can
and never raises. The live read + file persistence live in the ``__main__`` wiring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from supervisor.ports import RegistryRow
from supervisor.run_auditor import TERMINAL_RUN_STATUSES, RunRecord


def completed_run_records(rows: "list[RegistryRow] | tuple[RegistryRow, ...]") -> list[RunRecord]:
    """Map terminal ``ralph_runs`` rows into Run-Auditor :class:`RunRecord`s.

    Keeps only rows whose ``status`` is a canonical terminal status
    (:data:`~supervisor.run_auditor.TERMINAL_RUN_STATUSES`). Fact collections are left empty
    (the event-stream fact assembly is the OLB-16 follow-on); the records carry ``run_id`` /
    ``project_slug`` / ``status`` so the pass runs over real completed Runs and persists.
    """
    records: list[RunRecord] = []
    for row in rows:
        status = str(row.get("status", ""))
        if status not in TERMINAL_RUN_STATUSES:
            continue
        records.append(
            RunRecord(
                run_id=str(row.get("run_id", "")),
                project_slug=str(row.get("project_id") or row.get("project_slug") or ""),
                status=status,
            )
        )
    return records


@dataclass(frozen=True)
class LearningRecord:
    """One completed Run's cost/duration learning fact (Item 2 persisted corpus)."""

    run_id: str
    project_slug: str
    status: str
    cost_usd: Decimal | None
    duration_seconds: float | None


def _as_decimal(value: object) -> Decimal | None:
    """Coerce a money value to ``Decimal``, or ``None`` (booleans rejected)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    return None


def _duration_seconds(spawned_at: object, terminated_at: object) -> float | None:
    """Seconds between two ISO-8601 instants, or ``None`` when either is unparseable."""
    if not isinstance(spawned_at, str) or not isinstance(terminated_at, str):
        return None
    try:
        start = datetime.fromisoformat(spawned_at)
        end = datetime.fromisoformat(terminated_at)
    except ValueError:
        return None
    return (end - start).total_seconds()


def learning_records(rows: "list[RegistryRow] | tuple[RegistryRow, ...]") -> list[LearningRecord]:
    """Derive the per-completed-Run cost/duration/status learning facts from ``ralph_runs`` rows."""
    records: list[LearningRecord] = []
    for row in rows:
        status = str(row.get("status", ""))
        if status not in TERMINAL_RUN_STATUSES:
            continue
        records.append(
            LearningRecord(
                run_id=str(row.get("run_id", "")),
                project_slug=str(row.get("project_id") or row.get("project_slug") or ""),
                status=status,
                cost_usd=_as_decimal(row.get("terminal_cost_usd")),
                duration_seconds=_duration_seconds(
                    row.get("spawned_at"), row.get("terminated_at")
                ),
            )
        )
    return records


def render_learning_corpus(records: "list[LearningRecord] | tuple[LearningRecord, ...]") -> str:
    """Render the learning corpus as deterministic JSONL (one object per Run, sorted by run_id).

    A current snapshot — re-rendering the same completed-Run set yields the identical text, so the
    persisted corpus is idempotent (no per-cycle duplication). ``cost_usd`` is serialised as a
    string to preserve exact decimals (NFR-007); a ``None`` cost/duration is emitted as JSON null.
    """
    lines: list[str] = []
    for record in sorted(records, key=lambda r: r.run_id):
        lines.append(
            json.dumps(
                {
                    "run_id": record.run_id,
                    "project_slug": record.project_slug,
                    "status": record.status,
                    "cost_usd": None if record.cost_usd is None else str(record.cost_usd),
                    "duration_seconds": record.duration_seconds,
                },
                sort_keys=True,
            )
        )
    return "\n".join(lines)


__all__ = [
    "LearningRecord",
    "completed_run_records",
    "learning_records",
    "render_learning_corpus",
]
