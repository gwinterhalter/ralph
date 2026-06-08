"""Demo server — the REAL API (webui.server.app) over an in-memory seeded registry (no DB).

Purpose: the live-backend Playwright E2E (webui/app/e2e/live.spec.ts) drives the actual UI against
this real FastAPI app, so it proves the genuine UI <-> API <-> pure-core contract end-to-end (the
mocked smoke could not catch a JSON-shape drift between app.py and api.ts). The registry is a
stateful module-level singleton so an action (promote) persists and the next render reflects it.

Run:  OL_SUPERVISOR_WEBUI_STATIC=<app/dist> python -m uvicorn webui.server.demo:app --port 8788
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from webui.server.app import create_app


class _SeededRegistry:
    """A minimal in-memory RegistryLike with realistic seeded rows (demo/E2E only — no DB)."""

    def __init__(self) -> None:
        self._findings: list[dict[str, object]] = [
            {"finding_key": "answerer_dsl_candidate:abs-phase-boundary", "kind": "answerer_dsl_candidate",
             "subject": "abs-phase-boundary", "status": "proposed", "recommendation": "add an Answerer rule",
             "routes_to": "operator + cf-spec-writer", "authoring_skill": "cf-spec-writer", "runs_audited": 4},
            {"finding_key": "session_shape:spec_review_loop", "kind": "session_shape",
             "subject": "spec_review_loop", "status": "applied", "recommendation": "tune the shape",
             "routes_to": "operator", "authoring_skill": "cf-session-plan-reviewer", "runs_audited": 3},
        ]

    def read_candidates(self) -> Sequence[Mapping[str, object]]:
        return []

    def read_running(self) -> Sequence[Mapping[str, object]]:
        return [
            {"project_id": "oltest_c2", "display_name": "oltest_c2", "lifecycle_state": "paused_gate",
             "attention_debt": 1},
            {"project_id": "oltest_d2", "display_name": "oltest_d2", "lifecycle_state": "running",
             "attention_debt": 0},
        ]

    def read_audit_findings(self) -> Sequence[Mapping[str, object]]:
        return self._findings

    def read_audit_effects(self) -> Sequence[Mapping[str, object]]:
        return [
            {"finding_key": "session_shape:spec_review_loop", "kind": "session_shape",
             "subject": "spec_review_loop", "outcome": "regressed", "before_metric": 0.2,
             "after_metric": 0.9, "post_adoption_runs": 3, "applied_at": "2026-06-08T10:00:00+00:00",
             "detail": "revise-rate rose"},
        ]

    def read_correction_summary(self) -> Sequence[Mapping[str, object]]:
        return [{"item_id": "OLB-07", "attempts": 5, "projects": 2, "max_level": "L4"}]

    def read_events_db(
        self, *, project_id: str | None = None, event_type: str | None = None, limit: int = 50
    ) -> Sequence[Mapping[str, object]]:
        rows = [
            {"ts_utc": "2026-06-08T10:00:00+00:00", "project_id": "oltest_c2", "role": "gate",
             "event_type": "gate_fire", "subject_id": "abs-phase-boundary", "payload": {}},
        ]
        if project_id is not None:
            rows = [r for r in rows if r["project_id"] == project_id]
        if event_type is not None:
            rows = [r for r in rows if r["event_type"] == event_type]
        return rows[:limit]

    def set_finding_status(self, finding_key_value: str, status: str, *, decided_by: str) -> None:
        for f in self._findings:
            if f["finding_key"] == finding_key_value:
                f["status"] = status


_REGISTRY = _SeededRegistry()

app = create_app(
    registry_provider=lambda: _REGISTRY,
    static_dir=os.environ.get("OL_SUPERVISOR_WEBUI_STATIC"),
)
