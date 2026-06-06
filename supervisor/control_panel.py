"""Operator control panel — live status + control inputs + metrics (robustness T4#8).

The Status Surface (OLB-04 / OLB-16) shipped as a render core with a thin reader
loop (``run_status_loop``) but no runnable entrypoint, and the operator control
inputs were "hand-drop a command JSON into ``state/commands/``". This module is the
thin unifying entrypoint: ``python -m supervisor.control_panel`` renders the live
fleet on the FR-061 bounded refresh, writes the operator command JSONs (pause /
bump-budget / query) the orchestrator's command_dispatch already consumes, and
summarises the event log.

The decision/IO-free core is unit-tested: ``write_command`` (builds a
schema-conformant command file) and ``summarize_events`` (pure metrics fold over the
event log). The live status loop and the CLI are thin adapters over those plus the
OLB-16 surface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

#: The operator command types the orchestrator's command_dispatch consumes.
COMMAND_TYPES: frozenset[str] = frozenset(
    {"pause", "bump_budget", "query_register_state"}
)


def write_command(
    state_dir: str | Path,
    command_type: str,
    *,
    command_id: str,
    issued_by: str,
    issued_at: str,
    reason: str | None = None,
    new_cap_usd: str | None = None,
) -> Path:
    """Write a schema-conformant operator command to ``<state_dir>/commands/``.

    Carries the required ``command_type`` / ``command_id`` / ``issued_by`` /
    ``issued_at`` fields (+ ``new_cap_usd`` for ``bump_budget``, ``reason`` when
    given) that command_dispatch reads. ``command_id`` / ``issued_at`` are supplied
    by the caller (the CLI stamps them) so this stays deterministic and testable.
    """
    if command_type not in COMMAND_TYPES:
        raise ValueError(
            f"unknown command_type {command_type!r}; expected one of {sorted(COMMAND_TYPES)}"
        )
    if command_type == "bump_budget" and new_cap_usd is None:
        raise ValueError("bump_budget requires new_cap_usd")

    payload: dict[str, object] = {
        "command_type": command_type,
        "command_id": command_id,
        "issued_by": issued_by,
        "issued_at": issued_at,
    }
    if reason is not None:
        payload["reason"] = reason
    if command_type == "bump_budget" and new_cap_usd is not None:
        payload["new_cap_usd"] = new_cap_usd

    commands_dir = Path(state_dir) / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    path = commands_dir / f"{command_id}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


@dataclass(frozen=True)
class EventMetrics:
    """A summary fold over the event log for the metrics pane."""

    total: int
    by_type: dict[str, int] = field(default_factory=dict)
    total_cost_usd: Decimal = Decimal("0")
    failures: int = 0


def _event_type(event: dict[str, object]) -> str:
    for key in ("event_type", "subject_kind", "type", "phase"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _event_cost(event: dict[str, object]) -> Decimal:
    # cost_usd may sit at the top level, or nested under `payload` (the live
    # events.jsonl llm_call shape) or `detail` (synthetic/test shape).
    raw = event.get("cost_usd")
    if raw is None:
        for nested_key in ("payload", "detail"):
            nested = event.get(nested_key)
            if isinstance(nested, dict) and nested.get("cost_usd") is not None:
                raw = nested.get("cost_usd")
                break
    if raw is None:
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def summarize_events(events: list[dict[str, object]]) -> EventMetrics:
    """Fold the event log into counts-by-type, total cost, and a failure count (pure)."""
    by_type: dict[str, int] = {}
    total_cost = Decimal("0")
    failures = 0
    for event in events:
        etype = _event_type(event)
        by_type[etype] = by_type.get(etype, 0) + 1
        total_cost += _event_cost(event)
        if "fail" in etype.lower() or "halt" in etype.lower():
            failures += 1
    return EventMetrics(
        total=len(events), by_type=by_type, total_cost_usd=total_cost, failures=failures
    )


def read_events(events_path: str | Path) -> list[dict[str, object]]:
    """Read a JSONL event log into a list of dicts (skips blank/garbled lines)."""
    path = Path(events_path)
    if not path.is_file():
        return []
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
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


def render_metrics(metrics: EventMetrics) -> str:
    """Render the metrics pane (pure)."""
    lines = [
        f"events: {metrics.total}  |  failures/halts: {metrics.failures}  "
        f"|  cumulative cost: ${metrics.total_cost_usd}",
        "by type:",
    ]
    for etype in sorted(metrics.by_type):
        lines.append(f"  {etype}: {metrics.by_type[etype]}")
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI entrypoint
    import argparse
    import os
    import uuid
    from datetime import datetime, timezone

    parser = argparse.ArgumentParser(prog="supervisor.control_panel")
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("OL_SUPERVISOR_STATE_DIR", "."),
        help="the orchestrator state dir (commands/, logs/events.jsonl).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("metrics", help="summarise logs/events.jsonl")
    sub.add_parser("pause", help="write a pause command")
    sub.add_parser("query", help="write a query_register_state command")
    bump = sub.add_parser("bump", help="write a bump_budget command")
    bump.add_argument("new_cap_usd")
    for p in (parser,):
        p.add_argument("--by", default=os.environ.get("USER", "operator"))

    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir)

    if args.cmd == "metrics":
        metrics = summarize_events(read_events(state_dir / "logs" / "events.jsonl"))
        print(render_metrics(metrics))
        return 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cid = f"{args.cmd}_{uuid.uuid4().hex[:12]}"
    ctype = {"pause": "pause", "query": "query_register_state", "bump": "bump_budget"}[args.cmd]
    cap = getattr(args, "new_cap_usd", None)
    path = write_command(
        state_dir, ctype, command_id=cid, issued_by=args.by, issued_at=now, new_cap_usd=cap
    )
    print(f"wrote {ctype} command: {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main())


__all__ = [
    "COMMAND_TYPES",
    "EventMetrics",
    "write_command",
    "summarize_events",
    "read_events",
    "render_metrics",
]
