"""FastAPI app for the control-panel GUI — a thin adapter over supervisor reads + write seams.

Every endpoint reuses an existing pure core or Registry method; the API adds NO decisions
(design: docs/control_panel_gui_design.md, principle 6). Reads are GET; actions are POST and route
to the SAME write seams the CLI uses (write_command / set_finding_status / gate_response files / the
apply dispatch). The Registry is resolved through an injectable provider so tests pass a fake
(real-seam: the API wiring + the pure cores run for real; only the DB read/write is faked). Every
action is recorded to the operator action log.

Run (local): set OL_SUPERVISOR_DB_URL (+ OL_SUPERVISOR_STATE_DIR), then
``python -m webui.server`` (serves the built UI when OL_SUPERVISOR_WEBUI_STATIC is set).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

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
from webui.server import audit_log, gates

#: argv -> (returncode, stdout). The apply dispatcher; injected in tests (default spawns claude).
Dispatcher = Callable[[list[str]], "tuple[int, str]"]


class RegistryLike(Protocol):
    """The subset of supervisor.registry.Registry the API uses (so tests inject a fake)."""

    def read_candidates(self) -> Sequence[Mapping[str, object]]: ...
    def read_running(self) -> Sequence[Mapping[str, object]]: ...
    def read_all_projects(self) -> Sequence[Mapping[str, object]]: ...
    def read_audit_findings(self) -> Sequence[Mapping[str, object]]: ...
    def read_audit_effects(self) -> Sequence[Mapping[str, object]]: ...
    def read_correction_summary(self) -> Sequence[Mapping[str, object]]: ...
    def read_learning_records(self) -> Sequence[Mapping[str, object]]: ...
    def read_cumulative_spend_usd(self) -> Decimal: ...
    def read_events_db(
        self, *, project_id: str | None = ..., event_type: str | None = ..., limit: int = ...
    ) -> Sequence[Mapping[str, object]]: ...
    def set_finding_status(self, finding_key_value: str, status: str, *, decided_by: str) -> None: ...
    def prune_events(self, *, before_iso: str) -> int: ...
    def upsert_project(
        self, project_id: str, *, folder_path: str, priority: int, depends_on: Sequence[str],
        lifecycle_state: str = ...,
    ) -> bool: ...


def _default_registry_provider() -> RegistryLike:
    from supervisor.registry import Registry  # noqa: PLC0415 - lazy: only the live server needs a DB

    if not os.environ.get("OL_SUPERVISOR_DB_URL"):
        raise HTTPException(status_code=503, detail="OL_SUPERVISOR_DB_URL is not set.")
    return Registry.from_env()


def _default_dispatcher(argv: list[str]) -> tuple[int, str]:
    import subprocess  # noqa: PLC0415 - lazy; only the live apply path needs it

    try:
        p = subprocess.run(argv, check=False, capture_output=True, text=True)  # noqa: S603 - argv built
    except FileNotFoundError:
        return 127, "`claude` not on PATH"
    return p.returncode, (p.stdout or "") + (f"\n{p.stderr}" if p.stderr else "")


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
        "rows": [_jsonable(asdict(r)) for r in snap.rows],
        "counts_by_lifecycle_state": dict(snap.counts_by_lifecycle_state),
        "total_attention_debt": snap.total_attention_debt,
        "total_open_work_count": snap.total_open_work_count,
        "total_cumulative_cost_usd": str(snap.total_cumulative_cost_usd),
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
) -> FastAPI:
    """Build the API. ``registry_provider`` is called per request (inject a fake in tests).

    ``state_dir`` is where command/gate JSONs + the action log are written (default:
    OL_SUPERVISOR_STATE_DIR or '.'). ``allow_apply`` gates the dispatch-to-skill endpoint;
    ``dispatcher`` runs the apply argv (default spawns ``claude``). ``token`` (default:
    OL_SUPERVISOR_WEBUI_TOKEN) — when set, every ``/api/*`` route except ``/api/health`` requires it
    via ``Authorization: Bearer <token>`` or a ``?token=`` query param (the SSE/EventSource path).
    Unset = open (the localhost single-operator assumption)."""
    app = FastAPI(title="Outer Loop Supervisor — Control Panel", version="1.2")
    sdir = Path(state_dir or os.environ.get("OL_SUPERVISOR_STATE_DIR", "."))
    auth_token = token if token is not None else os.environ.get("OL_SUPERVISOR_WEBUI_TOKEN")

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
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _record(action: str, target: str, by: str, detail: str = "") -> None:
        try:
            audit_log.append_action(sdir, action=action, target=target, by=by, now_iso=_now_iso(),
                                    detail=detail)
        except OSError:
            pass

    def _inbox_payload(reg: RegistryLike) -> tuple[dict[str, Any], dict[str, Any]]:
        """(inbox, fleet) dicts from one snapshot — shared by /api/inbox, /api/fleet, /api/stream."""
        snap = build_full_fleet_snapshot(reg, now=datetime.now(timezone.utc))  # type: ignore[arg-type]
        cards = build_inbox(
            fleet_rows=snap.rows,
            findings=reg.read_audit_findings(),
            effects=reg.read_audit_effects(),
            corrections=reg.read_correction_summary(),
            gates=gates.list_pending_gates(sdir),
            budget_breach=_budget_breach(reg),
        )
        return {"cards": [asdict(c) for c in cards], "count": len(cards)}, _snapshot_dict(snap)

    # ---- reads -------------------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "db_configured": bool(os.environ.get("OL_SUPERVISOR_DB_URL"))}

    @app.get("/api/fleet")
    def fleet() -> dict[str, Any]:
        _, fl = _inbox_payload(registry_provider())
        return fl

    @app.get("/api/inbox")
    def inbox() -> dict[str, Any]:
        ib, _ = _inbox_payload(registry_provider())
        return ib

    @app.get("/api/stream")
    def stream(
        interval: float = Query(default=5.0, ge=0.5, le=60.0),
        max_events: int | None = Query(default=None, ge=1),
    ) -> StreamingResponse:
        """Server-Sent Events: push {inbox, fleet} every ``interval`` s (replaces client polling).

        ``max_events`` bounds the stream (tests pass 1); unbounded by default until the client
        disconnects. The sync generator is iterated in Starlette's threadpool, so the sleep + sync
        DB reads don't block the event loop."""
        def gen() -> "Any":
            import time  # noqa: PLC0415

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

    @app.get("/api/gates")
    def list_gates() -> dict[str, Any]:
        return {"gates": gates.list_pending_gates(sdir)}

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
        type: str | None = Query(default=None),  # noqa: A002 - matches the CLI's --type flag
        limit: int = Query(default=50, ge=1, le=100000),
    ) -> dict[str, Any]:
        reg = registry_provider()
        rows = list(reg.read_events_db(project_id=project, event_type=type, limit=limit))
        return {"events": [_jsonable(r) for r in rows], "metrics": asdict(summarize_events(rows))}

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

    @app.post("/api/gates/resolve")
    def resolve_gate(
        request_file: str = Body(embed=True),
        selected_option: str = Body(embed=True),
        reasoning: str = Body(default="", embed=True),
        by: str = Body(default="operator", embed=True),
    ) -> dict[str, object]:
        try:
            out = gates.write_gate_response(
                sdir, request_file, selected_option=selected_option,
                reasoning=reasoning or f"operator decision via control panel ({by})",
            )
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"no pending gate {request_file!r}") from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        _record("gate-resolve", request_file, by, selected_option)
        return {"request_file": request_file, "selected_option": selected_option,
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
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted = reg.prune_events(before_iso=cutoff)
        _record("events-prune", f"{days}d", by, f"deleted {deleted}")
        return {"deleted": deleted, "before": cutoff}

    if static_dir is not None and Path(static_dir).is_dir():
        from fastapi.staticfiles import StaticFiles  # noqa: PLC0415 - only when serving a built UI

        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="ui")

    return app


# Module-level app for `uvicorn webui.server.app:app` (live server; reads the env DB).
app = create_app(static_dir=os.environ.get("OL_SUPERVISOR_WEBUI_STATIC"))
