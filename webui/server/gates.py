"""Gate artifact I/O for the control panel — read pending gate requests, write operator responses.

Gates surface as ``gate_request_NNNN_M.json`` files in the orchestrator state dir (schema:
schemas/gate_request.schema.json). The operator's decision is a ``gate_response_NNNN_M.json`` with
the matching name (schema: gate_response.schema.json) — the SAME artifact rl-operator-answerer
writes, consumed by the orchestrator's gate flow. So resolving a gate from the GUI is a real seam,
parallel to the command channel: list the unanswered requests, write the chosen response.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GATE_REQUEST_GLOB = "gate_request_*.json"


def _response_name(request_name: str) -> str:
    return request_name.replace("gate_request_", "gate_response_", 1)


def _normalize_options(options: object) -> list[dict[str, str]]:
    """Normalise the request's options (string OR {id,label,consequence}) to {id,label,consequence}."""
    out: list[dict[str, str]] = []
    if isinstance(options, list):
        for o in options:
            if isinstance(o, str):
                out.append({"id": o, "label": o, "consequence": ""})
            elif isinstance(o, dict):
                oid = str(o.get("id") or o.get("label") or "")
                out.append({
                    "id": oid,
                    "label": str(o.get("label") or oid),
                    "consequence": str(o.get("consequence") or ""),
                })
    return out


def list_pending_gates(state_dir: str | Path) -> list[dict[str, Any]]:
    """Pending gates: every ``gate_request_*.json`` without a matching ``gate_response_*.json``."""
    d = Path(state_dir)
    if not d.is_dir():
        return []
    pending: list[dict[str, Any]] = []
    for req in sorted(d.glob(GATE_REQUEST_GLOB)):
        if (d / _response_name(req.name)).exists():
            continue
        try:
            data = json.loads(req.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        pending.append({
            "request_file": req.name,
            "gate_id": str(data.get("gate_id", "")),
            "question_text": str(data.get("question_text", "")),
            "options": _normalize_options(data.get("options")),
            "cluster": data.get("cluster"),
            "project_id": str(data.get("project_id") or data.get("initiative_slug") or ""),
        })
    return pending


def write_gate_response(
    state_dir: str | Path,
    request_file: str,
    *,
    selected_option: str,
    reasoning: str,
    confidence: float = 1.0,
    classification_check: str = "operator",
) -> Path:
    """Write the operator's ``gate_response_*.json`` answering ``request_file`` (validates the option).

    Raises FileNotFoundError if the request is gone, ValueError if ``selected_option`` is not one of
    the request's enumerated option ids. The orchestrator's gate flow consumes the response file."""
    d = Path(state_dir)
    req = d / request_file
    if not req.is_file():
        raise FileNotFoundError(request_file)
    data = json.loads(req.read_text(encoding="utf-8"))
    option_ids = {o["id"] for o in _normalize_options(data.get("options"))}
    if option_ids and selected_option not in option_ids:
        raise ValueError(f"{selected_option!r} is not an option of {request_file}: {sorted(option_ids)}")
    payload = {
        "gate_id": str(data.get("gate_id", "")),
        "selected_option": selected_option,
        "reasoning": reasoning,
        "confidence": confidence,
        "classification_check": classification_check,
    }
    out = d / _response_name(request_file)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


__all__ = ["GATE_REQUEST_GLOB", "list_pending_gates", "write_gate_response"]
