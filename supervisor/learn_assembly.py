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

from supervisor.attention import ESCALATION_KIND_ROUTINE, Escalation
from supervisor.ports import RegistryRow
from supervisor.run_auditor import (
    TERMINAL_RUN_STATUSES,
    AuditFinding,
    BindingOutcome,
    GateEvent,
    RunRecord,
    ShapeUsage,
    finding_key,
)

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

# FR-051 verification-binding pass/fail facts: a `verification` event per binding the consumer
# checked (subject_kind 'binding'; payload {binding, result|passed}). Emitted via the
# `lib/events.sh verification` CLI the consumer calls.
_VERIFICATION = "verification"
_BINDING_SUBJECT_KIND = "binding"
_PASS_TOKENS = frozenset({"pass", "passed", "ok", "success", "true"})

# FR-052 session-shape revision facts: the `revise_round` event (already emitted per plan-review
# round) carries verdict/findings; with the plan `shape` added to its payload, one shape-use per
# iteration is recovered (required_reviewer_revision iff any round needed revision).
_REVISE_ROUND = "revise_round"

# Correction history: the cf-correction-agent `correction_attempt` events (L1-L4 patch retries),
# payload {attempt, level, item_id}; subject_id is the item under correction.
_CORRECTION_ATTEMPT = "correction_attempt"


def _as_int(value: object) -> int | None:
    """Coerce to int, or None (booleans rejected)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


@dataclass(frozen=True)
class CorrectionAttempt:
    """One captured ``correction_attempt`` event (raw, for the correction_attempts DB table)."""

    event_uuid: str
    project_slug: str
    iteration_index: int | None
    attempt: int | None
    level: str
    item_id: str
    ts_utc: str | None


@dataclass(frozen=True)
class RunFacts:
    """The per-Run Run-Auditor facts assembled from one Run's event stream (FR-050/051/052)."""

    gate_events: tuple[GateEvent, ...] = ()
    binding_outcomes: tuple[BindingOutcome, ...] = ()
    shape_usages: tuple[ShapeUsage, ...] = ()


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


def build_binding_outcomes(
    events: "list[dict[str, object]] | tuple[dict[str, object], ...]",
) -> tuple[BindingOutcome, ...]:
    """Fold a Run's ``verification`` events into per-binding :class:`BindingOutcome` facts (FR-051).

    One :class:`BindingOutcome` per ``verification`` event whose ``subject_kind`` is ``binding``;
    ``passed`` is taken from ``payload.passed`` (bool) or ``payload.result`` (a pass token like
    ``pass``/``ok``/``success``). Events lacking a binding name or a resolvable pass/fail are
    skipped. Never raises.
    """
    outcomes: list[BindingOutcome] = []
    for event in events:
        if event.get("event_type") != _VERIFICATION or event.get("subject_kind") != _BINDING_SUBJECT_KIND:
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        binding = event.get("subject_id") or payload.get("binding")
        if not isinstance(binding, str) or not binding:
            continue
        passed_value = payload.get("passed")
        result = payload.get("result")
        if isinstance(passed_value, bool):
            passed = passed_value
        elif isinstance(result, str) and result:
            passed = result.strip().lower() in _PASS_TOKENS
        else:
            continue
        outcomes.append(BindingOutcome(binding=binding, passed=passed))
    return tuple(outcomes)


