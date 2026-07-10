# OL Supervisor — NSSM Daemonization Setup (v1.0)

| | |
|---|---|
| **Status** | **DEFERRED (2026-07-09) — hardened & ready, NOT executed.** On-demand operation chosen for now (the fleet is deliberately parked, so a resident service would only idle). Session-0 blockers were pre-cleared (see *Session-0 stability* row). **Resume at §2** when approved-work throughput makes hand-starting a chore. Install/remove needs **elevation** (CF Elevation Helper). Meanwhile, run on demand with `ralph\ops\run-supervisor-foreground.ps1`; check state with `ralph\ops\supervisor-status.ps1`. |
| **Purpose** | Companion to `Supervised_First_Flight_Runbook §7` — the concrete NSSM service definition + env/governor settings that convert `python -m supervisor` from a hand-started foreground process into a resident service ("the fleet drains while you work"). |
| **Machine** | Z2-WINTERHALTER (paths + account are machine-specific — verify per machine, §9 universal-rules). |
| **Verified inputs (2026-07-09)** | `python.exe` = `C:\Users\Winterhalter\AppData\Local\Python\pythoncore-3.14-64\python.exe`; `bash.exe` = `C:\Program Files\Git\bin\bash.exe`; `PROD_DB_URL`/`SUPABASE_ACCESS_TOKEN`/`F_GMAIL_SMTP_APP_PASSWORD` already machine-set; **`nssm.exe` = `C:\ProgramData\chocolatey\bin\nssm.exe` (2.24)**. |
| **Session-0 stability (verified 2026-07-09)** | **`K:` = physical local volume "2TB M2" (DriveType 3), NOT a subst/mapped drive → present in session 0.** `claude.exe` = `C:\Users\Winterhalter\.local\bin\claude.exe`, on the **Machine** PATH (a service inherits it → bare `claude` resolves). `~/.claude` credential lives in the `Winterhalter` profile (readable by a process running as that account). Headless-`claude` non-interactive auth already proven by the sv_smoke run ($3.27). **Residual:** only the session-0 *service* context is unproven — first `nssm start` + first approved dispatch prove it. |

---

## 0. Prerequisites — ALL must hold before you install

- [x] **Fleet gated.** 0 `candidate` rows (all Sub_Projects at `pending_approval`). Done 2026-07-09. *A resident supervisor dispatches every `candidate` up to the ceiling — this is the load-bearing safety gate.* (Regression-tested: `tests/test_phase3_unattended_approval_gate.py`.)
- [x] **Watched ONE foreground supervisor cycle end-to-end** — **done 2026-07-09.** `python -m supervisor --once` (from the **ralph** cwd, ceiling=1): preflight passed, `re-attach 0/0`, `Learn — 0 findings over 55 Runs`, `ingested 43 events`, and dispatched **0** — verified by (a) no dispatch line, (b) DB unchanged (15 pending_approval / 0 candidate·admitted·running), (c) no orchestrator spawned; clean exit 0. Proved preflight/reconcile/**schedule-gating**/learn/ingest.
- [x] **Watched the supervisor ADMIT → SPAWN → DRAIN → RECONCILE one project** — **done 2026-07-09** via a throwaway `sv_smoke` 1-item pilot: the supervisor moved it `candidate → admitted → running`, spawned the orchestrator (headless `claude` **resolved rl-* skills from the confined cwd**), the orchestrator drained to `INITIATIVE_COMPLETE`, and the supervisor **reconciled `running → complete`** (Learn count 55→56; `LIFECYCLE — emailed 'sv_smoke finished'`). Cost $3.27; fleet re-gated after. This proves headless `claude` works under a supervisor-spawned orchestrator — the **only remaining unknown is the Windows-service session-0 wrapper itself**, which the first `nssm start` (§3) exercises.
  - **FR-034 gotcha (found here):** supervisor admission **REJECTS** any seed whose `read_only_paths` omits the canonical corpus (`ADMISSION REJECTED … read_only_invariant_violation`). Every seed MUST list `…\Sub_Projects\Factory_Design\design\Project_Docs_Current` (and any design zone) as read-only. The `rl-project-intake` skeleton defaults `read_only_paths: []`, so intake-scaffolded projects fail admission until fixed → **FUP-0935**. (Direct orchestrator runs bypass this — admission-only.)
  ```powershell
  # ralph cwd — the supervisor PACKAGE imports here (import fails from Factory_V3; it is not pip-installed).
  cd "K:\OneDrive - EPM Solutions - Project Server- Project Online\Code_Factory\Factory_V3\Python_Executions\ralph"
  $env:OL_SUPERVISOR_WORKSPACE_ROOT = "K:\OneDrive - EPM Solutions - Project Server- Project Online\Code_Factory\Factory_V3"  # folder_path is Factory_V3-relative
  $env:OL_SUPERVISOR_STATE_DIR      = "$env:OL_SUPERVISOR_WORKSPACE_ROOT\Sub_Projects\ol-build\state"
  $env:OL_SUPERVISOR_ORCHESTRATOR   = "$env:OL_SUPERVISOR_WORKSPACE_ROOT\Python_Executions\ralph\orchestrator.sh"
  $env:OL_SUPERVISOR_BASH           = "C:\Program Files\Git\bin\bash.exe"
  $env:OL_SUPERVISOR_CONCURRENCY_CEILING = "1"
  python -m supervisor --once      # single cycle, watched; expect 0 dispatches + clean exit
  ```
