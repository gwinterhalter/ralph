"""FastAPI app for the control-panel GUI — a thin adapter over supervisor reads + write seams.

Every endpoint reuses an existing pure core or Registry method; the API adds NO decisions
(design: docs/control_panel_gui_design.md, principle 6). Reads are GET; actions are POST and route
to the SAME write seams the CLI uses (write_command / set_finding_status / gate_response files / the
apply dispatch). The Registry is resolved through an injectable provider so tests pass a fake
(real-seam: the API wiring + the pure cores run for real; only the DB read/write is faked). Every
action is recorded to the operator action log.

Run (local): set PROD_DB_URL (+ OL_SUPERVISOR_STATE_DIR), then
``python -m webui.server`` (serves the built UI when OL_SUPERVISOR_WEBUI_STATIC is set).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from supervisor.abs_onramp import abs_chain_plan
from supervisor.candidate_enrichment import open_work_counts_for
from supervisor.control_panel import (
    _dispatch_succeeded,
    build_dispatch_command,
    summarize_effect_outcomes,
    summarize_events,
    summarize_finding_statuses,
    write_command,
)
from supervisor.cost_forecast import forecast_fleet
from supervisor.full_status_surface import FullFleetSnapshot, build_full_fleet_snapshot
from supervisor.inbox import build_inbox
from supervisor.safety_gates import DEFAULT_CONCURRENCY_CEILING
from webui.server import audit_log, gates
from webui.server.supervisor_control import SupervisorRunner

#: argv -> (returncode, stdout). The apply dispatcher; injected in tests (default spawns claude).
Dispatcher = Callable[[list[str]], "tuple[int, str]"]


class RegistryLike(Protocol):
    """The subset of supervisor.registry.Registry the API uses (so tests inject a fake)."""

    def read_candidates(self) -> Sequence[Mapping[str, object]]: ...
    def read_running(self) -> Sequence[Mapping[str, object]]: ...
    def read_all_projects(self) -> Sequence[Mapping[str, object]]: ...
    def read_latest_run_per_project(self) -> Mapping[str, Mapping[str, object]]: ...
    def read_audit_findings(self) -> Sequence[Mapping[str, object]]: ...
    def read_audit_effects(self) -> Sequence[Mapping[str, object]]: ...
    def read_correction_summary(self) -> Sequence[Mapping[str, object]]: ...
    def read_learning_records(self) -> Sequence[Mapping[str, object]]: ...
    def read_completed_runs(self) -> Sequence[Mapping[str, object]]: ...
    def read_active_runs(self) -> Sequence[Mapping[str, object]]: ...
    def read_cumulative_spend_usd(self) -> Decimal: ...
    def read_events_db(
        self, *, project_id: str | None = ..., event_type: str | None = ..., limit: int = ...
    ) -> Sequence[Mapping[str, object]]: ...
    def set_finding_status(self, finding_key_value: str, status: str, *, decided_by: str) -> None: ...
    def set_lifecycle_state(self, project_id: str, state: str) -> None: ...
    def delete_pending_project(self, project_id: str) -> bool: ...
    def prune_events(self, *, before_iso: str) -> int: ...
    def upsert_project(
        self, project_id: str, *, folder_path: str, priority: int, depends_on: Sequence[str],
        lifecycle_state: str = ...,
    ) -> bool: ...


# FUP-1351: the provider used to build a fresh Registry -- and open a NEW psycopg connection --
# on EVERY request, never closing it, exhausting the Supabase connection slots within a session.
# Fix: cache ONE Registry for the server lifetime and reuse its connection. Because FastAPI runs
# sync endpoints (and the SSE stream) in a threadpool, a single psycopg connection would be used
# concurrently -- unsafe -- so hand out a lock-serialized proxy. The connection is rebuilt only if
# it has dropped (conn.closed). Single-operator localhost GUI: serialization cost is negligible.
import threading

_registry_singleton: RegistryLike | None = None
_registry_build_lock = threading.Lock()
_registry_call_lock = threading.Lock()


class _LockedRegistry:
    """Serializes every DB call on the shared connection through one lock (psycopg3
    Connections are not safe for concurrent use across threads)."""

    def __init__(self, base: object, lock: threading.Lock) -> None:
        self._base = base
        self._lock = lock

    def __getattr__(self, name: str) -> object:
        attr = getattr(self._base, name)
        if not callable(attr):
            return attr

        def _wrapped(*args: object, **kwargs: object) -> object:
            with self._lock:
                return attr(*args, **kwargs)

        return _wrapped


def _default_registry_provider() -> RegistryLike:
    from supervisor.registry import (
        Registry,
    )

    global _registry_singleton
    if not os.environ.get("PROD_DB_URL"):
        raise HTTPException(status_code=503, detail="PROD_DB_URL is not set.")
    with _registry_build_lock:
        reg = _registry_singleton
        if reg is not None:
            raw = getattr(reg._base, "_conn", None)  # type: ignore[attr-defined]
            if raw is not None and getattr(raw, "closed", False):
                reg = None  # connection dropped -> rebuild
        if reg is None:
            reg = cast("RegistryLike", _LockedRegistry(Registry.from_env(), _registry_call_lock))
            _registry_singleton = reg
    return reg


def _default_dispatcher(argv: list[str]) -> tuple[int, str]:
    import subprocess

    try:
        p = subprocess.run(argv, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return 127, "`claude` not on PATH"
    return p.returncode, (p.stdout or "") + (f"\n{p.stderr}" if p.stderr else "")


def _resolve_ceiling() -> int:
    """The operator-tunable Concurrency Ceiling for the DISPLAY surfaces (concurrency 2026-06-09).

    Reads ``OL_SUPERVISOR_CONCURRENCY_CEILING`` (the same env the supervisor dispatcher reads),
    so the GUI's ``Running: N/ceiling`` matches what the dispatcher actually fills to. Falls back
    to ``DEFAULT_CONCURRENCY_CEILING`` when unset/invalid — never raises."""
    raw = os.environ.get("OL_SUPERVISOR_CONCURRENCY_CEILING")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_CONCURRENCY_CEILING
        if value >= 1:
            return value
    return DEFAULT_CONCURRENCY_CEILING


def _money2(value: object) -> str:
    """Format a cost to a 2-decimal USD string (e.g. '157.30') — cents grain for GUI display."""
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (ArithmeticError, ValueError, TypeError):
        return "0.00"


def _jsonable(value: object) -> Any:
    """Recursively coerce Decimals/datetimes/dataclasses into JSON-safe values."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _snapshot_dict(snap: FullFleetSnapshot) -> dict[str, Any]:
    return {
        "as_of": snap.as_of.isoformat(),
        "rows": [_jsonable({**asdict(r), "cumulative_cost_usd": _money2(r.cumulative_cost_usd)}) for r in snap.rows],
        "counts_by_lifecycle_state": dict(snap.counts_by_lifecycle_state),
        "total_attention_debt": snap.total_attention_debt,
        "total_open_work_count": snap.total_open_work_count,
        "total_cumulative_cost_usd": _money2(snap.total_cumulative_cost_usd),
        "running_count": snap.running_count,
        "stalled_count": snap.stalled_count,
        "concurrency_ceiling": snap.concurrency_ceiling,
        "headroom": snap.headroom,
    }


