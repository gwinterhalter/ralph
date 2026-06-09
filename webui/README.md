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

## Status

Implemented and tested: **Home** (Needs-You inbox — gates, stalls, budget breach, learnings,
regressions, churn), **Fleet** (table + pause/bump), **Improve** (proposed→accepted→applied→measured
board), **Effects**, **Spend** (forecast + provision ABS chain + prune), **Events** (timeline),
**Actions** (operator action log). Endpoints: reads
(fleet/inbox/gates/learnings/effects/corrections/events/forecast/commands/actions) + actions
(pause/bump/**gate-resolve**/promote/reject/**apply** (real dispatch)/onramp-abs/events-prune). Gate
resolution writes a real `gate_response_*.json`; `apply` actually dispatches the authoring skill
(injectable; guarded against false success); every action is recorded to the operator action log;
the budget-breach card wires `read_cumulative_spend_usd` vs `OL_SUPERVISOR_BUDGET_CEILING_USD`.

**Live push (SSE):** the UI no longer polls — it subscribes to `GET /api/stream` (Server-Sent
Events) and the server pushes `{inbox, fleet}` every few seconds. **Graph** tab renders the project
`depends_on` chain (layered by depth). **Fleet** rows expand to a per-project recent-events
drill-down. **Auth:** set `OL_SUPERVISOR_WEBUI_TOKEN` and every `/api/*` route except `/api/health`
requires it via `Authorization: Bearer <token>` or `?token=` (open by default — localhost
single-operator assumption); the UI reads the token from its own `?token=` URL param.

Still future: richer graph layout (a real graph lib), per-run cost drill-down, multi-initiative
grouping.

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
| E2E — mocked API (fast) | Playwright | `cd webui/app && npx playwright install chromium && npm run e2e` |
| E2E — live backend (full contract) | Playwright + real FastAPI | `cd webui/app && npx playwright test -c playwright.live.config.ts` |

Two E2E modes:
- **Mocked** (`e2e/smoke.spec.ts`): `/api/*` is stubbed via route interception — needs neither the
  Python server nor a DB; verifies the browser render + interaction + fetch wiring fast.
- **Live backend** (`e2e-live/live.spec.ts`): drives the UI against the REAL FastAPI server
  (`webui.server.demo`) over a seeded in-memory registry — NO mocking. Proves the genuine
  UI ↔ API ↔ pure-core JSON contract and full action round-trips (gate resolve, Adopt
  proposed→accepted, Spend/Events/Graph/Actions, fleet drill-down). `npm run build` first.
- **Real DB** (`e2e-db/db.spec.ts`, opt-in): drives the UI against the REAL app wired to the live
  Registry → the actual database. Read-only; proves the UI ↔ API ↔ Registry ↔ DB path through the
  browser. Needs the disposable-branch DSN:
  `$env:OL_SUPERVISOR_DB_URL="<dev DSN>"; npx playwright test -c playwright.db.config.ts` (NEVER prod).

CI: `.github/workflows/webui-ci.yml` runs the Python gates + Vitest + the mocked & live-backend E2E
on every push/PR touching `supervisor/` or `webui/`.