- [x] **Service account has `claude` authenticated AND `Factory_V3` trusted** — the account IS `Z2-WINTERHALTER\Winterhalter` (this session's user); `~/.claude` credential present, `hasTrustDialogAccepted` set for `Factory_V3` (2026-07-09), and headless `claude` non-interactive auth **proven by the sv_smoke run**. The service MUST run as this account (**not `SYSTEM`** — SYSTEM lacks the `~/.claude` credential + trust → every run fails `Unknown command: /rl-initiative-planner`). *Residual:* the session-0 service context itself is proven only by the first approved dispatch.
- [x] **`claude` on PATH** — `C:\Users\Winterhalter\.local\bin\claude.exe` is on the **Machine** PATH (service inherits it); Git bash present at `C:\Program Files\Git\bin\bash.exe`.

---

## 1. Environment (the make-or-break parity)

| Var | Value | State | Notes |
|---|---|---|---|
| `PROD_DB_URL` | (prod `eybdbshxswutgaaylpol`) | ✅ set | live registry |
| `SUPABASE_ACCESS_TOKEN` | (PAT) | ✅ set | Executor supabase MCP |
| `PYTHONUNBUFFERED` | `1` | ⚠ **must set** | Python block-buffers stdout as a service → `AppStdout` stays empty until a large flush (observed 2026-07-09 running the supervisor foreground). Without it the log tail is useless mid-cycle. |
| `OL_SUPERVISOR_WORKSPACE_ROOT` | `…\Factory_V3` | ⚠ **must set** | `folder_path` (`Sub_Projects\…`) resolves as `root / folder_path` → root is **Factory_V3**, not Sub_Projects. **Absent → enrichment no-ops → nothing dispatches, silently.** |
| `OL_SUPERVISOR_STATE_DIR` | `…\Factory_V3\Sub_Projects\ol-build\state` | ⚠ **must set** | holds `logs/events.jsonl`, the `KILL_SWITCH` sentinel, attention state |
| `OL_SUPERVISOR_ORCHESTRATOR` | `…\ralph\orchestrator.sh` | ⚠ **must set** | absolute path (the default `orchestrator.sh` only resolves if cwd=ralph; cwd is Factory_V3 here) |
| `OL_SUPERVISOR_BASH` | `C:\Program Files\Git\bin\bash.exe` | ⚠ **must set** | bash used to spawn `orchestrator.sh` |
| `OL_SUPERVISOR_CONCURRENCY_CEILING` | `1` | ⚠ set conservative | one run at a time to start; raise once trusted |
| `OL_SUPERVISOR_MAX_DISPATCHES_PER_CYCLE` | `1` | ⚠ set conservative | ramp one/cycle, not fill-to-ceiling, initially |
| `OL_SUPERVISOR_USAGE_5H_CEILING_USD` | e.g. `20` | recommended | rolling 5h pause-not-kill (proxy dollars) |
| `OL_SUPERVISOR_USAGE_WEEKLY_CEILING_USD` | e.g. `100` | recommended | rolling weekly pause |
| `OL_SUPERVISOR_EMERGENCY_SPEND_CEILING_USD` | **cur cumulative + headroom** | ⚠ **footgun** | this is **cumulative all-time** spend — a value *below* current cumulative trips the kill on the FIRST cycle. Check `read_cumulative_spend_usd` first; set above it. |
| `OL_SUPERVISOR_FORECAST_CEILING_USD` | (optional) | optional | warn-only projected-total escalation |
| `F_GMAIL_SMTP_USER` / `F_GMAIL_SMTP_TO` | your gmail / dest | ⚠ **set for delivery** | `F_GMAIL_SMTP_APP_PASSWORD` is already set; without USER + TO, escalation emails don't deliver (or use the `OL_SUPERVISOR_SMTP_*` overrides). |

*(SMTP: the supervisor reads `OL_SUPERVISOR_SMTP_*` first, then falls back to the inner-loop `F_GMAIL_SMTP_*` — so completing `F_GMAIL_SMTP_USER`/`_TO` reuses the orchestrator's existing gmail app-password with no new config.)*

---

## 2. Install (ELEVATED — admin, or via the CF Elevation Helper)

```powershell
$svc = "CF OL Supervisor"
$py  = "C:\Users\Winterhalter\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$fv3 = "K:\OneDrive - EPM Solutions - Project Server- Project Online\Code_Factory\Factory_V3"
$nssm = "C:\ProgramData\chocolatey\bin\nssm.exe"

& $nssm install $svc $py "-m supervisor --interval 30"
& $nssm set $svc AppDirectory "$fv3\Python_Executions\ralph"        # cwd = RALPH (the supervisor package imports here; import FAILS from Factory_V3, not pip-installed). The spawned orchestrator separately gets cwd = the project dir (spawn.py _confined_cwd) — a non-git dir under trusted Factory_V3, which DOES resolve the rl-* role skills (verified 2026-07-09). So role-calls work even though the supervisor's own cwd is ralph.
& $nssm set $svc ObjectName "Z2-WINTERHALTER\Winterhalter" "<password>"   # account with claude auth + trust — NOT SYSTEM
& $nssm set $svc AppEnvironmentExtra `
    "OL_SUPERVISOR_WORKSPACE_ROOT=$fv3" `
    "OL_SUPERVISOR_STATE_DIR=$fv3\Sub_Projects\ol-build\state" `
    "OL_SUPERVISOR_ORCHESTRATOR=$fv3\Python_Executions\ralph\orchestrator.sh" `
    "OL_SUPERVISOR_BASH=C:\Program Files\Git\bin\bash.exe" `
    "OL_SUPERVISOR_CONCURRENCY_CEILING=1" `
    "OL_SUPERVISOR_MAX_DISPATCHES_PER_CYCLE=1" `
    "OL_SUPERVISOR_USAGE_5H_CEILING_USD=20" `
    "OL_SUPERVISOR_USAGE_WEEKLY_CEILING_USD=100"
    # (PROD_DB_URL / SUPABASE_ACCESS_TOKEN / F_GMAIL_SMTP_APP_PASSWORD inherit from Machine scope;
    #  add OL_SUPERVISOR_EMERGENCY_SPEND_CEILING_USD=<cumulative+headroom> and F_GMAIL_SMTP_USER/_TO here.)
& $nssm set $svc AppStdout "K:\CF-Backup\logs\ol-supervisor.out.log"
& $nssm set $svc AppStderr "K:\CF-Backup\logs\ol-supervisor.err.log"
& $nssm set $svc AppRotateFiles 1
& $nssm set $svc AppRotateBytes 10485760
& $nssm set $svc AppExit Default Restart                            # crash -> restart (FR-013 re-attach reclaims orphans)
& $nssm set $svc AppRestartDelay 10000
& $nssm set $svc AppThrottle 30000
& $nssm set $svc AppStopMethodConsole 15000                        # Ctrl-C -> supervisor finishes its cycle + persists
& $nssm set $svc Start SERVICE_DEMAND_START                         # MANUAL start first; auto-start only once trusted
```

**Via the CF Elevation Helper** (non-elevated session): put the block above into a `K:\CF-Backup\scripts\cf-install-ol-supervisor.ps1`, author a `CF `-named task XML (UTF-16) running it, `request-elevation.ps1 -Action register-task …`, `schtasks /Run`, verify, then `delete-task` + cleanup. (See the `cf-elevation-helper` memory / `K:\CF-Backup\README-elevation-helper.md`.)

---

## 3. First start (watched, manual)

```powershell
nssm start "CF OL Supervisor"
Get-Content "K:\CF-Backup\logs\ol-supervisor.out.log" -Wait   # tail
```
Confirm in the log: `preflight` OK · `re-attach — 0 re-attached, 0 orphaned` · `concurrency ceiling = 1` · **0 dispatches** (fleet gated) · a cycle line every ~30s. Then approve ONE project (control panel) and watch it admit → spawn → the orchestrator drain → reconcile `complete`.

---

## 4. Operate

- **Approve a run:** control-panel Approve (or `set_lifecycle_state <id> candidate`) → next cycle admits + spawns exactly it. *This is the only way anything runs (operator policy 2026-07-09).*
- **Emergency stop (no admin):** create the kill-switch sentinel — `New-Item "$STATE_DIR\KILL_SWITCH"` → the supervisor refuses ALL new dispatch next cycle (running Runs untouched). Delete it to resume.
- **Stop the service:** `nssm stop "CF OL Supervisor"`. **NOTE:** in-flight *detached* orchestrators keep running (they outlive the supervisor by design) — a service stop is **not** a fleet halt.
- **Trust it logged-off:** once you've watched a clean admit→spawn→reconcile and a restart (re-attach) cycle, `nssm set "CF OL Supervisor" Start SERVICE_AUTO_START`.

## 5. Uninstall

```powershell
nssm stop "CF OL Supervisor"; nssm remove "CF OL Supervisor" confirm
```

---

## Open validations / risks (resolve during the watched runs)

1. **Session-0 headless `claude`.** A service runs in session 0. Validate that `claude -p` (and its **auth refresh**) work non-interactively as the service account *before* trusting logged-off operation — if claude auth expires, a headless service cannot re-auth via a browser. This is the biggest unknown; watch the first several real dispatches.
2. **Emergency spend ceiling is cumulative-all-time** (see §1 footgun) — set above current cumulative.
3. **`OL_SUPERVISOR_WORKSPACE_ROOT = Factory_V3`** (not Sub_Projects) — verify a candidate's `folder_path` (`Sub_Projects\…`) resolves to a real dir under it before trusting dispatch (absent/wrong → silent no-dispatch).
4. **Elevation** — install/remove needs admin; the existing CF automations are Task Scheduler, so this is the first NSSM service on the box.

*v1.0 — 2026-07-09. Not executed; prerequisites §0 gate installation.*
