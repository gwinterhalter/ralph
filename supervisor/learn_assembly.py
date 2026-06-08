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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from supervisor.ports import RegistryRow
from supervisor.run_auditor import TERMINAL_RUN_STATUSES, GateEvent, RunRecord

# --- Event-stream gate-fact assembly (FR-050 Answerer-DSL candidates) ----------
# The per-Run gate facts the Run-Auditor's FR-050 finding keys on. They are NOT carried on a
# ``ralph_runs`` row — they live in the orchestrator's per-initiative event stream
# (``<state>/logs/events.jsonl``, Comprehensive_Event_Log_Spec). These are the event types that
# carry gate identity + escalation + resolution.
_GATE_FIRE = "gate_fire"  # payload.cls == 'gate_human' => the gate was a human gate
_GATE_ESCALATE = "gate_escalate"  # the gate was escalated to the operator (gate_human)
_GATE_RESOLVE = "gate_resolve"  # payload.option carries the chosen option (auto/inline resolutions)
#: The gate-role event types build_gate_events considers; anything else (e.g. an audit_target
#: event whose subject_id is a doc name) is ignored so a non-gate subject_id is never mistaken
#: for a gate.
_GATE_EVENT_TYPES = frozenset({_GATE_FIRE, _GATE_ESCALATE, _GATE_RESOLVE})
_GATE_SUBJECT_KIND = "gate"

# NOTE on the remaining two finding facts: verification-binding pass/fail (FR-051) and
# session-shape revision (FR-052) are NOT present in the event stream — there is no per-binding
# pass/fail event, and shape names are not evented. They stay empty here (no fabrication); lighting
# them up needs those facts emitted (or sourced from reports/plans) — a separate follow-on.


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp (``…Z`` or offset) to a datetime, else None."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def build_gate_events(
    events: "list[dict[str, object]] | tuple[dict[str, object], ...]",
) -> tuple[GateEvent, ...]:
    """Fold a Run's gate events into per-gate :class:`GateEvent` facts (pure; FR-050).

    One :class:`GateEvent` per distinct ``gate_id`` seen, in first-seen order:
    ``escalated_to_gate_human`` is True iff the gate was escalated to the operator
    (``gate_escalate``) or fired as a human gate (``gate_fire`` with ``payload.cls ==
    'gate_human'``); ``resolved_option`` is the option recorded on its ``gate_resolve`` event
    (present for auto/inline resolutions; ``None`` when an operator resolution recorded no option,
    which the FR-050 finding then ignores). Never raises.
    """
    escalated: dict[str, bool] = {}
    option: dict[str, str | None] = {}
    order: list[str] = []
    for event in events:
        event_type = event.get("event_type")
        # Only real gate-role events contribute — never a stray subject_id from another event
        # kind (e.g. an audit_target whose subject_id is a doc name).
        if event_type not in _GATE_EVENT_TYPES and event.get("subject_kind") != _GATE_SUBJECT_KIND:
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        raw_gate = event.get("subject_id") or payload.get("gate_id")
        if not isinstance(raw_gate, str) or not raw_gate:
            continue
        if raw_gate not in escalated:
            escalated[raw_gate] = False
            option[raw_gate] = None
            order.append(raw_gate)
        if event_type == _GATE_ESCALATE:
            escalated[raw_gate] = True
        elif event_type == _GATE_FIRE and payload.get("cls") == "gate_human":
            escalated[raw_gate] = True
        elif event_type == _GATE_RESOLVE:
            chosen = payload.get("option")
            if isinstance(chosen, str) and chosen:
                option[raw_gate] = chosen
    return tuple(
        GateEvent(
            gate_id=gate_id,
            escalated_to_gate_human=escalated[gate_id],
            resolved_option=option[gate_id],
        )
        for gate_id in order
    )


def read_events_jsonl(path: str | Path) -> list[dict[str, object]]:
    """Thin JSONL adapter: read an ``events.jsonl`` into a list of event dicts (fault-tolerant).

    Mirrors :func:`supervisor.heartbeats.read_heartbeats_from_log`'s reader — a missing file or a
    malformed line is skipped, never raised.
    """
    p = Path(path)
    if not p.is_file():
        return []
    events: list[dict[str, object]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def gate_events_from_run(row: RegistryRow) -> tuple[GateEvent, ...]:
    """Assemble a completed Run's gate facts from its initiative event stream (I/O; FR-050).

    Locates the events log at ``<seed dir>/state/logs/events.jsonl`` (the seed's sibling ``state``
    tree, the same convention the production completion probe uses), scopes the events to this Run
    by ``project_id`` AND the ``[spawned_at, terminated_at]`` window (runs are sequential per
    project — the active-run unique index guarantees at most one running Run per project — so the
    window cleanly partitions one project's append-only log into its successive Runs), and folds
    them via :func:`build_gate_events`. Returns ``()`` when the seed/log is absent. Never raises.
    """
    seed = row.get("seed_path")
    if not isinstance(seed, str) or not seed:
        return ()
    events_path = Path(seed).parent / "state" / "logs" / "events.jsonl"
    events = read_events_jsonl(events_path)
    if not events:
        return ()
    slug = row.get("project_id") or row.get("project_slug")
    start = _parse_ts(row.get("spawned_at"))
    end = _parse_ts(row.get("terminated_at"))
    scoped: list[dict[str, object]] = []
    for event in events:
        if isinstance(slug, str) and slug and event.get("project_id") not in (slug, None):
            continue
        ts = _parse_ts(event.get("ts_utc")) or _parse_ts(event.get("ts"))
        if start is not None and ts is not None and ts < start:
            continue
        if end is not None and ts is not None and ts > end:
            continue
        scoped.append(event)
    return build_gate_events(scoped)


def completed_run_records(
    rows: "list[RegistryRow] | tuple[RegistryRow, ...]",
    *,
    gate_events_for: Callable[[RegistryRow], tuple[GateEvent, ...]] | None = None,
) -> list[RunRecord]:
    """Map terminal ``ralph_runs`` rows into Run-Auditor :class:`RunRecord`s.

    Keeps only rows whose ``status`` is a canonical terminal status
    (:data:`~supervisor.run_auditor.TERMINAL_RUN_STATUSES`). When ``gate_events_for`` is supplied
    (production wires :func:`gate_events_from_run`), each record carries the Run's gate facts so the
    FR-050 Answerer-DSL finding can fire; without it (the default) the gate facts are empty. The
    verification-binding (FR-051) and session-shape (FR-052) facts are always empty — those are not
    in the event stream (see module note), so they are never fabricated.
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
                gate_events=gate_events_for(row) if gate_events_for is not None else (),
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
    "build_gate_events",
    "completed_run_records",
    "gate_events_from_run",
    "learning_records",
    "read_events_jsonl",
    "render_learning_corpus",
]
