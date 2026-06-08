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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from supervisor.full_status_surface import (
    FullFleetSnapshot,
    RefreshScheduler,
    run_status_loop,
)

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


def render_learnings(findings: Sequence[Mapping[str, object]]) -> str:
    """Render the persisted Run-Auditor learnings pane (pure; over read_audit_findings rows).

    Groups by finding kind and lists each subject with its recommendation + adoption route, so the
    operator sees the accumulated cross-run/cross-project learnings (Item 2 DB capture). An empty
    set renders a clear 'no learnings yet' line."""
    if not findings:
        return "learnings: none captured yet (the Learn step records findings to run_audit_findings)."
    lines = [f"learnings: {len(findings)} finding(s) captured (run_audit_findings):"]
    for finding in findings:
        kind = str(finding.get("kind", "?"))
        binding_class = finding.get("binding_class")
        if binding_class:
            kind = f"{kind}:{binding_class}"
        subject = str(finding.get("subject", "?"))
        runs = finding.get("runs_audited", "?")
        lines.append(f"  [{kind}] {subject}  (across {runs} runs)")
        lines.append(f"    → {finding.get('recommendation', '')}")
        lines.append(f"    adopt: {finding.get('routes_to', '')}")
    return "\n".join(lines)


def render_correction_summary(rows: Sequence[Mapping[str, object]]) -> str:
    """Render the per-item correction-churn pane (pure; over read_correction_summary rows)."""
    if not rows:
        return "corrections: none captured yet (the Learn step records correction_attempts)."
    lines = [f"corrections: {len(rows)} item(s) entered the correction loop:"]
    for row in rows:
        lines.append(
            f"  {row.get('item_id', '?')}: {row.get('attempts', 0)} attempt(s) "
            f"across {row.get('projects', 0)} project(s), deepest {row.get('max_level', '?')}"
        )
    return "\n".join(lines)


def render_events(rows: Sequence[Mapping[str, object]]) -> str:
    """Render a fleet event list (pure; over read_events_db rows), most-recent first."""
    if not rows:
        return "events: none in the fleet events table."
    lines = [f"events: {len(rows)} (most recent first):"]
    for row in rows:
        subject = row.get("subject_id")
        suffix = f" {subject}" if subject else ""
        lines.append(
            f"  {row.get('ts_utc', '?')}  {row.get('project_id', '?')}  "
            f"{row.get('role', '?')}/{row.get('event_type', '?')}{suffix}"
        )
    return "\n".join(lines)


def build_dispatch_command(
    finding: Mapping[str, object], *, skills_dir: str
) -> list[str]:
    """Build the ``claude -p`` argv that dispatches a promoted finding to its authoring skill (pure).

    The promoter's adoption step: invoke the skill named in the finding's ``authoring_skill`` with
    the finding's ``recommendation`` as the instruction, resolving the skill from ``skills_dir``
    (the FUP-0743 ``--add-dir <skills> -- /slash`` form). The supervisor never edits the spec itself
    — the named cf-* skill applies the change under its own review. Returns the argv only; the CLI
    `apply` runs it."""
    skill = str(finding.get("authoring_skill") or "").strip()
    recommendation = str(finding.get("recommendation") or "").strip()
    if not skill:
        raise ValueError(
            f"finding {finding.get('finding_key')!r} has no authoring_skill to dispatch to"
        )
    return [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--add-dir",
        skills_dir,
        "--",
        f"/{skill} {recommendation}",
    ]


