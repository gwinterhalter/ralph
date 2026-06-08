"""Learning effect-measurement (supervisor.effect_measure) — real-seam: raw events → outcome."""
from __future__ import annotations

import pytest

from supervisor.effect_measure import (
    CONFIRMED,
    NO_EFFECT,
    PENDING,
    REGRESSED,
    measure_effect,
    relevant_project_ids,
    run_signal,
)

pytestmark = pytest.mark.unit


def _gate(gid: str, *, human: bool, escalate: bool = False, pid: str = "p1") -> list[dict[str, object]]:
    evs: list[dict[str, object]] = [
        {"event_type": "gate_fire", "subject_id": gid, "project_id": pid,
         "payload": {"gate_id": gid, "cls": "gate_human" if human else "gate_dc"}}
    ]
    if escalate:
        evs.append({"event_type": "gate_escalate", "subject_id": gid, "project_id": pid, "payload": {"gate_id": gid}})
    return evs


def _verif(binding: str, result: str, pid: str = "p1") -> list[dict[str, object]]:
    return [{"event_type": "verification", "subject_id": binding, "subject_kind": "binding",
             "project_id": pid, "payload": {"binding": binding, "result": result}}]


def _revise(shape: str, *, revise: bool, pid: str = "p1") -> list[dict[str, object]]:
    return [{"event_type": "revise_round", "project_id": pid,
             "payload": {"shape": shape, "verdict": "revise" if revise else "converged",
                         "findings_blocker": 1 if revise else 0, "findings_drift": 0}}]


def _corr(item: str, pid: str = "p1") -> list[dict[str, object]]:
    return [{"event_type": "correction_attempt", "subject_id": item, "project_id": pid,
             "payload": {"item_id": item, "level": "L3"}}]


# --- run_signal per kind (None = run is not evidence) ---

def test_run_signal_gate() -> None:
    assert run_signal("answerer_dsl_candidate", "g", _gate("g", human=True, escalate=True)) == 1.0
    assert run_signal("answerer_dsl_candidate", "g", _gate("g", human=False)) == 0.0  # fired, auto
    assert run_signal("answerer_dsl_candidate", "g", _verif("b", "pass")) is None  # gate absent


def test_run_signal_binding_and_shape_and_correction() -> None:
    assert run_signal("binding_defect", "b", _verif("b", "fail")) == 0.0
    assert run_signal("binding_defect", "b", _verif("b", "pass")) == 1.0
    assert run_signal("binding_defect", "b", _corr("x")) is None  # binding absent
    assert run_signal("over_verification", "b", _verif("b", "pass")) == 1.0
    assert run_signal("over_verification", "b", _corr("x")) == 0.0  # presence: absent → 0 (never None)
    assert run_signal("session_shape", "s", _revise("s", revise=True)) == 1.0
    assert run_signal("session_shape", "s", _revise("s", revise=False)) == 0.0
    assert run_signal("correction_pattern", "i", _corr("i")) == 1.0
    assert run_signal("correction_pattern", "i", _verif("b", "pass")) == 0.0


# --- measure_effect outcomes (≥3 post-runs to leave pending) ---

def test_gate_confirmed() -> None:
    rec = measure_effect("answerer_dsl_candidate", "g",
                         before_runs=[_gate("g", human=True, escalate=True)] * 3,
                         after_runs=[_gate("g", human=False)] * 3, finding_key="k")
    assert rec.outcome == CONFIRMED and rec.before_metric == 1.0 and rec.after_metric == 0.0


def test_pending_when_too_few_post_runs() -> None:
    rec = measure_effect("answerer_dsl_candidate", "g",
                         before_runs=[_gate("g", human=True, escalate=True)] * 3,
                         after_runs=[_gate("g", human=False)], finding_key="k")
    assert rec.outcome == PENDING


def test_gate_regressed() -> None:
    rec = measure_effect("answerer_dsl_candidate", "g",
                         before_runs=[_gate("g", human=False)] * 3,
                         after_runs=[_gate("g", human=True, escalate=True)] * 3, finding_key="k")
    assert rec.outcome == REGRESSED


def test_binding_defect_confirmed() -> None:
    rec = measure_effect("verification_binding", "b", binding_class="binding_defect",
                         before_runs=[_verif("b", "fail")] * 3, after_runs=[_verif("b", "pass")] * 3,
                         finding_key="k")
    assert rec.outcome == CONFIRMED


def test_over_verification_confirmed_with_limitation_note() -> None:
    rec = measure_effect("verification_binding", "b", binding_class="over_verification",
                         before_runs=[_verif("b", "pass")] * 3, after_runs=[_corr("x")] * 3,
                         finding_key="k")
    assert rec.outcome == CONFIRMED  # binding absent after → spend saved
    assert "cannot prove no defect" in rec.detail  # D3 limitation recorded


def test_shape_confirmed_and_correction_confirmed() -> None:
    s = measure_effect("session_shape", "s", before_runs=[_revise("s", revise=True)] * 3,
                       after_runs=[_revise("s", revise=False)] * 3, finding_key="k")
    assert s.outcome == CONFIRMED
    c = measure_effect("correction_pattern", "i", before_runs=[_corr("i")] * 3,
                       after_runs=[_verif("b", "pass")] * 3, finding_key="k")
    assert c.outcome == CONFIRMED  # item stopped entering the loop


def test_no_effect_when_unchanged() -> None:
    rec = measure_effect("correction_pattern", "i", before_runs=[_corr("i")] * 3,
                        after_runs=[_corr("i")] * 3, finding_key="k")
    assert rec.outcome == NO_EFFECT


def test_relevant_project_ids_scopes_to_subject() -> None:
    events = _gate("g", human=True, pid="pa") + _verif("b", "pass", pid="pb")
    assert relevant_project_ids("answerer_dsl_candidate", "g", events) == {"pa"}
    assert relevant_project_ids("verification_binding", "b", events, binding_class="over_verification") == {"pb"}
