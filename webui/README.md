# Control Panel GUI (webui)

A local web console for the Outer Loop Supervisor — the integrated, attention-first GUI that
replaces the 14-verb CLI mental model. Design: [`docs/control_panel_gui_design.md`](../docs/control_panel_gui_design.md).

**The GUI adds no decision logic.** It is a thin presentation + action layer over the SAME
`supervisor` reads, pure cores, and write seams (Registry, `build_full_fleet_snapshot`,
`build_inbox`, `write_command`, `set_finding_status`, the apply dispatch). The CLI
(`python -m supervisor.control_panel`) remains the headless surface.

```
webui/
  server/   FastAPI read/action API over the supervisor package (Python)
  app/      React + Vite single-page console (TypeScript)
```

## Status (Phase 1)

Implemented and tested: **Home** (Needs-You inbox), **Fleet** (table + pause/bump), **Improve**
(proposed→accepted→applied→measured board), **Effects** (outcome rollup). The API exposes the read
endpoints (fleet/inbox/learnings/effects/corrections/events) and the safe action endpoints
(pause/bump/promote/reject; `apply` returns the argv it would dispatch — the live spawn is a later
phase). SSE live-push, the dependency graph, and the operator action log are future phases.

## Run

```powershell
# 1. API (needs the supervisor env)
$env:OL_SUPERVISOR_DB_URL = "<dev-branch DSN>"      # never the prod ref
$env:OL_SUPERVISOR_STATE_DIR = "<orchestrator state dir>"   # for pause/bump command writes
pip install -e ".[web]"
python -m webui.server --port 8787

# 2. UI (dev server proxies /api -> :8787)
cd webui/app
npm install
npm run dev          # http://localhost:5173

# Production-style: build the UI and let the API serve it
npm run build
$env:OL_SUPERVISOR_WEBUI_STATIC = "$PWD/dist"
python -m webui.server   # UI at http://127.0.0.1:8787/
```

## Test

| Layer | Tool | Command |
|---|---|---|
| Inbox aggregation core (pure) | pytest (hermetic suite) | `python -m pytest tests/test_inbox.py` |
| API (HTTP → fake Registry) | pytest + FastAPI TestClient | `python -m pytest webui/server/tests` |
| UI components (pure views) | Vitest + Testing Library | `cd webui/app && npm test` |
| End-to-end (browser) | Playwright | `cd webui/app && npx playwright install chromium && npm run e2e` |

The Playwright smoke mocks `/api/*` via route interception, so it needs neither the Python server
nor a DB — it verifies the real browser render + interaction + fetch wiring. A live-backend E2E is a
later phase.
