# Supervised First Flight Runbook

| | |
|---|---|
| **Version** | 1.3 |
| **Date** | 2026-07-08 |
| **Status** | PROMOTE → `Sub_Projects\Ralph Loop\design\Supervised_First_Flight_Runbook_v1.3.md`. Container-delivered; operator promotes from `Ralph Loop\New\`. Strip nothing — no staging header to remove. |
| **Purpose** | Convert the operator from **interactive CC babysitting** of Ralph Loop runs (mode "C") to a **watched headless run** (mode "A") by flying ONE deliberately trivial, unambiguously RL-fit initiative end-to-end — intake → approve → drain-to-done — without touching CC during iteration. |
| **Applies once** | This is a *proving* run, not real work. After a clean first flight, real initiatives follow the same path and the supervisor can be daemonized (out of scope here — see §7). |
| **Related** | `Ralph_Loop_User_Guide_v1.0.md`; `RL_Project_Intake_and_Evaluation_Spec_v1.0.md`; `rl-project-intake_v1.0/SKILL.md`; `Initiative_Orchestrator_Spec_v1_9.md`; `RL_OL_Control_Panel_GUI_Design_v1.0`. |

## Changelog

| Version | Date | Notes |
|---------|------|-------|
| 1.3 | 2026-07-08 | **First flight actually FLOWN** (pilot `first_flight_docstub` drained to `INITIATIVE_COMPLETE`: 3 doc-stub items closed over 3 iterations, ~$9.4 on the successful run / ~$14.6 incl. the defect-surfacing run, zero fleet blast radius). Lessons folded in: (1) **NEW §1.2 runtime gate** — the orchestrator's `claude -p` role-calls resolve the `rl-*` role skills ONLY when run from the **`Factory_V3` cwd** (where `.claude/skills` lives) AND that workspace is **trusted** in `~/.claude.json` (else `Unknown command: /rl-initiative-planner`, or permissions silently ignored → Executor writes denied). (2) **NEW §2 rule** — a *custom* session shape MUST require the Executor to write `execution_report_<ITER>.md` with a `## Items closed` section; telling it "create only the artifact, nothing else" makes the Planner forbid the report and the Consumer HALTs `failed_report_missing`. (3) **NEW §5 caution** — starting the fleet supervisor dispatches EVERY admittable `candidate`, not just your pilot; for a scoped first flight, run the orchestrator directly on the pilot seed. Harness gap (report-recovery net was shape-hardcoded) fixed at commit `9596b68` (+`doc_stub`); follow-ups filed FUP-0930 (generalize the net) + FUP-0931 (`initiative_narrative.md` bootstrap owner). P2/P4 verified states updated to "exercised on live data". |
| 1.2 | 2026-07-08 | Corrected two ground-truth errors: (1) the intake insert keys off **`PROD_DB_URL`**, not `OL_SUPERVISOR_DB_URL` (`Registry.from_env()` → `DB_URL_ENV`); (2) Checkpoint 1 dropped `metadata->'proposal'` (no such column on `projects`; proposal lives in the seed file). Standardized `check_kind` to `registry_zero_open`; fixed the Related link; footer version. |
| 1.1 | 2026-07-08 | Grounded §1.1 preflight + §5 env block against the canonical `seed.template_v1_4.md`. Added the seed-schema-1.4 preflight (slug↔projects-row FK, `SUPABASE_ACCESS_TOKEN`, `verification_bindings: []` default, `completion_predicate.params.path` real-filename rule). |
| 1.0 | 2026-07-08 | First draft. Grounded in operator-verified ground truth: supervisor hand-started; approve endpoint wired but never fired; `pending_approval` in the deployed CHECK. |

---

## 0. Why this runbook exists

You built a headless, chat-free, self-resuming autonomous loop — and you operate it by hand, interactively, through CC. Every pain you feel (per-iteration wire, continuation prompts, day-plus latency) is a symptom of driving interactively a system designed to run without you. The fix is a **mode switch**, and this runbook flies it once, on something that doesn't matter, so you can trust it before you rely on it.

**The correct division of labour** (per intake spec D1 + NFR-005):

- **CC, interactive — by design:** the intake Q&A only.
- **GUI, one click:** Approve (`pending_approval → candidate`).
- **Supervisor/orchestrator, headless, no human:** admit → spawn detached orchestrator → drain to done, escalating only true `gate_human`.

The behavioural change is narrow: **stop at the `pending_approval` handoff instead of hand-driving iterations.**

---

