"""Control-panel API (webui.server.app) — real-seam: HTTP → fake Registry + real pure cores.

The Registry read/write is faked (no DB); the pure cores (build_full_fleet_snapshot, build_inbox,
summarize_*) and the FastAPI wiring run for real. Run with:  python -m pytest webui/server/tests
(needs the `web` extra: fastapi/httpx). These tests are OUTSIDE the hermetic supervisor suite.
"""
from __future__ import annotations

import json
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
        self.status_calls: list[tuple[str, str, str]] = []

    def read_candidates(self): return self.candidates
    def read_running(self): return self.running
    def read_audit_findings(self): return self.findings
    def read_audit_effects(self): return self.effects
    def read_correction_summary(self): return self.corrections_rows

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


@pytest.fixture()
def client(tmp_path: Path) -> tuple[TestClient, FakeRegistry]:
    reg = FakeRegistry()
    app = create_app(registry_provider=lambda: reg, state_dir=tmp_path)
    return TestClient(app), reg


def test_health(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    assert c.get("/api/health").json()["ok"] is True


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


def test_apply_requires_accepted(client: tuple[TestClient, FakeRegistry]) -> None:
    c, _ = client
    # 'session_shape:s' starts accepted -> returns the argv it would dispatch.
    ok = c.post("/api/findings/session_shape:s/apply", json={"by": "greg"})
    assert ok.status_code == 200 and ok.json()["would_dispatch"][0] == "claude"
    # a proposed finding is not yet applyable.
    conflict = c.post("/api/findings/answerer_dsl_candidate:g1/apply", json={"by": "greg"})
    assert conflict.status_code == 409
    # an unknown key 404s.
    assert c.post("/api/findings/nope/apply", json={"by": "greg"}).status_code == 404
