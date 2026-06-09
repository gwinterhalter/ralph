"""Demo server — the REAL API (webui.server.app) over an in-memory seeded registry (no DB).

Purpose: the live-backend Playwright E2E (webui/app/e2e/live.spec.ts) drives the actual UI against
this real FastAPI app, so it proves the genuine UI <-> API <-> pure-core contract end-to-end (the
mocked smoke could not catch a JSON-shape drift between app.py and api.ts). The registry is a
stateful module-level singleton so an action (promote) persists and the next render reflects it.

Run:  OL_SUPERVISOR_WEBUI_STATIC=<app/dist> python -m uvicorn webui.server.demo:app --port 8788
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path

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
        self._projects: list[dict[str, object]] = [
            {"project_id": "abs_phase0", "display_name": "abs_phase0", "folder_path": "abs_phase0",
             "lifecycle_state": "complete", "attention_debt": 0, "depends_on": []},
            {"project_id": "abs_phase1", "display_name": "abs_phase1", "folder_path": "abs_phase1",
             "lifecycle_state": "running", "attention_debt": 0, "depends_on": ["abs_phase0"]},
            {"project_id": "abs_phase2", "display_name": "abs_phase2", "folder_path": "abs_phase2",
             "lifecycle_state": "candidate", "attention_debt": 0, "depends_on": ["abs_phase1"]},
            {"project_id": "oltest_old", "display_name": "oltest_old", "folder_path": "oltest_old",
             "lifecycle_state": "failed", "attention_debt": 0, "depends_on": []},
            {"project_id": "oltest_paused", "display_name": "oltest_paused", "folder_path": "oltest_paused",
             "lifecycle_state": "paused_gate", "attention_debt": 1, "depends_on": []},
            {"project_id": "proposed_demo", "display_name": "proposed_demo", "folder_path": "proposed_demo",
             "lifecycle_state": "pending_approval", "attention_debt": 0, "depends_on": []},
        ]

    def read_candidates(self) -> Sequence[Mapping[str, object]]:
        return [{"project_id": "abs_phase2", "display_name": "abs_phase2",
                 "lifecycle_state": "candidate", "attention_debt": 0}]

    def read_running(self) -> Sequence[Mapping[str, object]]:
        # abs_phase1 carries the seeded gate (resolving removes that card) + the drill-down events.
        return [{"project_id": "abs_phase1", "display_name": "abs_phase1",
                 "lifecycle_state": "running", "attention_debt": 0}]

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

    def read_all_projects(self) -> Sequence[Mapping[str, object]]:
        return self._projects

    def set_lifecycle_state(self, project_id: str, state: str) -> None:
        for p in self._projects:
            if p["project_id"] == project_id:
                if p["lifecycle_state"] == "pending_approval" and state == "candidate":
                    p["lifecycle_state"] = state
                    return
                raise ValueError(f"illegal {p['lifecycle_state']}->{state}")
        raise ValueError(f"unknown project {project_id}")

    def delete_pending_project(self, project_id: str) -> bool:
        for i, p in enumerate(self._projects):
            if p["project_id"] == project_id and p["lifecycle_state"] == "pending_approval":
                del self._projects[i]
                return True
        return False

    def read_learning_records(self) -> Sequence[Mapping[str, object]]:
        return [{"project_slug": "oltest_d2", "cost_usd": "2.50"}]

    def read_completed_runs(self) -> Sequence[Mapping[str, object]]:
        return [
            {"run_id": "rA", "project_id": "abs_phase0", "status": "complete",
             "terminal_cost_usd": "2.76", "spawned_at": "2026-06-08T08:00:00+00:00",
             "terminated_at": "2026-06-08T09:00:00+00:00"},
            {"run_id": "rB", "project_id": "oltest_old", "status": "failed",
             "terminal_cost_usd": "0.41", "spawned_at": "2026-06-07T10:00:00+00:00",
             "terminated_at": "2026-06-07T10:20:00+00:00"},
        ]

    def read_cumulative_spend_usd(self) -> Decimal:
        return Decimal("5.30")

    def prune_events(self, *, before_iso: str) -> int:
        return 0

    def upsert_project(
        self, project_id: str, *, folder_path: str, priority: int,
        depends_on: Sequence[str], lifecycle_state: str = "candidate"
    ) -> bool:
        return True

    def read_events_db(
        self, *, project_id: str | None = None, event_type: str | None = None, limit: int = 50
    ) -> Sequence[Mapping[str, object]]:
        rows = [
            {"ts_utc": "2026-06-08T10:00:00+00:00", "project_id": "abs_phase1", "role": "gate",
             "event_type": "gate_fire", "subject_id": "abs-phase-boundary", "payload": {}},
            {"ts_utc": "2026-06-08T09:30:00+00:00", "project_id": "abs_phase1", "role": "executor",
             "event_type": "phase_complete", "subject_id": "plan", "payload": {}},
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


def _seed_pending_gate(state_dir: Path) -> None:
    """Reset the demo E2E state on each fresh server start: clear the prior gate response + action
    log (so the run is deterministic) and write a pending gate_request to exercise resolution."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "gate_response_0012_0000.json").unlink(missing_ok=True)
    (state_dir / "operator_actions.jsonl").unlink(missing_ok=True)
    (state_dir / "gate_request_0012_0000.json").write_text(
        json.dumps({
            "gate_id": "abs-phase-boundary",
            "question_text": "proceed to Phase 1?",
            "project_id": "abs_phase1",
            "options": [
                {"id": "proceed", "label": "Proceed", "consequence": "advance to Phase 1"},
                {"id": "hold", "label": "Hold", "consequence": "stay in Phase 0"},
            ],
        }),
        encoding="utf-8",
    )


class _DemoRunner:
    """In-memory loop runner — the demo/E2E exercises Start/Stop/Run-once WITHOUT spawning a real
    supervisor (no real orchestrators, no $)."""

    def __init__(self) -> None:
        self._running = False

    def run_once(self) -> int:
        return 1111

    def start_loop(self, interval: float = 30.0) -> int:
        if self._running:
            raise RuntimeError("supervisor loop already running")
        self._running = True
        return 2222

    def stop_loop(self) -> bool:
        was = self._running
        self._running = False
        return was

    def loop_running(self) -> bool:
        return self._running


_STATE_DIR = Path(os.environ.get("OL_SUPERVISOR_STATE_DIR", "."))
_seed_pending_gate(_STATE_DIR)
_REGISTRY = _SeededRegistry()

app = create_app(
    registry_provider=lambda: _REGISTRY,
    state_dir=_STATE_DIR,
    static_dir=os.environ.get("OL_SUPERVISOR_WEBUI_STATIC"),
    runner=_DemoRunner(),
)
