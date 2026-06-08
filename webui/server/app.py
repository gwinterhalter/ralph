"""FastAPI app for the control-panel GUI — a thin adapter over supervisor reads + write seams.

Every endpoint reuses an existing pure core or Registry method; the API adds NO decisions
(design: docs/control_panel_gui_design.md, principle 6). Reads are GET; actions are POST and route
to the SAME write seams the CLI uses (write_command / set_finding_status / the apply dispatch). The
Registry is resolved through an injectable provider so tests pass a fake (real-seam: the API wiring
+ the pure cores run for real; only the DB read/write is faked).

Run (local): set OL_SUPERVISOR_DB_URL (+ OL_SUPERVISOR_STATE_DIR), then
``uvicorn webui.server.app:app --port 8787`` (or ``python -m webui.server`` once added).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from supervisor.control_panel import (
    build_dispatch_command,
    summarize_effect_outcomes,
    summarize_events,
    summarize_finding_statuses,
    write_command,
)
from supervisor.full_status_surface import FullFleetSnapshot, build_full_fleet_snapshot
from supervisor.inbox import build_inbox


class RegistryLike(Protocol):
    """The subset of supervisor.registry.Registry the API uses (so tests inject a fake)."""

    def read_candidates(self) -> Sequence[Mapping[str, object]]: ...
    def read_running(self) -> Sequence[Mapping[str, object]]: ...
    def read_audit_findings(self) -> Sequence[Mapping[str, object]]: ...
    def read_audit_effects(self) -> Sequence[Mapping[str, object]]: ...
    def read_correction_summary(self) -> Sequence[Mapping[str, object]]: ...
    def read_events_db(
        self, *, project_id: str | None = ..., event_type: str | None = ..., limit: int = ...
    ) -> Sequence[Mapping[str, object]]: ...
    def set_finding_status(self, finding_key_value: str, status: str, *, decided_by: str) -> None: ...


def _default_registry_provider() -> RegistryLike:
    from supervisor.registry import Registry  # noqa: PLC0415 - lazy: only the live server needs a DB

    if not os.environ.get("OL_SUPERVISOR_DB_URL"):
        raise HTTPException(status_code=503, detail="OL_SUPERVISOR_DB_URL is not set.")
    return Registry.from_env()


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


def create_app(
    registry_provider: Callable[[], RegistryLike] = _default_registry_provider,
    *,
    state_dir: str | Path | None = None,
    static_dir: str | Path | None = None,
    allow_apply: bool = True,
) -> FastAPI:
    """Build the API. ``registry_provider`` is called per request (inject a fake in tests).

    ``state_dir`` is where operator command JSONs are written (default: OL_SUPERVISOR_STATE_DIR or
    '.'). ``allow_apply`` gates the dispatch-to-skill endpoint (it spawns ``claude``)."""
    app = FastAPI(title="Outer Loop Supervisor — Control Panel", version="1.0")
    sdir = Path(state_dir or os.environ.get("OL_SUPERVISOR_STATE_DIR", "."))

    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "db_configured": bool(os.environ.get("OL_SUPERVISOR_DB_URL"))}

    @app.get("/api/fleet")
    def fleet() -> dict[str, Any]:
        reg = registry_provider()
        snap = build_full_fleet_snapshot(reg, now=datetime.now(timezone.utc))  # type: ignore[arg-type]
        return _snapshot_dict(snap)

    @app.get("/api/inbox")
    def inbox() -> dict[str, Any]:
        reg = registry_provider()
        snap = build_full_fleet_snapshot(reg, now=datetime.now(timezone.utc))  # type: ignore[arg-type]
        cards = build_inbox(
            fleet_rows=snap.rows,
            findings=reg.read_audit_findings(),
            effects=reg.read_audit_effects(),
            corrections=reg.read_correction_summary(),
        )
        return {"cards": [asdict(c) for c in cards], "count": len(cards)}

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

    @app.post("/api/projects/{project_id}/pause")
    def pause(project_id: str, by: str = Body(default="operator", embed=True)) -> dict[str, object]:
        cid = f"pause_{uuid.uuid4().hex[:12]}"
        write_command(sdir, "pause", command_id=cid, issued_by=by, issued_at=_now_iso())
        return {"command_id": cid, "state": "queued", "project_id": project_id}

    @app.post("/api/projects/{project_id}/budget")
    def budget(
        project_id: str,
        new_cap_usd: str = Body(embed=True),
        by: str = Body(default="operator", embed=True),
    ) -> dict[str, object]:
        cid = f"bump_{uuid.uuid4().hex[:12]}"
        write_command(
            sdir, "bump_budget", command_id=cid, issued_by=by, issued_at=_now_iso(),
            new_cap_usd=new_cap_usd,
        )
        return {"command_id": cid, "state": "queued", "project_id": project_id}

    @app.post("/api/findings/{finding_key}/promote")
    def promote(finding_key: str, by: str = Body(default="operator", embed=True)) -> dict[str, object]:
        registry_provider().set_finding_status(finding_key, "accepted", decided_by=by)
        return {"finding_key": finding_key, "status": "accepted"}

    @app.post("/api/findings/{finding_key}/reject")
    def reject(finding_key: str, by: str = Body(default="operator", embed=True)) -> dict[str, object]:
        registry_provider().set_finding_status(finding_key, "rejected", decided_by=by)
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
        skills_dir = os.environ.get("CLAUDE_SKILLS_DIR", "")
        argv = build_dispatch_command(finding, skills_dir=skills_dir)
        # The dispatch itself (spawns claude) + the _dispatch_succeeded guard live in the CLI's apply
        # path; the server returns the argv it WOULD run so a future phase wires the live spawn behind
        # an explicit confirm. (Phase-1 honesty: we do not silently spawn from a GET-driven UI.)
        return JSONResponse({"finding_key": finding_key, "would_dispatch": argv, "state": "ready"})

    if static_dir is not None and Path(static_dir).is_dir():
        from fastapi.staticfiles import StaticFiles  # noqa: PLC0415 - only when serving a built UI

        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="ui")

    return app


# Module-level app for `uvicorn webui.server.app:app` (live server; reads the env DB).
app = create_app(static_dir=os.environ.get("OL_SUPERVISOR_WEBUI_STATIC"))