## 1. Preconditions (verified 2026-07-08 — re-confirm at run time)

| # | Fact | Verified state | Re-check before flight |
|---|------|----------------|------------------------|
| P1 | `pending_approval` in the live `projects_lifecycle_state_check` | **Landed** (prod `eybdbshxswutgaaylpol`) | `SELECT` the constraint again. |
| P2 | Approve endpoint `POST /api/projects/{id}/approve` → `candidate` | **Wired + exercised on live data (2026-07-08)** — flipped a real pilot pending_approval → candidate; 409 on illegal transition confirmed | Confirm the webui is running (or drive the `set_lifecycle_state` seam) before you need to Approve. |
| P3 | Supervisor `python -m supervisor` | **Runnable, not resident.** Needs `PROD_DB_URL` + `OL_SUPERVISOR_WORKSPACE_ROOT` | Confirm both env vars are set. |
| P4 | `rl-project-intake` scaffold + insert; full orchestrator drain | **Exercised end-to-end (2026-07-08)** — `first_flight_docstub` intake → approve → `INITIATIVE_COMPLETE` | Still the run's first real checkpoint (§3); treat each new initiative's first insert as unproven until you see the row. |

**The one genuine first-flight risk** is P4: the intake insert path (`Registry.from_env().upsert_project`, reading **`PROD_DB_URL`** — the SAME DB the supervisor + control-panel read). A malformed seed or a mis-pointed `PROD_DB_URL` surfaces *here*. The skill's Failure Protocol HALTs loudly rather than faking success. Checkpoint 1 (§3) verifies the row landed clean.

> **Critical:** intake must write to the **same** `PROD_DB_URL` the supervisor (§5) and the webui use, or the Approve card never appears.

### 1.1 Seed-schema-1.4 preflight (from `seed.template_v1_4.md`)

Three baked-in lessons are load-bearing pre-launch gates:

- **Slug ↔ projects-row FK ordering (FUP-0808, HARD).** `initiative.slug` MUST equal a real `projects.project_id` before the orchestrator runs (else Phase-Z `cf-followup-tracker` INSERTs fail `PG-23503`). The intake→approve flow satisfies this by construction.
- **`SUPABASE_ACCESS_TOKEN` (HARD, or supabase MCP dies at iter-0001)** — set it alongside the launch env (§5). For a doc-only pilot with **no** DB writes, you may instead omit the `supabase` entry from `mcp_servers[]` entirely (simpler; the `first_flight_docstub` pilot used `mcp_servers: []`).
- **`verification_bindings: []` + inline verification is the supported trivial-pilot default** — a trivial pilot needs NO custom bindings; the empty-list default routes closure through the inline per-session-plan check (artefact-exists). *Do not* bind `cf-doc-reviewer` on a trivial doc pilot — a non-empty binding with no captured reviewer output stalls closure (P4-07). *(This exact trap bit the first flight's defect-surfacing run.)*

Also eyeball at Checkpoint 1: **`completion_predicate[].params.path` must be a real bare filename** (`registry_zero_open` on your registry file) — never a descriptive sentinel (FUP-0813).

### 1.2 Runtime gate — cwd + workspace trust (HARD; discovered on the first flight)

The orchestrator dispatches each role (`/rl-initiative-planner`, `/rl-iteration-consumer`) as a `claude -p` slash-command. Two environment facts are load-bearing, and BOTH failed on the first launch attempt:

- **Run from the `Factory_V3` cwd, not the `ralph` subdir.** The `rl-*` roles live as skills under `Factory_V3\.claude\skills\`; `claude` discovers them relative to cwd. From `Python_Executions\ralph\` you get `Unknown command: /rl-initiative-planner`. Launch the supervisor/orchestrator with cwd = `Factory_V3` (the orchestrator resolves its own hooks by script path, so this is safe).
- **`Factory_V3` must be a TRUSTED workspace.** If `~/.claude.json` lacks `projects["…/Factory_V3"].hasTrustDialogAccepted: true`, the CLI *ignores* `.claude/settings.json` permissions and the headless Executor's file writes are denied. Trust it once (run `claude` interactively in `Factory_V3` and accept, or set the flag).

Confirm both before igniting: `cd` into `Factory_V3`, and check the trust flag exists for it.

---

## 2. Phase 0 — Pick the pilot (framing is the only human-irreducible step)

The loop automates *iterate-to-goal*. It does **not** automate *decide-the-goal-and-its-verification*. For a first flight the framing must be deliberately trivial.

**Pilot selection criteria — all must hold:**

- **3–4 registry items, no more.**
- **Every closure headlessly verifiable** by an `artefact_exists` predicate or a cf-pytest binding — never "looks right".
- **A mechanical completion predicate** — `registry_zero_open`.
- **Bounded, sandboxed writable paths** — the pilot writes only under its own `Sub_Projects/<slug>/` tree.
- **Nothing you care about.**

> **Custom session shapes: require the execution report (learned on the first flight, HARD).** Whatever shape your pilot uses, the Executor MUST write `execution_report_<ITER>.md` (in the iteration dir) with a top-level `## Items closed` section naming the items closed — that is how the Consumer commits a closure. Do **not** instruct the Executor to "create only the artifact and nothing else": the Planner will then *forbid* the report, and the Consumer HALTs `failed_report_missing`. State the report as a required per-iteration deliverable in the shape. *(A harness recovery net re-asks the Executor for a missing report, but as of 2026-07-08 it only covers a fixed shape list — `component_build|integration_checkpoint|skill_build|doc_stub`; see FUP-0930.)*

**Good pilot shapes:** a 3-item doc-stub drain (each item = "file X exists at path Y"); a tiny skill-scaffold backlog; a 4-row gap register whose closures are all "artefact exists". **Bad pilots:** anything Claude.ai-surface-authored, taste-judged, or touching `Project_Docs_Current\` / prod schema.

---

## 3. Phase 1 — Intake (CC, interactive)

Run `/rl-project-intake` and answer the Q&A. Expected outcome: **RL-FIT**, then the skill scaffolds `Sub_Projects/<slug>/` (seed + work registry) and inserts the projects row at `pending_approval` via `Registry.from_env().upsert_project` (reading `PROD_DB_URL`). The skill then **STOPS**.

> **CHECKPOINT 1 — the insert.** Before touching the GUI:
> ```sql
> SELECT project_id, lifecycle_state, priority, folder_path
> FROM projects WHERE lifecycle_state = 'pending_approval';
> ```
> Confirm: exactly your pilot's row; `pending_approval`; `priority`/`folder_path` as expected.
>
> **Note:** the proposal / rubric verdict / seed_path are **not** on the `projects` row (`upsert_project` writes only project_id/display_name/folder_path/status/lifecycle_state/priority/depends_on; there is no `projects.metadata` column). Verify those in the **scaffolded seed file**. Also confirm the registry has your enumerated item count.
>
> **If absent/malformed:** the insert is owed — usually `PROD_DB_URL` unset or pointed at the wrong DB. Fix the cause, re-run intake; do NOT hand-INSERT, do NOT proceed to approve.

---

## 4. Phase 2 — Approve (GUI, one click)

Open the control panel. The `pending_approval` project shows as an **"Approval needed"** card. Click **Approve** — this fires `POST /api/projects/{id}/approve`, flipping `pending_approval → candidate`.

> **CHECKPOINT 2 — the flip.**
> ```sql
> SELECT project_id, lifecycle_state FROM projects WHERE project_id = '<pilot_slug>';
> ```
> Confirm `candidate`. (A 409 means an illegal transition / unknown project — Checkpoint 1 wasn't actually green.)

Do **not** start the supervisor before this. A `pending_approval` row is invisible to admit/schedule by design.

---

## 5. Phase 3 — Ignite (FOREGROUND, WATCHED — do not touch CC)

> **⚠ Fleet caution (learned on the first flight).** `python -m supervisor` admits and dispatches **EVERY** admittable `candidate` up to the concurrency ceiling — not just your pilot. If other real candidates sit in the registry, they will spawn too (real spend). For a *scoped* first flight, either clear/park the other candidates first, or run the orchestrator **directly on the pilot seed** (this is exactly what the supervisor's spawn does):
> ```
> cd "<…>/Factory_V3"          # REQUIRED — see §1.2 (skill discovery + trust)
> bash Python_Executions/ralph/orchestrator.sh "<abs path to pilot seed>"
> ```

For the full fleet supervisor path, launch from the `Factory_V3` cwd with the env set:

```
cd "<…>/Factory_V3"                  # §1.2: role-calls resolve only here, and it must be trusted
echo $PROD_DB_URL                    # supervisor refuses / no-ops without it
echo $OL_SUPERVISOR_WORKSPACE_ROOT   # required — without it enrichment no-ops and NOTHING dispatches
echo $SUPABASE_ACCESS_TOKEN          # Executor's supabase MCP dies at iter-0001 without it (skip if seed omits supabase)

python -m supervisor --interval 30
```

Run in the **foreground the first time** — watch it reconcile, admit, schedule, spawn. (Daemonize via NSSM only *after* you trust it — §7.)

**State cheat-sheet:**

| You see | It means | Your action |
|---------|----------|-------------|
| `candidate → admitted` | Schedule granted a slot | Watch. |
| `admitted → running` | Orchestrator spawned **detached** | Watch. |
| Iteration `NNNN` dirs under `state/iterations/` | Planner → Executor → Consumer turning; registry rows flip `P1/P2/P3 → RESOLVED` | Watch. |
| `running → paused_gate` | A `gate_human` | Answer async — §5.1. Do NOT open CC. |
| `running → paused_budget` | Iteration/spend cap hit | Raise the cap in the seed and relaunch. |
| `running → paused_safety` / HALT | Read-only violation or hash drift | **Safety proof working.** Inspect; don't override. |
| `running → complete` | `registry_zero_open` passed | Done → §6. |
| `running → failed` | Unrecoverable | Inspect narrative + escalation; stop-on-fail (NFR-004). |

### 5.1 Answering a `gate_human` async (the trust-earning moment)

When the run blocks on `paused_gate`, it notifies you. Resolve it **without an interactive CC session**:

1. Read the gate request (`state/iterations/NNNN/gate_request_*.json` or under `escalations/`).
2. Write a valid `gate_response_<iter>_<gate>.json` (`selected_option` or `custom_text`) into the pending iteration dir (or `escalations/`).
3. Do nothing else. The next ~30s cycle detects the cleared gate and resumes (`paused_gate → running`).

**Watch that resume happen.** You dropped a file; the machine picked the loop back up — no chat, no continuation prompt.

> Ideally engineer the trivial pilot to raise **zero** gates for a clean drain, *then* run a second tiny pilot that deliberately raises one, purely to rehearse the async-answer drill. *(The first flight raised a legitimate gate when it hit a real closure-path defect — the Planner correctly refused to fake a closure and escalated. That refusal is the safety property you most want to confirm.)*

---

## 6. Phase 4 — Debrief

After `complete`, write two lists: **what the loop did alone** (every planner decision, report-read, next-step dispatch, state-carry — the wire you no longer are) and **what genuinely needed you** (framing, the Approve click, any gate answers). If the second list is short, the mode switch is validated.

Then apply the rubric's four hard gates (decomposable / headlessly-verifiable / bounded predicate / durable substrate) to your real recurring work: the passers are your headless backlog; the failures are the manual residue (§6.1) — correct, not a shortfall.

### 6.1 The NOT-FIT residue (what correctly stays manual)

- **Taste-judged "done"** (R2/R3 fail).
- **Claude.ai-surface authoring interleaved with execution** (two-surface discipline; the headless Executor drives CC only).
- **Continuous human steering** (R4 hard-fail).

For these, `/rl-project-intake` returns NOT-FIT and writes an `RL_Improvements_Needed_<slug>` spec — even a rejection feeds the roadmap.

---

## 7. After a clean first flight (pointers only)

- **Daemonize the supervisor** as an NSSM service — only *after* you have watched it run clean once. Remember §1.2: its service cwd must be `Factory_V3` (trusted), or the role-calls won't resolve.
- **Concurrency:** the `uq_ralph_runs_active_per_project` invariant guarantees one active run per project; multiple projects run in parallel. Seed several and let them drain — but see the §5 fleet caution.
- **The Learn loop:** the read-only Run-Auditor proposes auto-resolve rules from repeated gate patterns, shrinking `gate_human` interruptions over time.

---

## 8. One-screen summary

```
FRAME (you, minutes)          →  pick a trivial 3–4 item RL-fit pilot; custom shape MUST require the execution_report
INTAKE (CC, interactive)      →  /rl-project-intake  →  RL-FIT  →  scaffold + pending_approval row (PROD_DB_URL)
  └ CHECKPOINT 1              →  verify the row landed clean
APPROVE (GUI, one click)      →  pending_approval → candidate
  └ CHECKPOINT 2              →  verify the flip
IGNITE (watched)              →  cd Factory_V3 (trusted!) ; run orchestrator on the pilot seed  [scoped — §5 fleet caution]
  └ WATCH                     →  candidate→admitted→running→…→complete
  └ gate_human?               →  drop gate_response_*.json, watch it self-resume  ← the trust moment
DEBRIEF (you)                 →  inventory what ran alone vs. what needed you
```

*End of Supervised First Flight Runbook v1.3.*