def _budget_breach(reg: RegistryLike) -> dict[str, object] | None:
    """A fleet budget-breach signal when cumulative spend >= OL_SUPERVISOR_BUDGET_CEILING_USD."""
    raw = os.environ.get("OL_SUPERVISOR_BUDGET_CEILING_USD") or os.environ.get(
        "OL_SUPERVISOR_FORECAST_CEILING_USD"
    )
    if not raw:
        return None
    try:
        ceiling = Decimal(raw)
        spend = reg.read_cumulative_spend_usd()
    except Exception:  # noqa: BLE001 - best-effort signal; never break the inbox
        return None
    if spend >= ceiling:
        return {"project_id": "*fleet*", "detail": f"cumulative ${spend} ≥ ceiling ${ceiling}"}
    return None


def create_app(
    registry_provider: Callable[[], RegistryLike] = _default_registry_provider,
    *,
    state_dir: str | Path | None = None,
    static_dir: str | Path | None = None,
    allow_apply: bool = True,
    dispatcher: Dispatcher = _default_dispatcher,
    token: str | None = None,
    runner: SupervisorRunner | Any | None = None,
    allow_supervisor_control: bool = True,
) -> FastAPI:
    """Build the API. ``registry_provider`` is called per request (inject a fake in tests).

    ``state_dir`` is where command/gate JSONs + the action log are written (default:
    OL_SUPERVISOR_STATE_DIR or '.'). ``allow_apply`` gates the dispatch-to-skill endpoint;
    ``dispatcher`` runs the apply argv (default spawns ``claude``). ``token`` (default:
    OL_SUPERVISOR_WEBUI_TOKEN) — when set, every ``/api/*`` route except ``/api/health`` requires it
    via ``Authorization: Bearer <token>`` or a ``?token=`` query param (the SSE/EventSource path).
    Unset = open (the localhost single-operator assumption)."""
    app = FastAPI(title="Outer Loop Supervisor — Control Panel", version="1.3")
    sdir = Path(state_dir or os.environ.get("OL_SUPERVISOR_STATE_DIR", "."))
    auth_token = token if token is not None else os.environ.get("OL_SUPERVISOR_WEBUI_TOKEN")
    repo_root = Path(__file__).resolve().parent.parent.parent  # ...\Ralph-dev
    sup_runner = runner if runner is not None else SupervisorRunner(repo_root, sdir / "supervisor_loop.pid")

    def _gate_dirs(reg: RegistryLike) -> list[Path]:
        """The supervisor state dir + each project's own state dir (real fleets keep per-project
        gate files at <workspace_root>/<folder_path>/state/)."""
        dirs = [sdir]
        root = os.environ.get("OL_SUPERVISOR_WORKSPACE_ROOT")
        if root:
            for p in reg.read_all_projects():
                folder = p.get("folder_path")
                if isinstance(folder, str) and folder:
                    fp = Path(folder)
                    dirs.append((fp if fp.is_absolute() else Path(root) / fp) / "state")
        return dirs

    @app.middleware("http")
    async def _auth(request: Request, call_next: Callable[[Request], Any]) -> Any:
        path = request.url.path
        if auth_token and path.startswith("/api/") and path != "/api/health":
            header = request.headers.get("authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else request.query_params.get("token")
            if supplied != auth_token:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    def _now_iso() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _record(action: str, target: str, by: str, detail: str = "") -> None:
        try:
            audit_log.append_action(sdir, action=action, target=target, by=by, now_iso=_now_iso(),
                                    detail=detail)
        except OSError:
            pass

    def _inbox_payload(reg: RegistryLike) -> tuple[dict[str, Any], dict[str, Any]]:
        """(inbox, fleet) dicts from one snapshot — shared by /api/inbox, /api/fleet, /api/stream."""
        snap = build_full_fleet_snapshot(
            reg, now=datetime.now(UTC), concurrency_ceiling=_resolve_ceiling(),  # type: ignore[arg-type]
            cumulative_costs=_cost_by_project(reg),  # completed terminal + live in-flight spend
        )
        cards = build_inbox(
            fleet_rows=snap.rows,
            findings=reg.read_audit_findings(),
            effects=reg.read_audit_effects(),
            corrections=reg.read_correction_summary(),
            gates=gates.list_pending_gates(_gate_dirs(reg)),
            projects=reg.read_all_projects(),  # surfaces paused_gate/failed projects absent from the snapshot
            budget_breach=_budget_breach(reg),
        )
        return {"cards": [asdict(c) for c in cards], "count": len(cards)}, _snapshot_dict(snap)

    def _live_spend_by_project(reg: RegistryLike) -> dict[str, Decimal]:
        """In-flight spend for currently-running runs, read FRESH from each run's
        ``state/spend.json`` (``total_spend_usd``). A running run has no
        ``terminal_cost_usd`` yet (that's set at completion-reconcile), so without this
        a still-running / reaped-before-completion project shows $0 cost in the GUI even
        while it spends — the "old projects show cost, new ones don't" gap. Best-effort:
        a missing/garbled spend.json contributes nothing."""
        totals: dict[str, Decimal] = {}
        try:
            active = reg.read_active_runs()
        except Exception:  # noqa: BLE001  (read_active_runs is additive; tolerate absence)
            return totals
        for run in active:
            pid = str(run.get("project_id") or run.get("project_slug") or "")
            seed = run.get("seed_path")
            if not pid or not isinstance(seed, str) or not seed:
                continue
            try:
                data = json.loads((Path(seed).parent / "state" / "spend.json").read_text(encoding="utf-8"))
                raw = data.get("total_spend_usd")
                cost = Decimal(str(raw)) if raw is not None else Decimal(0)
            except (OSError, ValueError, json.JSONDecodeError, ArithmeticError):
                continue
            totals[pid] = totals.get(pid, Decimal(0)) + cost
        return totals

    def _cost_by_project(reg: RegistryLike) -> dict[str, Decimal]:
        """Per-project cost = completed runs' terminal cost PLUS live in-flight spend of
        any currently-running run (so a project that is still running — or was reaped
        before a clean completion-reconcile — surfaces its real spend, not $0)."""
        totals: dict[str, Decimal] = {}
        for run in reg.read_completed_runs():
            pid = str(run.get("project_id") or "")
            raw = run.get("terminal_cost_usd")
            try:
                cost = Decimal(str(raw)) if raw is not None else Decimal(0)
            except Exception:  # noqa: BLE001
                cost = Decimal(0)
            totals[pid] = totals.get(pid, Decimal(0)) + cost
        for pid, live in _live_spend_by_project(reg).items():
            totals[pid] = totals.get(pid, Decimal(0)) + live
        return totals

    # ---- reads -------------------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "db_configured": bool(os.environ.get("PROD_DB_URL"))}

    @app.get("/api/fleet")
    def fleet() -> dict[str, Any]:
        _, fl = _inbox_payload(registry_provider())
        return fl

    @app.get("/api/inbox")
    def inbox() -> dict[str, Any]:
        ib, _ = _inbox_payload(registry_provider())
        return ib

    @app.get("/api/gates/answered")
    def gates_answered() -> dict[str, Any]:
        """Resolved gates (question + the answer: selected_option/custom_text + reasoning +
        confidence + how it was answered) — the audit surface for auto-answers, which drop off
        the pending /api/gates list once resolved."""
        reg = registry_provider()
        items = gates.list_answered_gates(_gate_dirs(reg))
        return {"gates": items, "count": len(items)}

    @app.get("/api/stream")
    def stream(
        interval: float = Query(default=5.0, ge=0.5, le=60.0),
        max_events: int | None = Query(default=None, ge=1),
    ) -> StreamingResponse:
        """Server-Sent Events: push {inbox, fleet} every ``interval`` s (replaces client polling).

        ``max_events`` bounds the stream (tests pass 1); unbounded by default until the client
        disconnects. The sync generator is iterated in Starlette's threadpool, so the sleep + sync
        DB reads don't block the event loop."""
        def gen() -> Any:
            import time

            count = 0
            while True:
                ib, fl = _inbox_payload(registry_provider())
                yield f"data: {json.dumps({'inbox': ib, 'fleet': fl})}\n\n"
                count += 1
                if max_events is not None and count >= max_events:
                    return
                time.sleep(interval)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/graph")
    def graph() -> dict[str, Any]:
        """Project dependency graph (Item 1 ``depends_on`` edges) for the graph view."""
        reg = registry_provider()
        nodes: list[dict[str, object]] = []
        edges: list[dict[str, str]] = []
        for p in reg.read_all_projects():
            pid = str(p.get("project_id"))
            nodes.append({"id": pid, "lifecycle_state": str(p.get("lifecycle_state") or "")})
            deps = p.get("depends_on")
            if isinstance(deps, (list, tuple)):
                edges.extend({"from": pid, "to": str(d)} for d in deps)
        return {"nodes": nodes, "edges": edges}

    @app.get("/api/projects")
    def projects(include_archived: bool = Query(default=False)) -> dict[str, Any]:
        """ALL projects (every lifecycle state) + per-project cumulative cost + run count — the full
        fleet picture the active-only FR-058 snapshot omits (completed/failed/paused projects).

        Retired projects (``status='archived'``) are hidden from this fleet view by default so a
        retired shard does not clutter the active picture; pass ``?include_archived=1`` to include
        them (each row carries its ``status`` so the client can badge/section archived ones)."""
        reg = registry_provider()
        cost = _cost_by_project(reg)
        runs_by_project: dict[str, int] = {}
        for run in reg.read_completed_runs():
            pid = str(run.get("project_id") or "")
            runs_by_project[pid] = runs_by_project.get(pid, 0) + 1
        # FUP-0873/0876: per-project latest run -> issue reason + spawned/terminated for duration.
        try:
            latest_run = dict(reg.read_latest_run_per_project())
        except Exception:  # noqa: BLE001 - best-effort enrichment; never break the fleet view
            latest_run = {}
        out: list[dict[str, Any]] = []
        for p in reg.read_all_projects():
            status = str(p.get("status") or "")
            if status == "archived" and not include_archived:
                continue  # retired shard — hidden from the active fleet unless explicitly requested
            pid = str(p.get("project_id"))
            deps = p.get("depends_on")
            lifecycle = str(p.get("lifecycle_state") or "")
            lr = latest_run.get(pid) or {}
            # FUP-0873: a blocked/failed shard's REASON (failure_detail; or just the run status for
            # a paused_gate "waiting" row) so the GUI shows WHY, not a bare badge. FUP-0874: the
            # waiting flag is derivable client-side from lifecycle_state == 'paused_gate'.
            issue = lr.get("failure_detail")
            out.append({
                "project_id": pid,
                "display_name": str(p.get("display_name") or pid),
                "lifecycle_state": lifecycle,
                "status": status,
                "attention_debt": p.get("attention_debt") or 0,
                "depends_on": list(deps) if isinstance(deps, (list, tuple)) else [],
                "cost_usd": _money2(cost.get(pid, Decimal(0))),
                "runs": runs_by_project.get(pid, 0),
                "issue": str(issue) if issue else None,
                "run_status": lr.get("run_status"),
                "spawned_at": _jsonable(lr.get("spawned_at")),
                "terminated_at": _jsonable(lr.get("terminated_at")),
            })
        out.sort(key=lambda r: r["project_id"])
        return {"projects": out, "count": len(out)}

    @app.get("/api/runs")
    def runs() -> dict[str, Any]:
        """Past terminal runs (complete/failed) with cost + duration boundaries — closed activity."""
        reg = registry_provider()
        out = [
            {
                "run_id": str(r.get("run_id") or ""),
                "project_id": str(r.get("project_id") or ""),
                "status": str(r.get("status") or ""),
                "cost_usd": _money2(r.get("terminal_cost_usd") or "0"),
                "spawned_at": _jsonable(r.get("spawned_at")),
                "terminated_at": _jsonable(r.get("terminated_at")),
            }
            for r in reg.read_completed_runs()
        ]
        out.sort(key=lambda r: str(r["terminated_at"] or ""), reverse=True)
        total = sum((Decimal(str(r["cost_usd"] or "0")) for r in out), Decimal(0))
        return {"runs": out, "count": len(out), "total_cost_usd": _money2(total)}

    @app.get("/api/loop-status")
    def loop_status() -> dict[str, Any]:
        """Heuristic 'is the supervisor loop active?' from the most-recent fleet activity (honest:
        it reports last-activity, not a definitive process probe). The GUI is a window on the DB —
        it does not run the loop."""
        reg = registry_provider()
        stamps: list[datetime] = []
        try:
            recent = list(reg.read_events_db(limit=1))
            for e in recent:
                ts = e.get("ts_utc")
                if isinstance(ts, datetime):
                    stamps.append(ts)
        except Exception:
            logging.getLogger(__name__).debug("events freshness probe failed", exc_info=True)
        for run in reg.read_completed_runs():
            raw = run.get("terminated_at")
            if isinstance(raw, str) and raw:
                try:
                    stamps.append(datetime.fromisoformat(raw))
                except ValueError:
                    pass
        managed = bool(sup_runner.loop_running())
        if not stamps:
            return {"last_activity": None, "seconds_since": None, "active_guess": managed,
                    "managed_running": managed}
        last = max(stamps)
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        secs = (datetime.now(UTC) - last).total_seconds()
        return {"last_activity": last.isoformat(), "seconds_since": int(secs),
                "active_guess": managed or secs < 600, "managed_running": managed}

    @app.get("/api/gates")
    def list_gates() -> dict[str, Any]:
        return {"gates": gates.list_pending_gates(_gate_dirs(registry_provider()))}

    @app.get("/api/learnings")
    def learnings(status: str | None = Query(default=None)) -> dict[str, Any]:
        reg = registry_provider()
        rows = list(reg.read_audit_findings())
        if status is not None:
            rows = [r for r in rows if str(r.get("status") or "proposed") == status]
        return {"findings": [_jsonable(r) for r in rows], "by_status": summarize_finding_statuses(rows)}

    @app.get("/api/effects")
    def effects() -> dict[str, Any]:
        reg = registry_provider()
        rows = list(reg.read_audit_effects())
        return {"effects": [_jsonable(r) for r in rows], "by_outcome": summarize_effect_outcomes(rows)}

    @app.get("/api/corrections")
    def corrections() -> dict[str, Any]:
        reg = registry_provider()
        return {"items": [_jsonable(r) for r in reg.read_correction_summary()]}

    @app.get("/api/events")
    def events(
        project: str | None = Query(default=None),
        type: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100000),
    ) -> dict[str, Any]:
        reg = registry_provider()
        rows = list(reg.read_events_db(project_id=project, event_type=type, limit=limit))
        return {"events": [_jsonable(r) for r in rows], "metrics": asdict(summarize_events(rows))}

    @app.get("/api/throttling")
    def throttling(limit: int = Query(default=100, ge=1, le=100000)) -> dict[str, Any]:
        """Rate-limit / usage-limit events the orchestrator captured from ``claude`` stderr
        (concurrency 2026-06-09). Surfaces the count + most-recent occurrences (with reset hints)
        for the Spend / Events tab — the GUI throttling indicator. Empty when nothing was throttled."""
        reg = registry_provider()
        rows = list(reg.read_events_db(event_type="rate_limit", limit=limit))
        recent: list[dict[str, Any]] = []
        for r in rows:
            payload = r.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            recent.append(
                {
                    "ts_utc": _jsonable(r.get("ts_utc")),
                    "project_id": r.get("project_id"),
                    "role": r.get("role"),
                    "reset_hint": payload.get("reset_hint"),
                    "detail": payload.get("detail"),
                }
            )
        return {"count": len(rows), "recent": recent}

    @app.get("/api/forecast")
    def forecast() -> dict[str, Any]:
        reg = registry_provider()
        projects = list(reg.read_all_projects())
        try:
            open_counts = open_work_counts_for(
                projects, workspace_root=os.environ.get("OL_SUPERVISOR_WORKSPACE_ROOT")  # type: ignore[arg-type]
            )
        except Exception:  # noqa: BLE001 - filesystem read is best-effort
            open_counts = {}
        return _jsonable(asdict(forecast_fleet(reg.read_learning_records(), open_counts)))

    @app.get("/api/commands")
    def commands() -> dict[str, Any]:
        """Pending operator commands still in the queue (the orchestrator removes each on consume)."""
        cdir = sdir / "commands"
        items: list[dict[str, object]] = []
        if cdir.is_dir():
            for f in sorted(cdir.glob("*.json")):
                try:
                    obj = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if isinstance(obj, dict):
                    items.append(obj)
        return {"pending": items, "count": len(items)}

    @app.get("/api/actions")
    def actions(limit: int = Query(default=100, ge=1, le=10000)) -> dict[str, Any]:
        return {"actions": audit_log.read_actions(sdir, limit=limit)}

    # ---- actions -----------------------------------------------------------------------------

    @app.post("/api/projects/{project_id}/pause")
    def pause(project_id: str, by: str = Body(default="operator", embed=True)) -> dict[str, object]:
        cid = f"pause_{uuid.uuid4().hex[:12]}"
        write_command(sdir, "pause", command_id=cid, issued_by=by, issued_at=_now_iso())
        _record("pause", project_id, by, cid)
        return {"command_id": cid, "state": "queued", "project_id": project_id}

    @app.post("/api/projects/{project_id}/budget")
    def budget(
        project_id: str,
        new_cap_usd: str = Body(embed=True),
        by: str = Body(default="operator", embed=True),
    ) -> dict[str, object]:
        cid = f"bump_{uuid.uuid4().hex[:12]}"
        write_command(sdir, "bump_budget", command_id=cid, issued_by=by, issued_at=_now_iso(),
                     new_cap_usd=new_cap_usd)
        _record("bump_budget", project_id, by, f"{new_cap_usd} ({cid})")
        return {"command_id": cid, "state": "queued", "project_id": project_id}

    @app.post("/api/projects/{project_id}/approve")
    def approve_project(project_id: str, by: str = Body(default="operator", embed=True)) -> dict[str, object]:
        """Approve a proposed RL project (RL Project Intake): pending_approval → candidate, so the
        supervisor can admit it. Errors if the project is not in pending_approval."""
        try:
            registry_provider().set_lifecycle_state(project_id, "candidate")
        except Exception as e:  # noqa: BLE001 - IllegalTransitionError / unknown project → 409
            raise HTTPException(status_code=409, detail=f"cannot approve {project_id!r}: {e}") from None
        _record("approve-project", project_id, by)
        return {"project_id": project_id, "lifecycle_state": "candidate", "state": "approved"}

    @app.post("/api/projects/{project_id}/reject")
    def reject_project(project_id: str, by: str = Body(default="operator", embed=True)) -> dict[str, object]:
        """Reject a proposed RL project: delete the row (guarded to pending_approval only). The
        scaffolded folder + proposal doc are retained on disk."""
        if not registry_provider().delete_pending_project(project_id):
            raise HTTPException(status_code=404, detail=f"no pending-approval proposal {project_id!r}")
        _record("reject-project", project_id, by)
        return {"project_id": project_id, "state": "rejected"}

    @app.post("/api/gates/resolve")
    def resolve_gate(
        request_path: str = Body(embed=True),
        selected_option: str = Body(embed=True),
        reasoning: str = Body(default="", embed=True),
        by: str = Body(default="operator", embed=True),
    ) -> dict[str, object]:
        try:
            out = gates.write_gate_response(
                request_path, selected_option=selected_option,
                reasoning=reasoning or f"operator decision via control panel ({by})",
            )
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"no pending gate {request_path!r}") from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        _record("gate-resolve", Path(request_path).name, by, selected_option)
        return {"request_path": request_path, "selected_option": selected_option,
                "response_file": out.name, "state": "written"}

    @app.post("/api/findings/{finding_key}/promote")
    def promote(finding_key: str, by: str = Body(default="operator", embed=True)) -> dict[str, object]:
        registry_provider().set_finding_status(finding_key, "accepted", decided_by=by)
        _record("promote", finding_key, by)
        return {"finding_key": finding_key, "status": "accepted"}

    @app.post("/api/findings/{finding_key}/reject")
    def reject(finding_key: str, by: str = Body(default="operator", embed=True)) -> dict[str, object]:
        registry_provider().set_finding_status(finding_key, "rejected", decided_by=by)
        _record("reject", finding_key, by)
        return {"finding_key": finding_key, "status": "rejected"}

    @app.post("/api/findings/{finding_key}/apply")
    def apply(finding_key: str, by: str = Body(default="operator", embed=True)) -> JSONResponse:
        if not allow_apply:
            raise HTTPException(status_code=403, detail="apply is disabled on this server.")
        reg = registry_provider()
        finding = {str(f.get("finding_key")): f for f in reg.read_audit_findings()}.get(finding_key)
        if finding is None:
            raise HTTPException(status_code=404, detail=f"no finding {finding_key!r}")
        if str(finding.get("status")) != "accepted":
            raise HTTPException(
                status_code=409,
                detail=f"{finding_key} is '{finding.get('status')}', not 'accepted' — promote first.",
            )
        argv = build_dispatch_command(finding, skills_dir=os.environ.get("CLAUDE_SKILLS_DIR", ""))
        returncode, output = dispatcher(argv)
        if _dispatch_succeeded(returncode, output):
            reg.set_finding_status(finding_key, "applied", decided_by=by)
            _record("apply", finding_key, by, "dispatched")
            return JSONResponse({"finding_key": finding_key, "status": "applied", "dispatched": argv})
        _record("apply-failed", finding_key, by, output[-200:])
        # Leave the finding 'accepted' — it was NOT applied (NFR: no false success).
        raise HTTPException(
            status_code=502,
            detail=f"dispatch did not run the skill (exit {returncode}); left 'accepted'. "
                   f"Ensure CLAUDE_SKILLS_DIR resolves /{finding.get('authoring_skill')}. "
                   f"Output tail: {output[-300:]}",
        )

    @app.post("/api/findings/{finding_key}/revert")
    def revert(finding_key: str, by: str = Body(default="operator", embed=True)) -> dict[str, object]:
        """Operator rollback REQUEST for an applied learning (surface-only, D1): records the intent to
        the action log + names the authoring skill to reverse it. Does NOT auto-undo — the change was
        applied by a cf-* skill under review, so reversal is an operator-driven re-dispatch, not a
        silent revert. The finding stays 'applied' (effect-measurement keys on it); the request is the
        signal."""
        reg = registry_provider()
        finding = {str(f.get("finding_key")): f for f in reg.read_audit_findings()}.get(finding_key)
        if finding is None:
            raise HTTPException(status_code=404, detail=f"no finding {finding_key!r}")
        skill = str(finding.get("authoring_skill") or "?")
        _record("revert-requested", finding_key, by, f"route to {skill}")
        return {"finding_key": finding_key, "state": "revert-requested",
                "detail": f"recorded — reverse via {skill} (operator/CC), not auto-undone."}

    @app.post("/api/onramp-abs")
    def onramp_abs(
        apply_plan: bool = Query(default=False, alias="apply"),
        by: str = Query(default="operator"),
    ) -> dict[str, Any]:
        plan = abs_chain_plan()
        plan_out = [_jsonable(asdict(p)) for p in plan]
        if not apply_plan:
            return {"plan": plan_out, "applied": False}
        reg = registry_provider()
        created = [
            p.project_id for p in plan
            if reg.upsert_project(p.project_id, folder_path=p.folder_path, priority=p.priority,
                                 depends_on=p.depends_on)
        ]
        _record("onramp-abs", ",".join(created) or "none", by, "applied")
        return {"plan": plan_out, "applied": True, "created": created}

    @app.post("/api/events-prune")
    def events_prune(
        days: float = Query(gt=0), by: str = Query(default="operator")
    ) -> dict[str, object]:
        reg = registry_provider()
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        deleted = reg.prune_events(before_iso=cutoff)
        _record("events-prune", f"{days}d", by, f"deleted {deleted}")
        return {"deleted": deleted, "before": cutoff}

    @app.post("/api/commands/query")
    def query_register(by: str = Body(default="operator", embed=True)) -> dict[str, object]:
        """Queue a query_register_state command (CLI `query` parity)."""
        cid = f"query_{uuid.uuid4().hex[:12]}"
        write_command(sdir, "query_register_state", command_id=cid, issued_by=by, issued_at=_now_iso())
        _record("query", "register", by, cid)
        return {"command_id": cid, "state": "queued"}

    # ---- supervisor loop control (SPAWNS REAL WORK — gated + confirmed in the UI) -------------

    def _require_control() -> None:
        if not allow_supervisor_control:
            raise HTTPException(status_code=403, detail="supervisor control is disabled on this server.")

    @app.post("/api/supervisor/run-once")
    def supervisor_run_once(by: str = Body(default="operator", embed=True)) -> dict[str, object]:
        _require_control()
        pid = sup_runner.run_once()
        _record("supervisor-run-once", "fleet", by, f"pid {pid}")
        return {"started": True, "pid": pid, "mode": "once"}

    @app.post("/api/supervisor/start")
    def supervisor_start(
        interval: float = Query(default=30.0, ge=1.0, le=3600.0),
        by: str = Body(default="operator", embed=True),
    ) -> dict[str, object]:
        _require_control()
        try:
            pid = sup_runner.start_loop(interval)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        _record("supervisor-start", "fleet", by, f"pid {pid} interval {interval}s")
        return {"started": True, "pid": pid, "mode": "loop", "interval": interval}

    @app.post("/api/supervisor/stop")
    def supervisor_stop(by: str = Body(default="operator", embed=True)) -> dict[str, object]:
        _require_control()
        stopped = sup_runner.stop_loop()
        _record("supervisor-stop", "fleet", by, "stopped" if stopped else "none-tracked")
        return {"stopped": stopped}

    if static_dir is not None and Path(static_dir).is_dir():
        from fastapi.staticfiles import (
            StaticFiles,
        )

        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="ui")
    else:
        @app.get("/")
        def _root_help() -> JSONResponse:
            # API-only mode (no built UI found): a 404 at / is confusing, so explain how to get the UI.
            return JSONResponse({
                "service": "Outer Loop Supervisor — control-panel API",
                "api": "/api/* (try /api/health)",
                "ui": "not served — build it: `cd webui/app && npm run build` then restart this "
                      "server, OR run the dev server `cd webui/app && npm run dev` "
                      "(http://localhost:5173).",
            })

    return app


def _default_static_dir() -> str | None:
    """Resolve the built UI: OL_SUPERVISOR_WEBUI_STATIC if set, else webui/app/dist if it exists."""
    env = os.environ.get("OL_SUPERVISOR_WEBUI_STATIC")
    if env:
        return env
    dist = Path(__file__).resolve().parent.parent / "app" / "dist"  # webui/app/dist
    return str(dist) if dist.is_dir() else None


# Module-level app for `uvicorn webui.server.app:app` (live server; reads the env DB).
app = create_app(static_dir=_default_static_dir())