def run_status_panel(
    fetch_snapshot: Callable[[], FullFleetSnapshot],
    *,
    interval_seconds: float = 30.0,
    once: bool = False,
    emit: Callable[[str], None] = print,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Drive the live status dashboard on the FR-061 bounded refresh cadence.

    Thin wiring over the OLB-16 surface: builds a :class:`RefreshScheduler` and runs
    :func:`run_status_loop`, which rebuilds via ``fetch_snapshot``, renders, and
    ``emit``s each snapshot. ``once`` renders a single pass; otherwise it loops until
    interrupted. ``sleep`` / ``now`` are injected (defaults: ``time.sleep`` + UTC
    wall-clock) so the cadence is testable without real time. Returns an exit code.
    """
    import time as _time

    _sleep = sleep if sleep is not None else _time.sleep
    _now = now if now is not None else (lambda: datetime.now(timezone.utc))
    scheduler = RefreshScheduler(interval=timedelta(seconds=interval_seconds))
    run_status_loop(
        fetch_snapshot,
        scheduler=scheduler,
        emit=emit,
        sleep=_sleep,
        now=_now,
        max_refreshes=1 if once else None,
    )
    return 0


def _main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI entrypoint
    import argparse
    import os
    import uuid

    parser = argparse.ArgumentParser(prog="supervisor.control_panel")
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("OL_SUPERVISOR_STATE_DIR", "."),
        help="the orchestrator state dir (commands/, logs/events.jsonl).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    metrics_p = sub.add_parser("metrics", help="summarise events (a state-dir log, or --fleet from DB)")
    metrics_p.add_argument(
        "--fleet", action="store_true", help="summarise the whole fleet from the DB events table"
    )
    events_p = sub.add_parser("events", help="list persisted fleet events (DB)")
    events_p.add_argument("--project", default=None)
    events_p.add_argument("--type", dest="event_type", default=None)
    events_p.add_argument("--limit", type=int, default=50)
    sub.add_parser("forecast", help="project fleet spend-to-completion (cost forecasting)")
    onramp_p = sub.add_parser("onramp-abs", help="provision the ABS Phase 0->1->2 dependency chain")
    onramp_p.add_argument("--apply", action="store_true", help="write the rows (default: dry-run)")
    sub.add_parser("pause", help="write a pause command")
    sub.add_parser("query", help="write a query_register_state command")
    bump = sub.add_parser("bump", help="write a bump_budget command")
    bump.add_argument("new_cap_usd")
    status_p = sub.add_parser("status", help="render the live fleet dashboard")
    status_p.add_argument("--once", action="store_true", help="render a single snapshot and exit")
    status_p.add_argument(
        "--interval", type=float, default=30.0, help="bounded refresh interval (seconds)"
    )
    sub.add_parser("learnings", help="list the captured Run-Auditor learnings (run_audit_findings)")
    sub.add_parser("corrections", help="list per-item correction-loop churn (correction_attempts)")
    promote_p = sub.add_parser("promote", help="accept a learning (queue it for adoption)")
    promote_p.add_argument("finding_key")
    reject_p = sub.add_parser("reject", help="reject a learning (suppress re-surfacing)")
    reject_p.add_argument("finding_key")
    apply_p = sub.add_parser("apply", help="dispatch an accepted learning to its authoring skill")
    apply_p.add_argument("finding_key")
    for p in (parser,):
        p.add_argument("--by", default=os.environ.get("USER", "operator"))

    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir)

    if args.cmd == "metrics" and getattr(args, "fleet", False):
        dsn = os.environ.get("OL_SUPERVISOR_DB_URL")
        if not dsn:
            print("control_panel metrics --fleet: OL_SUPERVISOR_DB_URL is not set.")
            return 1
        from supervisor.registry import Registry

        registry = Registry.from_env()
        events = [dict(r) for r in registry.read_events_db(limit=100000)]
        print(render_metrics(summarize_events(events)))
        return 0

    if args.cmd == "events":
        dsn = os.environ.get("OL_SUPERVISOR_DB_URL")
        if not dsn:
            print("control_panel events: OL_SUPERVISOR_DB_URL is not set.")
            return 1
        from supervisor.registry import Registry

        registry = Registry.from_env()
        print(
            render_events(
                registry.read_events_db(
                    project_id=args.project, event_type=args.event_type, limit=args.limit
                )
            )
        )
        return 0

    if args.cmd == "metrics":
        events_path = state_dir / "logs" / "events.jsonl"
        if not events_path.is_file():
            # The most common "metrics looks broken" cause: --state-dir defaulted to
            # '.' (no event log here) instead of the orchestrator's state dir. Say so,
            # rather than silently printing all-zero metrics.
            print(f"control_panel metrics: no event log at {events_path}")
            print(
                "  point --state-dir at the orchestrator state dir (the one holding "
                "logs/events.jsonl) or set OL_SUPERVISOR_STATE_DIR."
            )
            return 1
        metrics = summarize_events(read_events(events_path))
        print(render_metrics(metrics))
        return 0

    if args.cmd == "status":
        dsn = os.environ.get("OL_SUPERVISOR_DB_URL")
        if not dsn:
            print("control_panel status: OL_SUPERVISOR_DB_URL is not set — cannot read the live fleet.")
            return 1
        from supervisor.full_status_surface import build_full_fleet_snapshot
        from supervisor.registry import Registry

        registry = Registry.from_env()

        def _fetch() -> FullFleetSnapshot:
            return build_full_fleet_snapshot(registry, now=datetime.now(timezone.utc))

        return run_status_panel(_fetch, interval_seconds=args.interval, once=args.once)

    if args.cmd == "learnings":
        dsn = os.environ.get("OL_SUPERVISOR_DB_URL")
        if not dsn:
            print("control_panel learnings: OL_SUPERVISOR_DB_URL is not set — cannot read learnings.")
            return 1
        from supervisor.registry import Registry

        registry = Registry.from_env()
        print(render_learnings(registry.read_audit_findings()))
        return 0

    if args.cmd == "onramp-abs":
        from supervisor.abs_onramp import abs_chain_plan, render_chain_plan

        plan = abs_chain_plan()
        if not args.apply:
            print(render_chain_plan(plan))
            print("(dry-run — pass --apply to provision)")
            return 0
        dsn = os.environ.get("OL_SUPERVISOR_DB_URL")
        if not dsn:
            print("control_panel onramp-abs --apply: OL_SUPERVISOR_DB_URL is not set.")
            return 1
        from supervisor.registry import Registry

        registry = Registry.from_env()
        created = 0
        for phase in plan:
            if registry.upsert_project(
                phase.project_id,
                folder_path=phase.folder_path,
                priority=phase.priority,
                depends_on=phase.depends_on,
            ):
                created += 1
                print(f"provisioned {phase.project_id} (depends_on={list(phase.depends_on)})")
            else:
                print(f"{phase.project_id} already exists — left unchanged")
        print(f"ABS on-ramp: {created} new project(s) provisioned.")
        return 0

    if args.cmd == "forecast":
        dsn = os.environ.get("OL_SUPERVISOR_DB_URL")
        if not dsn:
            print("control_panel forecast: OL_SUPERVISOR_DB_URL is not set.")
            return 1
        from supervisor.candidate_enrichment import open_work_counts_for
        from supervisor.cost_forecast import forecast_fleet, render_forecast
        from supervisor.registry import Registry

        registry = Registry.from_env()
        projects = list(registry.read_all_projects())
        workspace_root = os.environ.get("OL_SUPERVISOR_WORKSPACE_ROOT")
        open_counts = open_work_counts_for(projects, workspace_root=workspace_root)
        forecast = forecast_fleet(registry.read_learning_records(), open_counts)
        ceiling_raw = os.environ.get("OL_SUPERVISOR_FORECAST_CEILING_USD")
        ceiling = Decimal(ceiling_raw) if ceiling_raw else None
        print(render_forecast(forecast, ceiling_usd=ceiling))
        return 0

    if args.cmd in ("corrections", "promote", "reject", "apply"):
        dsn = os.environ.get("OL_SUPERVISOR_DB_URL")
        if not dsn:
            print(f"control_panel {args.cmd}: OL_SUPERVISOR_DB_URL is not set.")
            return 1
        from supervisor.registry import Registry

        registry = Registry.from_env()

        if args.cmd == "corrections":
            print(render_correction_summary(registry.read_correction_summary()))
            return 0

        if args.cmd == "reject":
            registry.set_finding_status(args.finding_key, "rejected", decided_by=args.by)
            print(f"rejected {args.finding_key} (will not be re-surfaced).")
            return 0

        if args.cmd == "promote":
            registry.set_finding_status(args.finding_key, "accepted", decided_by=args.by)
            print(
                f"accepted {args.finding_key} — queued for adoption. "
                f"Run `control_panel apply {args.finding_key}` to dispatch it to its authoring skill."
            )
            return 0

        # apply: dispatch the accepted finding to its authoring skill (live; spawns claude).
        findings = {str(f.get("finding_key")): f for f in registry.read_audit_findings()}
        finding = findings.get(args.finding_key)
        if finding is None:
            print(f"control_panel apply: no finding {args.finding_key!r}.")
            return 1
        if str(finding.get("status")) != "accepted":
            print(
                f"control_panel apply: {args.finding_key} is '{finding.get('status')}', not "
                f"'accepted' — promote it first."
            )
            return 1
        skills_dir = os.environ.get("CLAUDE_SKILLS_DIR", "")
        argv_cmd = build_dispatch_command(finding, skills_dir=skills_dir)
        print("dispatching to authoring skill:\n  " + " ".join(argv_cmd))
        import subprocess  # noqa: PLC0415 - lazy; only the live apply path needs it

        try:
            completed = subprocess.run(argv_cmd, check=False)  # noqa: S603 - argv is built, not shell
        except FileNotFoundError:
            print("control_panel apply: `claude` not on PATH — cannot dispatch.")
            return 1
        if completed.returncode == 0:
            registry.set_finding_status(args.finding_key, "applied", decided_by=args.by)
            print(f"applied {args.finding_key}.")
            return 0
        print(f"control_panel apply: dispatch exited {completed.returncode}; left 'accepted'.")
        return 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cid = f"{args.cmd}_{uuid.uuid4().hex[:12]}"
    ctype = {"pause": "pause", "query": "query_register_state", "bump": "bump_budget"}[args.cmd]
    cap = getattr(args, "new_cap_usd", None)
    path = write_command(
        state_dir, ctype, command_id=cid, issued_by=args.by, issued_at=now, new_cap_usd=cap
    )
    print(f"wrote {ctype} command: {path}")
    print(
        "  queued for the orchestrator's command_dispatch — it is consumed on the "
        "running orchestrator's next cycle (asynchronous; not a synchronous read). "
        "With no orchestrator running for this state dir, it simply waits."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main())


__all__ = [
    "COMMAND_TYPES",
    "EventMetrics",
    "write_command",
    "run_status_panel",
    "summarize_events",
    "read_events",
    "render_metrics",
    "render_learnings",
    "render_correction_summary",
    "render_events",
    "build_dispatch_command",
]
