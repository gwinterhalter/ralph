"""Gate artifact I/O for the control panel — read pending gate requests, write operator responses.

Gates surface as ``gate_request_NNNN_M.json`` files (schema: schemas/gate_request.schema.json). The
operator's decision is a ``gate_response_NNNN_M.json`` with the matching name (schema:
gate_response.schema.json) — the SAME artifact rl-operator-answerer writes, consumed by the
orchestrator's gate flow. Resolving a gate from the GUI is a real seam.

Real fleets put each project's gate files in that project's own state dir
(``<workspace_root>/<folder_path>/state/``), not one shared dir — so the scanners take a LIST of
directories and every pending gate carries its absolute ``request_path`` (the resolution key), which
``write_gate_response`` writes the response next to.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

GATE_REQUEST_GLOB = "gate_request_*.json"


def _response_path(request_path: Path) -> Path:
    return request_path.with_name(request_path.name.replace("gate_request_", "gate_response_", 1))


def _normalize_options(options: object) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if isinstance(options, list):
        for o in options:
            if isinstance(o, str):
                out.append({"id": o, "label": o, "consequence": ""})
            elif isinstance(o, dict):
                oid = str(o.get("id") or o.get("label") or "")
                out.append({"id": oid, "label": str(o.get("label") or oid),
                            "consequence": str(o.get("consequence") or "")})
    return out


def list_pending_gates(dirs: Iterable[str | Path]) -> list[dict[str, Any]]:
    """Pending gates across all given dirs: each ``gate_request_*.json`` lacking its response."""
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in dirs:
        d = Path(raw)
        if not d.is_dir() or str(d) in seen:
            continue
        seen.add(str(d))
        for req in sorted(d.glob(GATE_REQUEST_GLOB)):
            if _response_path(req).exists():
                continue
            try:
                data = json.loads(req.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            pending.append({
                "request_path": str(req),
                "request_file": req.name,
                "gate_id": str(data.get("gate_id", "")),
                "question_text": str(data.get("question_text", "")),
                "options": _normalize_options(data.get("options")),
                "cluster": data.get("cluster"),
                "project_id": str(data.get("project_id") or data.get("initiative_slug") or ""),
            })
    return pending


def write_gate_response(
    request_path: str | Path,
    *,
    selected_option: str,
    reasoning: str,
    confidence: float = 1.0,
    classification_check: str = "operator",
) -> Path:
    """Write the operator's ``gate_response_*.json`` next to ``request_path`` (validates the option)."""
    req = Path(request_path)
    if not req.is_file():
        raise FileNotFoundError(str(request_path))
    data = json.loads(req.read_text(encoding="utf-8"))
    option_ids = {o["id"] for o in _normalize_options(data.get("options"))}
    if option_ids and selected_option not in option_ids:
        raise ValueError(f"{selected_option!r} is not an option of {req.name}: {sorted(option_ids)}")
    payload = {
        "gate_id": str(data.get("gate_id", "")),
        "selected_option": selected_option,
        "reasoning": reasoning,
        "confidence": confidence,
        "classification_check": classification_check,
    }
    out = _response_path(req)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


__all__ = ["GATE_REQUEST_GLOB", "list_pending_gates", "write_gate_response"]