def build_shape_usages(
    events: "list[dict[str, object]] | tuple[dict[str, object], ...]",
) -> tuple[ShapeUsage, ...]:
    """Fold a Run's ``revise_round`` events into per-iteration :class:`ShapeUsage` facts (FR-052).

    Each iteration that ran the plan-review loop is one shape-use, keyed by ``(iteration_index,
    shape)`` (the ``shape`` is read from the ``revise_round`` payload). ``required_reviewer_revision``
    is True iff ANY round for that use needed revision — a non-``converged`` verdict OR a positive
    ``findings_blocker`` / ``findings_drift`` count. A ``revise_round`` lacking a ``shape`` is
    skipped (the field is only present once the orchestrator stamps it). Never raises.
    """
    required: dict[tuple[object, str], bool] = {}
    shape_of: dict[tuple[object, str], str] = {}
    order: list[tuple[object, str]] = []
    for event in events:
        if event.get("event_type") != _REVISE_ROUND:
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        shape = payload.get("shape")
        if not isinstance(shape, str) or not shape:
            continue
        key = (event.get("iteration_index"), shape)
        if key not in required:
            required[key] = False
            shape_of[key] = shape
            order.append(key)
        verdict = payload.get("verdict")
        fb = payload.get("findings_blocker")
        fd = payload.get("findings_drift")
        needed = (
            (verdict is not None and verdict != "converged")
            or (isinstance(fb, int) and not isinstance(fb, bool) and fb > 0)
            or (isinstance(fd, int) and not isinstance(fd, bool) and fd > 0)
        )
        if needed:
            required[key] = True
    return tuple(
        ShapeUsage(shape=shape_of[key], required_reviewer_revision=required[key])
        for key in order
    )


def build_correction_attempts(
    events: "list[dict[str, object]] | tuple[dict[str, object], ...]",
) -> list[CorrectionAttempt]:
    """Fold ``correction_attempt`` events into :class:`CorrectionAttempt` rows (for DB capture).

    Keyed by ``event_uuid`` (so re-reading the append-only log is idempotent). An event lacking an
    event_uuid is skipped. Never raises."""
    attempts: list[CorrectionAttempt] = []
    for event in events:
        if event.get("event_type") != _CORRECTION_ATTEMPT:
            continue
        uuid = event.get("event_uuid")
        if not isinstance(uuid, str) or not uuid:
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        item = event.get("subject_id") or payload.get("item_id") or ""
        ts = event.get("ts_utc")
        attempts.append(
            CorrectionAttempt(
                event_uuid=uuid,
                project_slug=str(event.get("project_id") or event.get("initiative_slug") or ""),
                iteration_index=_as_int(event.get("iteration_index")),
                attempt=_as_int(payload.get("attempt")),
                level=str(payload.get("level") or ""),
                item_id=str(item),
                ts_utc=ts if isinstance(ts, str) and ts else None,
            )
        )
    return attempts


