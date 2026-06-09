"""Control Panel GUI — local web console (FastAPI API + React app).

This package is intentionally OUTSIDE the strict-gated ``supervisor/`` package and the hermetic
pytest suite (pyproject ``files = ["supervisor"]`` / ``testpaths = ["tests"]``). It carries the web
dependencies (FastAPI/uvicorn/httpx) declared as the ``web`` optional-dependency extra, and is a thin
presentation + action layer over the SAME ``supervisor`` reads/pure-cores/write-seams — it adds no
decision logic (design: docs/control_panel_gui_design.md).
"""
