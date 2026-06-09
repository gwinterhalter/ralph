"""Control-panel API (webui.server.app) — real-seam: HTTP → fake Registry + real pure cores.

The Registry read/write is faked (no DB); the pure cores (build_full_fleet_snapshot, build_inbox,
summarize_*) and the FastAPI wiring run for real. Run with:  python -m pytest webui/server/tests
(needs the `web` extra: fastapi/httpx). These tests are OUTSIDE the hermetic supervisor suite.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from webui.server.app import create_app  # noqa: E402


class FakeRegistry:
    def __init__(self) -> None:
        self.candidates: list[dict[str, object]] = []
        self.running: list[dict[str, object]] = [
            {"project_id": "p1", "display_name": "Proj One", "lifecycle_state": "running",
             "attention_debt": 0}
        ]
        self.findings: list[dict[str, object]] = [
            {"finding_key": "answerer_dsl_candidate:g1", "kind": "answerer_dsl_candidate",
             "subject": "g1", "status": "proposed", "recommendation": "add rule",
             "authoring_skill": "cf-spec-writer", "runs_audited": 3},
            {"finding_key": "session_shape:s", "kind": "session_shape", "subject": "s",
             "status": "accepted", "recommendation": "tune", "authoring_skill": "cf-session-plan-reviewer",
             "runs_audited": 2},
        ]
        self.effects: list[dict[str, object]] = [
            {"finding_key": "session_shape:s", "kind": "session_shape", "subject": "s",
             "outcome": "regressed", "before_metric": 0.2, "after_metric": 0.9,
             "post_adoption_runs": 3, "applied_at": "2026-06-08T10:00:00+00:00", "detail": "worse"},
        ]
        self.corrections_rows: list[dict[str, object]] = [
            {"item_id": "OLB-07", "attempts": 5, "projects": 2, "max_level": "L4"},
        ]
        self.events: list[dict[str, object]] = [
            {"ts_utc": "2026-06-08T10:00:00+00:00", "project_id": "p1", "role": "gate",
             "event_type": "gate_fire", "subject_id": "g1", "payload": {"cls": "gate_dc"}},
        ]
        self.learning_rows: list[dict[str, object]] = [
            {"project_slug": "p1", "cost_usd": "2.50"},
        ]
        self.projects: list[dict[str, object]] = [
            {"project_id": "p1", "display_name": "Proj One", "folder_path": "p1",
             "lifecycle_state": "running", "attention_debt": 0, "depends_on": []},
            {"project_id": "p2", "display_name": "Proj Two", "folder_path": "p2",
             "lifecycle_state": "candidate", "attention_debt": 0, "depends_on": ["p1"]},
            {"project_id": "p3", "display_name": "Proj Three", "folder_path": "p3",
             "lifecycle_state": "complete", "attention_debt": 0, "depends_on": []},
            {"project_id": "pg", "display_name": "Proj Gate", "folder_path": "pg",
             "lifecycle_state": "paused_gate", "attention_debt": 1, "depends_on": []},
        ]
        self.completed_runs: list[dict[str, object]] = [
            {"run_id": "r1", "project_id": "p3", "status": "complete", "terminal_cost_usd": "2.50",
             "spawned_at": "2026-06-08T08:00:00+00:00", "terminated_at": "2026-06-08T09:00:00+00:00"},
            {"run_id": "r2", "project_id": "p1", "status": "failed", "terminal_cost_usd": "0.00",
             "spawned_at": "2026-06-08T07:00:00+00:00", "terminated_at": "2026-06-08T07:30:00+00:00"},
        ]
        self.cumulative_spend = Decimal("12.00")
        self.status_calls: list[tuple[str, str, str]] = []
        self.pruned: list[str] = []
        self.upserted: list[str] = []

    def read_candidates(self): return self.candidates
    def read_running(self): return self.running
    def read_all_projects(self): return self.projects
    def read_audit_findings(self): return self.findings
    def read_audit_effects(self): return self.effects
    def read_correction_summary(self): return self.corrections_rows
    def read_learning_records(self): return self.learning_rows
    def read_completed_runs(self): return self.completed_runs
    def read_cumulative_spend_usd(self): return self.cumulative_spend

    def read_events_db(self, *, project_id=None, event_type=None, limit=50):
        rows = self.events
        if project_id is not None:
            rows = [r for r in rows if r.get("project_id") == project_id]
        if event_type is not None:
            rows = [r for r in rows if r.get("event_type") == event_type]
        return rows[:limit]

    def set_finding_status(self, finding_key_value, status, *, decided_by):
        self.status_calls.append((finding_key_value, status, decided_by))
        for f in self.findings:
            if f["finding_key"] == finding_key_value:
                f["status"] = status

    def prune_events(self, *, before_iso):
        self.pruned.append(before_iso)
        return 7

    def upsert_project(self, project_id, *, folder_path, priority, depends_on, lifecycle_state="candidate"):
        self.upserted.append(project_id)
        return True


@pytest.fixture()
def client(tmp_path: Path) -> tuple[TestClient, FakeRegistry]:
    reg = FakeRegistry()
    # capture apply dispatches instead of spawning claude
    dispatched: list[list[str]] = []

    def _fake_dispatch(argv: list[str]) -> tuple[int, str]:
        dispatched.append(argv)
        return 0, "ok"

    app = create_app(registry_provider=lambda: reg, state_dir=tmp_path, dispatcher=_fake_dispatch)
    tc = TestClient(app)
    tc.dispatched = dispatched  # type: ignore[attr-defined]
    tc.state_dir = tmp_path  # type: ignore[attr-defined]
    return tc, reg


def _seed_gate(state_dir: Path) -> str:
    name = "gate_request_0012_0000.json"
    (state_dir / name).write_text(json.dumps({
        "gate_id": "abs-phase-boundary",
        "question_text": "proceed to Phase 1?",
        "project_id": "p1",
        "options": [{"id": "proceed", "label": "Proceed"}, {"id": "hold", "label": "Hold"}],
    }), encoding="utf-8")
    return name


def test_health(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    assert c.get("/api/health").json()["ok"] is True


def test_root_serves_ui_when_static_present(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
    reg = FakeRegistry()
    c = TestClient(create_app(registry_provider=lambda: reg, state_dir=tmp_path, static_dir=tmp_path))
    r = c.get("/")
    assert r.status_code == 200 and "root" in r.text  # the built UI, not a 404


def test_root_help_when_no_static(client: tuple[TestClient, FakeRegistry]) -> None:
    # API-only mode (no built UI): / must explain how to get the UI, not bare-404 (the launch bug).
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "npm run build" in r.json()["ui"]


def test_fleet_serializes_rows(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    body = c.get("/api/fleet").json()
    assert body["rows"][0]["project_id"] == "p1"
    assert body["rows"][0]["display_name"] == "Proj One"
    assert body["running_count"] == 1
    # Decimal serialized as a string, never a float (NFR-007).
    assert isinstance(body["total_cumulative_cost_usd"], str)


def test_inbox_aggregates_learning_and_regressed(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    cards = c.get("/api/inbox").json()["cards"]
    kinds = {card["kind"] for card in cards}
    assert "learning" in kinds   # the proposed finding
    assert "regressed" in kinds  # the non-confirmed effect
    assert "churn" in kinds      # OLB-07 over threshold
    # urgency-ordered
    assert [card["urgency"] for card in cards] == sorted(card["urgency"] for card in cards)


def test_learnings_status_filter(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    allf = c.get("/api/learnings").json()
    assert allf["by_status"] == {"proposed": 1, "accepted": 1}
    only = c.get("/api/learnings", params={"status": "proposed"}).json()
    assert len(only["findings"]) == 1 and only["findings"][0]["status"] == "proposed"


def test_effects_and_corrections(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    eff = c.get("/api/effects").json()
    assert eff["by_outcome"] == {"regressed": 1}
    corr = c.get("/api/corrections").json()
    assert corr["items"][0]["item_id"] == "OLB-07"


def test_events_with_filter_and_metrics(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    body = c.get("/api/events", params={"type": "gate_fire"}).json()
    assert body["events"][0]["event_type"] == "gate_fire"
    assert body["metrics"]["total"] == 1


def test_pause_writes_command_file(client: tuple[TestClient, FakeRegistry], tmp_path: Path) -> None:
    c, _ = client
    body = c.post("/api/projects/p1/pause", json={"by": "greg"}).json()
    assert body["state"] == "queued"
    written = list((tmp_path / "commands").glob("*.json"))
    assert written, "pause must write a command JSON the orchestrator consumes"
    payload = json.loads(written[0].read_text())
    assert payload["command_type"] == "pause" and payload["issued_by"] == "greg"


def test_budget_writes_bump_command(client: tuple[TestClient, FakeRegistry], tmp_path: Path) -> None:
    c, _ = client
    c.post("/api/projects/p1/budget", json={"new_cap_usd": "25.00", "by": "greg"})
    payloads = [json.loads(p.read_text()) for p in (tmp_path / "commands").glob("*.json")]
    assert any(p["command_type"] == "bump_budget" and p["new_cap_usd"] == "25.00" for p in payloads)


def test_promote_and_reject_call_registry(client: tuple[TestClient, FakeRegistry]) -> None:
    c, reg = client
    c.post("/api/findings/answerer_dsl_candidate:g1/promote", json={"by": "greg"})
    c.post("/api/findings/session_shape:s/reject", json={"by": "greg"})
    assert ("answerer_dsl_candidate:g1", "accepted", "greg") in reg.status_calls
    assert ("session_shape:s", "rejected", "greg") in reg.status_calls


def test_apply_dispatches_and_marks_applied(client: tuple[TestClient, FakeRegistry]) -> None:
    c, reg = client
    # 'session_shape:s' starts accepted -> the injected dispatcher runs, finding -> applied.
    ok = c.post("/api/findings/session_shape:s/apply", json={"by": "greg"})
    assert ok.status_code == 200 and ok.json()["status"] == "applied"
    assert c.dispatched and c.dispatched[0][0] == "claude"  # type: ignore[attr-defined]
    assert ("session_shape:s", "applied", "greg") in reg.status_calls
    # a proposed finding is not yet applyable.
    assert c.post("/api/findings/answerer_dsl_candidate:g1/apply", json={"by": "greg"}).status_code == 409
    # an unknown key 404s.
    assert c.post("/api/findings/nope/apply", json={"by": "greg"}).status_code == 404


def test_apply_failure_leaves_accepted(tmp_path: Path) -> None:
    reg = FakeRegistry()

    def _bad_dispatch(_argv: list[str]) -> tuple[int, str]:
        return 0, "Unknown command: /cf-session-plan-reviewer"  # exit 0 but no-op

    c = TestClient(create_app(registry_provider=lambda: reg, state_dir=tmp_path, dispatcher=_bad_dispatch))
    r = c.post("/api/findings/session_shape:s/apply", json={"by": "greg"})
    assert r.status_code == 502  # not falsely 'applied'
    assert ("session_shape:s", "applied", "greg") not in reg.status_calls


def test_gates_list_and_resolve(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    name = _seed_gate(c.state_dir)  # type: ignore[attr-defined]
    listed = c.get("/api/gates").json()["gates"]
    assert listed and listed[0]["gate_id"] == "abs-phase-boundary"
    assert {o["id"] for o in listed[0]["options"]} == {"proceed", "hold"}
    # resolve writes a gate_response_*.json the orchestrator consumes
    r = c.post("/api/gates/resolve", json={"request_file": name, "selected_option": "proceed", "by": "greg"})
    assert r.status_code == 200
    resp = json.loads((c.state_dir / "gate_response_0012_0000.json").read_text())  # type: ignore[attr-defined]
    assert resp["gate_id"] == "abs-phase-boundary" and resp["selected_option"] == "proceed"
    # once answered, it's no longer pending
    assert c.get("/api/gates").json()["gates"] == []


def test_gate_resolve_rejects_bad_option(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    name = _seed_gate(c.state_dir)  # type: ignore[attr-defined]
    bad = c.post("/api/gates/resolve", json={"request_file": name, "selected_option": "nonsense"})
    assert bad.status_code == 400
    missing = c.post("/api/gates/resolve", json={"request_file": "gate_request_9999_0.json", "selected_option": "x"})
    assert missing.status_code == 404


def test_inbox_includes_gate_card_and_budget_breach(client: tuple[TestClient, FakeRegistry], monkeypatch) -> None:
    c, _ = client
    _seed_gate(c.state_dir)  # type: ignore[attr-defined]
    monkeypatch.setenv("OL_SUPERVISOR_BUDGET_CEILING_USD", "5.00")  # spend 12 >= 5 -> breach
    cards = c.get("/api/inbox").json()["cards"]
    kinds = [card["kind"] for card in cards]
    assert "budget" in kinds and kinds[0] == "budget"  # most urgent
    gate = next(card for card in cards if card["kind"] == "gate")
    assert gate["detail"] == "proceed to Phase 1?" and "proceed" in gate["actions"]


def test_forecast(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    body = c.get("/api/forecast").json()
    assert isinstance(body, dict)  # serialized FleetForecast (shape-agnostic)


def test_onramp_dry_run_then_apply(client: tuple[TestClient, FakeRegistry]) -> None:
    c, reg = client
    dry = c.post("/api/onramp-abs").json()
    assert dry["applied"] is False and len(dry["plan"]) == 3
    applied = c.post("/api/onramp-abs", params={"apply": "true"}).json()
    assert applied["applied"] is True and "abs_phase0" in applied["created"]
    assert "abs_phase0" in reg.upserted


def test_events_prune(client: tuple[TestClient, FakeRegistry]) -> None:
    c, reg = client
    body = c.post("/api/events-prune", params={"days": 30}).json()
    assert body["deleted"] == 7 and reg.pruned


def test_actions_log_records_every_action(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    c.post("/api/projects/p1/pause", json={"by": "greg"})
    c.post("/api/findings/answerer_dsl_candidate:g1/promote", json={"by": "greg"})
    acts = c.get("/api/actions").json()["actions"]
    kinds = {a["action"] for a in acts}
    assert {"pause", "promote"} <= kinds
    assert acts[0]["action"] == "promote"  # most recent first


def test_commands_lists_pending(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    c.post("/api/projects/p1/pause", json={"by": "greg"})
    pending = c.get("/api/commands").json()
    assert pending["count"] == 1 and pending["pending"][0]["command_type"] == "pause"


def test_projects_lists_all_with_cost_and_runs(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    body = c.get("/api/projects").json()
    by_id = {p["project_id"]: p for p in body["projects"]}
    assert set(by_id) == {"p1", "p2", "p3", "pg"}            # ALL lifecycle states, not just active
    assert by_id["p3"]["lifecycle_state"] == "complete" and by_id["p3"]["cost_usd"] == "2.50"
    assert by_id["pg"]["lifecycle_state"] == "paused_gate"   # the otherwise-hidden one
    assert by_id["p1"]["runs"] == 1


def test_runs_history(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    body = c.get("/api/runs").json()
    assert body["count"] == 2 and body["total_cost_usd"] == "2.50"
    assert body["runs"][0]["terminated_at"] >= body["runs"][1]["terminated_at"]  # most recent first


def test_loop_status_reports_last_activity(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    body = c.get("/api/loop-status").json()
    assert "active_guess" in body and "seconds_since" in body


def test_inbox_surfaces_paused_gate_project(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    cards = c.get("/api/inbox").json()["cards"]
    gate = [card for card in cards if card["kind"] == "gate" and card["subject"] == "pg"]
    assert gate and "investigate" in gate[0]["actions"]  # paused_gate project 'pg' is now visible


def test_revert_records_request(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    c.post("/api/findings/session_shape:s/promote")  # exists; any status
    r = c.post("/api/findings/session_shape:s/revert", json={"by": "greg"})
    assert r.status_code == 200 and r.json()["state"] == "revert-requested"
    acts = c.get("/api/actions").json()["actions"]
    assert any(a["action"] == "revert-requested" for a in acts)
    assert c.post("/api/findings/nope/revert").status_code == 404


def test_graph_nodes_and_depends_on_edges(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    g = c.get("/api/graph").json()
    assert {"p1", "p2", "p3", "pg"} <= {n["id"] for n in g["nodes"]}
    assert {"from": "p2", "to": "p1"} in g["edges"]  # p2 depends_on p1


def test_stream_pushes_inbox_and_fleet(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    with c.stream("GET", "/api/stream", params={"max_events": 1}) as r:
        body = "".join(r.iter_text())
    assert body.startswith("data: ")
    payload = json.loads(body[len("data: "):].strip())
    assert "inbox" in payload and "fleet" in payload
    assert "cards" in payload["inbox"] and "rows" in payload["fleet"]


def test_auth_required_when_token_set(tmp_path: Path) -> None:
    reg = FakeRegistry()
    c = TestClient(create_app(registry_provider=lambda: reg, state_dir=tmp_path, token="s3cret"))
    assert c.get("/api/health").status_code == 200          # health is always open
    assert c.get("/api/inbox").status_code == 401            # no token -> blocked
    assert c.get("/api/inbox", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/api/inbox", params={"token": "s3cret"}).status_code == 200   # SSE query path
    assert c.get("/api/inbox", params={"token": "wrong"}).status_code == 401