def assemble_run_facts(
    events: "list[dict[str, object]] | tuple[dict[str, object], ...]",
) -> RunFacts:
    """Assemble all three Run-Auditor fact kinds from one Run's events (FR-050/051/052)."""
    return RunFacts(
        gate_events=build_gate_events(events),
        binding_outcomes=build_binding_outcomes(events),
        shape_usages=build_shape_usages(events),
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


def scoped_events_for_run(row: RegistryRow) -> list[dict[str, object]]:
    """Read + scope a completed Run's events from its initiative event stream (I/O).

    Locates the events log at ``<seed dir>/state/logs/events.jsonl`` (the seed's sibling ``state``
    tree, the same convention the production completion probe uses) and scopes the events to this
    Run by ``project_id`` AND the ``[spawned_at, terminated_at]`` window (runs are sequential per
    project — the active-run unique index guarantees at most one running Run per project — so the
    window cleanly partitions one project's append-only log into its successive Runs). Returns
    ``[]`` when the seed/log is absent. Never raises.
    """
    seed = row.get("seed_path")
    if not isinstance(seed, str) or not seed:
        return []
    events = read_events_jsonl(Path(seed).parent / "state" / "logs" / "events.jsonl")
    if not events:
        return []
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
    return scoped


def gate_events_from_run(row: RegistryRow) -> tuple[GateEvent, ...]:
    """A completed Run's gate facts from its event stream (I/O; FR-050) — :func:`build_gate_events`
    over :func:`scoped_events_for_run`."""
    return build_gate_events(scoped_events_for_run(row))


def run_facts_from_run(row: RegistryRow) -> RunFacts:
    """All three of a completed Run's Run-Auditor fact kinds from its event stream (I/O).

    Reads + window-scopes the Run's events once and assembles the gate (FR-050), verification-binding
    (FR-051), and session-shape (FR-052) facts. Returns an empty :class:`RunFacts` when the
    seed/log is absent. Never raises.
    """
    return assemble_run_facts(scoped_events_for_run(row))


def completed_run_records(
    rows: "list[RegistryRow] | tuple[RegistryRow, ...]",
    *,
    facts_for: Callable[[RegistryRow], RunFacts] | None = None,
) -> list[RunRecord]:
    """Map terminal ``ralph_runs`` rows into Run-Auditor :class:`RunRecord`s.

    Keeps only rows whose ``status`` is a canonical terminal status
    (:data:`~supervisor.run_auditor.TERMINAL_RUN_STATUSES`). When ``facts_for`` is supplied
    (production wires :func:`run_facts_from_run`), each record carries the Run's assembled gate /
    verification-binding / session-shape facts so the FR-050/051/052 findings can fire; without it
    (the default) the fact collections are empty — never fabricated.
    """
    records: list[RunRecord] = []
    for row in rows:
        status = str(row.get("status", ""))
        if status not in TERMINAL_RUN_STATUSES:
            continue
        facts = facts_for(row) if facts_for is not None else RunFacts()
        records.append(
            RunRecord(
                run_id=str(row.get("run_id", "")),
                project_slug=str(row.get("project_id") or row.get("project_slug") or ""),
                status=status,
                gate_events=facts.gate_events,
                binding_outcomes=facts.binding_outcomes,
                shape_usages=facts.shape_usages,
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


def findings_to_escalations(
    findings: "list[AuditFinding] | tuple[AuditFinding, ...]",
    *,
    new_keys: set[str],
    now: datetime,
    project_id: str = "*fleet*",
    confidence: float = 0.85,
) -> list[Escalation]:
    """Convert NEW Run-Auditor findings into routine one-confirm operator escalations (auto-feedback).

    Only findings whose :func:`~supervisor.run_auditor.finding_key` is in ``new_keys`` are surfaced
    (the ``run_audit_findings`` table dedups across passes → each learning is raised exactly once).
    Each becomes a ROUTINE :class:`~supervisor.attention.Escalation` (batched / Quiet-Hours-deferrable
    — a learning is not urgent) carrying the finding's ``recommendation`` as the ``suggested_option``
    and a ``confidence`` the FR-032 one-confirm path reads; ``reversible=True`` (adopting a learning is
    a reversible config edit). The next Attend pass delivers it via the notification port (Item 3), so
    the operator is told the learning + a suggested action and can one-confirm. Pure — ``now`` injected,
    no I/O. The findings already cleared the FR-050/051/052 consistency thresholds, hence the
    confidence default sits above the seed one-confirm threshold (0.7)."""
    escalations: list[Escalation] = []
    for finding in findings:
        key = finding_key(finding)
        if key not in new_keys:
            continue
        escalations.append(
            Escalation(
                project_id=project_id,
                gate_id=f"learning:{key}",
                kind=ESCALATION_KIND_ROUTINE,
                reversible=True,
                suggested_option=finding.recommendation,
                confidence=confidence,
                raised_at=now,
            )
        )
    return escalations


__all__ = [
    "CorrectionAttempt",
    "LearningRecord",
    "RunFacts",
    "assemble_run_facts",
    "build_correction_attempts",
    "findings_to_escalations",
    "scoped_events_for_run",
    "build_binding_outcomes",
    "build_gate_events",
    "build_shape_usages",
    "completed_run_records",
    "gate_events_from_run",
    "learning_records",
    "read_events_jsonl",
    "render_learning_corpus",
    "run_facts_from_run",
]
