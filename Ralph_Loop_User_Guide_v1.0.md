# Ralph Loop User Guide

**Version:** 1.0
**Date:** 2026-07-02
**Status:** Base copy (curated). Ground-truth this against the source files listed in §11 before relying on any exact field name, exit code, or path — the loop is under active change (FUP-driven) and specifics drift.

## Change history

| Version | Date | Author | Notes |
|---------|------|--------|-------|
| 1.0 | 2026-07-02 | Ralph Loop maintainers | First curated base copy. Consolidates `Initiative_Orchestrator_Spec_v1_9`, the `orchestrator.sh` + `hooks/` + `lib/` implementation, the `supervisor/` fleet host, the four `rl-*` skills (planner v1.5.1, consumer v1_5, answerer v1_2, project-intake v1.0), `seed.template_v1_4.md`, and the Sub_Projects `CLAUDE.md` v1.36 In-Run Bug Workflow. |

> **Terminology note.** The open-source `README.md`/`CLAUDE.md`/`ralph.sh`/`prompt.md` that also live in `Python_Executions/ralph/` are the upstream Geoffrey Huntley / `snarktank/ralph` PRD-loop project. The CF factory's autonomous outer loop — the subject of this guide — is a **different, heavier system** built on top of that fork: `orchestrator.sh` + `hooks/` + `lib/` + `supervisor/` + the `rl-*` skills, driven by the `Initiative_Orchestrator_Spec`. When this guide says "Ralph Loop" it means the CF system, not `ralph.sh`.

---

## 1. What the Ralph Loop is, and when to use it

The **Ralph Loop** (formally the *Initiative Orchestrator*) is a long-running controller that drives a single seeded **initiative** from its current state to a verifiable, machine-checkable **completion criterion** — with no operator intervention except at the gates the seed classes as needing a human.

It replaces the manual loop the operator used to run by hand: open a Desktop Claude session, draft one session plan, hand it to Claude Code, answer its clarifying questions inline, read the report, then open a *fresh* session for the next plan. That manual loop burns days-to-weeks, leaks fidelity as Desktop chats hit their limits around iteration 10–15, and fatigues the operator on routine decisions. The Ralph Loop dissolves it by storing **nothing in chat**: every unit of reasoning ("Role Call") is a fresh, stateless `claude -p` subprocess whose context is loaded on demand from durable files on disk. Memory lives in the work registry, the narrative log, and the state snapshot — never in a conversation.

**Use the Ralph Loop when a body of work is:**

- **Decomposable** into a *work registry* of discrete, independently-closeable items (one row per item). *(hard requirement)*
- **Objectively verifiable headlessly** — each item's closure can be confirmed by a script/skill without a human eyeballing it. *(hard)*
- **Bounded by a completion predicate** — there is a mechanical "we are done" test (typically "zero open items in the registry"). *(hard)*
- **Backed by durable substrate** — files and/or DB rows, not ephemeral chat state. *(hard)*
- With **gate-able ambiguities**, **bounded blast radius**, and **per-iteration work an LLM can finish inside the budget**. *(soft)*

If the hard gates fail — e.g. the work can't be split into verifiable items, or "done" is a matter of taste — it is **NOT-FIT** and should not be run through the loop. The `rl-project-intake` skill (§9.1) scores this formally before anything is launched.

Canonical example initiatives: draining a spec's gap register to zero (`Auto_Build_Spec`), a skill-build backlog, a migration-authoring sweep. Not a fit: "make the dashboard nicer," "add auth" — unbounded, taste-driven, or not decomposable.

---

## 2. Architecture

Three tiers, from the top down: the **fleet supervisor** (Python), the **per-initiative orchestrator** (bash), and the **role skills + hooks** the orchestrator drives.

```
                       ┌───────────────────────────────────────────────┐
                       │  SUPERVISOR  (python -m supervisor)            │
                       │  one host, one process, a fleet of Projects    │
                       │  Reconcile → Admit → Schedule → Attend →       │
                       │              Guard → Learn   (every ~30s)      │
                       └───────────────┬───────────────────────────────┘
             spawns (detached), tracks pid, reconciles, resumes
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        ▼                              ▼                              ▼
 ┌──────────────┐              ┌──────────────┐              ┌──────────────┐
 │ orchestrator │              │ orchestrator │   …one per   │ orchestrator │
 │ .sh (Proj A) │              │ .sh (Proj B) │  admitted    │ .sh (Proj N) │
 └──────┬───────┘              └──────────────┘  Project      └──────────────┘
        │  per iteration, in order:
        │   stop_check.sh → Planner → plan_review.sh → execute_with_gates.sh → Consumer → budget_check.sh
        ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │  HOOKS (bash)              SKILLS (claude -p / claude --print)           │
 │  stop_check.sh            /rl-initiative-planner   (Planner)             │
 │  plan_review.sh    ⇄      /cf-session-plan-reviewer (Reviewer)          │
 │  execute_with_gates.sh ⇄  /rl-operator-answerer    (Answerer, gate_dc)  │
 │  budget_check.sh          claude --print < plan     (Executor)          │
 │  (lib/: seed.sh, notify.sh, events.sh, heartbeat.sh, command_dispatch)  │
 │                           /rl-iteration-consumer    (Consumer)          │
 └─────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Supervisor (fleet host)

`python -m supervisor` is a single long-running loop that manages a **fleet** of Projects (rows in a Postgres `projects` table, keyed by `project_id`/`project_slug`). It does **not** reason about any initiative's content — it is a *mechanical* supervisor (NFR-005). Each cycle (`SupervisionCycle.run_once()`, default every 30s) runs six steps in fixed order:

1. **Reconcile** — classify every active Run against its live process + on-disk signals (see §6).
2. **Admit** — pass-through; the real admission gate runs atomically inside Schedule's dispatch.
3. **Schedule** — pick and dispatch the next Project(s) up to the concurrency ceiling (see §7).
4. **Attend** — deliver queued operator escalations/notifications.
5. **Guard** — safety gates, cost circuit-breaker, stall bridge.
6. **Learn** — the Run-Auditor pass over completed Runs (read-only, findings-only).

Each admitted Project gets exactly **one** running `orchestrator.sh` process — the "shard." The supervisor spawns it **detached** (`OrchestratorSpawnPort`, `subprocess.Popen([bash, orchestrator.sh, seed])` with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows) so the orchestrator **outlives the supervisor cycle**. It records the child pid + OS start-time into the `ralph_runs` registry row so a supervisor restart can re-attach to (or reap) the still-running child without mistaking a recycled PID.

**Concurrency invariant:** a partial unique index `uq_ralph_runs_active_per_project` guarantees at most one active Run per Project.

### 2.2 Orchestrator (per-initiative controller)

`orchestrator.sh <seed_path>` is the Ralph Loop proper — ~700 lines of bash implementing `Initiative_Orchestrator_Spec §13.1`. It owns exactly one initiative, holds a **single-instance lock** on its state dir (`orchestrator.lock`; refuses to start if a live instance already holds it — prevents concurrent orchestrators racing the same state), and runs the iteration cycle (§4) until one of: `INITIATIVE_COMPLETE`, `BUDGET_EXHAUSTED`, a `gate_human` block (clean exit, awaiting operator), or a HALT.

It can be launched **standalone** by an operator (`bash orchestrator.sh path/to/seed.md`) or **by the supervisor**. Both paths are identical; the supervisor just adds fleet scheduling, reconcile, and resume around it.

### 2.3 Hooks and libs

The orchestrator delegates each phase to a bash hook under `hooks/`, and shared helpers under `lib/`:

| Hook | Role | Exit contract |
|------|------|---------------|
| `stop_check.sh` | Evaluate `completion_predicate[]` at the top of every iteration | 0 = complete; 1 = continue; 2 = budget; ≥3 = HALT (malformed predicate) |
| `plan_review.sh` | Run the `cf-session-plan-reviewer` ⇄ planner `--revise` inner loop (≤5 rounds) | 0 = converged; 1 = non-convergence → gate_human |
| `execute_with_gates.sh` | Pre-execution gate broker + Executor `claude --print` + post-exec gate | 0 = ready for Consumer; 1 = failed / gate_human block; 2 = read-only violation (HALT) |
| `budget_check.sh` | Enforce `budget.iterations_max` + cumulative `budget.tokens_usd` | 0 = within budget; 1 = exhausted |

Libs: `lib/seed.sh` (`read_seed_field` — the single canonical seed accessor; reads YAML frontmatter only, via `yq`), `lib/notify.sh` (`dispatch_notification` — gmail_smtp / wintoast / slack_webhook), `lib/events.sh` (`emit_event` — local-first NDJSON event log + idempotent Supabase sync), `lib/heartbeat.sh` (per-iteration `workstreams`-row UPSERT), `lib/command_dispatch.sh` (operator command channel, §8.4).

---

## 3. The five roles

Four of the five roles are **stateless `claude -p` Role Calls** — one fresh subprocess per invocation, all context loaded from disk. The fifth (Executor) is a `claude --print` subprocess fed a session plan on stdin.

| Role | Delivered by | Spec | Job (one line) |
|------|--------------|------|----------------|
| **Planner** | `/rl-initiative-planner` (v1.5.1) | §5.1 | Decide if the initiative is done; if not, draft the next session plan and enumerate every ambiguity as a gate. |
| **Reviewer** | `/cf-session-plan-reviewer` | §5.1 step 8 / §13.2 | Adversarially review each draft plan; loop ≤5 rounds until 0 BLOCKER / 0 DRIFT or escalate. |
| **Answerer** | `/rl-operator-answerer` (v1_2) | §5.3 | Auto-resolve `gate_dc` (delegated-choice) gates *before* the Executor runs, so the plan is fully specified. |
| **Executor** | `claude --print` subprocess | §5.2 | Run the reviewed plan to clean completion or failure, under the seed's permission posture. |
| **Consumer** | `/rl-iteration-consumer` (v1_5) | §5.4 | Ingest the result, verify each declared closure, and update the registry + narrative + snapshot. **Sole closure writer.** |

Two design principles cut across the roles:

- **"Enumerate, don't ask."** The Executor runs headless, where `AskUserQuestion` is structurally unavailable. So the Planner must surface **every** pre-execution ambiguity up front as a `gate_request` file; the Answerer (or, for human-class gates, the operator) resolves it *before* the Executor starts. The Executor never pauses to ask.
- **Stateless roles, durable state.** No role carries memory between calls. The registry, narrative, snapshot, and logs on disk are the entire memory of the run.

---

## 4. The iteration cycle

One iteration is one full loop turn. The orchestrator's main loop (`orchestrator.sh`) runs these phases in order:

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │  TOP OF LOOP                                                         │
   │  stop_check.sh  ── all completion_predicate[] pass? ── yes ─▶ INITIATIVE_COMPLETE (exit 0)
   │         │ no (exit 1)                                                │
   │         ▼                                                            │
   │  ITER = next 4-digit index; mkdir iterations/NNNN/                   │
   │         ▼                                                            │
   │  ① PLANNER  /rl-initiative-planner  → session_plan_NNNN.md           │
   │             (or gate_request files, or bare INITIATIVE_COMPLETE)     │
   │         ▼                                                            │
   │  ② PLAN_REVIEW  plan_review.sh  (reviewer ⇄ --revise, ≤5 rounds)     │
   │         │ converged                    │ non-convergence → gate_human │
   │         ▼                                                            │
   │  ③ EXECUTE  execute_with_gates.sh                                    │
   │       pre-exec gate broker (gate_dc → Answerer; gate_human → block)  │
   │       → claude --print < plan  → execution_result_NNNN.json + report │
   │       → read-only scan + post-exec permission-denial gate           │
   │         ▼                                                            │
   │  ④ CONSUMER  /rl-iteration-consumer                                  │
   │       gate the result JSON → verify closures → write registry/state  │
   │         ▼                                                            │
   │  P4-07 fail_counts ≥3 guard → budget_check.sh → poll command channel │
   │         └────────────────────── loop ──────────────────────────────┘
   └─────────────────────────────────────────────────────────────────────┘
```

### 4.1 Planner (`/rl-initiative-planner`)

Runs once per iteration (plus zero-or-more `--revise` callbacks). Its nine-step procedure (IOS §5.1):

1. **Completion check** — evaluate every `completion_predicate[]` against the current substrate.
2. **Read work registry** — count open items by priority; build the candidate list.
3. **Select the next target** — by `seed.target_order[]` if declared, else open items by priority within registry order, honouring declared prerequisites. If a registry row declares an explicit `closure_shape`/`closure_path`, that overrides shape inference.
4. **Read discrepancy notes** for the target (prior failed attempts).
5. **Fail-count gate (FR-013)** — if this item's `fail_count` is ≥3, do **not** draft another plan; emit a `gate_human` gate (`gate_request_NNNN_0000.json`) for operator strategy review.
6. **Match target to a session shape** from `seed.session_shape_catalog[]`.
7. **Draft the plan** and emit a `gate_request_NNNN_M.json` for each pre-execution ambiguity.
8. **Emit and exit** (the hook then runs the reviewer inner loop).
9. **Read-only path assertion** before exit.

It emits **exactly one** of three outputs each iteration (mutually exclusive):

- **Path B (normal):** a `session_plan_NNNN.md` **and** zero-or-more `gate_request` files.
- **Escalation:** a `gate_human` `gate_request_NNNN_0000.json` and **no plan** (fail-count ≥3, or a scope-correction violation).
- **Path A (early stop):** the string `INITIATIVE_COMPLETE` on stdout with an evidence summary, and **no plan, no gates** — when every completion predicate already passes.

The `session_plan_NNNN.md` uses fixed CF section IDs: §1 Goal, §2 Inputs, §3 Outputs, §4 Files-not-to-touch, §5 Numbered steps (each with per-step verification), §6 Failure protocol, §7 Skill references, §8 Open items, §9 Changelog, §10 Audit status, §11 (Phase Z) mandatory `cf-followup-tracker` invocation with `project_id=<seed.initiative.slug>`. Top metadata carries `iteration_index`, `shape`, `max_turns`, and `target_item_id`.

**`--revise` mode** (rounds 2–5 of the plan-review loop): re-reads all durable inputs, parses the reviewer's findings, and either rewrites the plan (if ≥1 BLOCKER or DRIFT) or emits `REVISE_NOOP_KEEP_VERDICT` and exits without rewriting (on a clean KEEP verdict — a byte-identical overwrite would obscure convergence).

### 4.2 Plan review (`plan_review.sh`)

For each draft plan, up to 5 rounds: invoke `/cf-session-plan-reviewer` (default on a cheaper model — `plan_review_model`, default `sonnet` — to conserve the Opus weekly allowance), check for convergence, and if not converged invoke `/rl-initiative-planner --revise <findings>` to produce a revised plan in place. **Convergence** = a "Session Plan Review Complete" block with 0 BLOCKER and 0 DRIFT, **or** an explicit reviewer KEEP / "ready for Executor dispatch" verdict (COSMETIC findings never block). On 5-round non-convergence the hook writes an escalation file and exits 1; the orchestrator routes that as a `gate_human` block (it persists a real `gate_request` so the run reconciles as `paused_gate`, not `failed`). The hook retries transient `claude` failures up to 3× so a flaky rate-limit isn't mis-reported as non-convergence.

### 4.3 Execute (`execute_with_gates.sh`)

Three stages:

1. **Pre-execution gate broker (Path A, two-pass).** For each `gate_request_NNNN_M.json`: validate against schema, **classify** per `seed.gate_policy.pre_classification[]` (§5, §10.2 DSL). Then:
   - `gate_dc` → resolve via `/rl-operator-answerer` (the answer is inlined for the Executor). If the Answerer's response is missing/malformed or self-escalates, it is **demoted to `gate_human`**.
   - `gate_human` → deferred to pass 2. If a valid `gate_response_*.json` already exists (async operator answer, or a resume), inline it and proceed; otherwise copy the request to `escalations/`, write `pending_gate` to the snapshot, dispatch a notification, and **exit 1 (block)**.
   - `auto_resolve` (schema 1.4) → the broker writes the response directly per the matched pre-classification entry, skipping both Answerer and operator (audited to `logs/auto_resolve.log`).
   It also generates `mcp_config.json` from `seed.mcp_servers[]` (+ any per-iteration additions) and validates it against schema.
2. **Executor.** `claude --print --output-format json --permission-mode <posture> --strict-mcp-config --mcp-config mcp_config.json --max-budget-usd <cap> --max-turns <n> < session_plan_NNNN.md`. Writes `execution_result_NNNN.json` (atomically, to a temp name the agent can't target) and the Executor authors `execution_report_NNNN.md`.
3. **Post-execution gates.** (a) **Read-only scan** — for each `read_only_paths[]` root, any file modified since iteration start is a **boundary violation → exit 2 → orchestrator HALT** (FR-017). (b) **Permission-denial gate (§10.5)** — each `permission_denials[]` entry is classified: a nested `claude -p`/`--print` spawn (a benign *verification* spawn) vs a `deliverable_blocking` denial. Exit 1 **only** if ≥1 deliverable-blocking denial; all-verification-spawn denials are logged and the run continues so the Consumer can still ingest completed deliverables. (c) If `terminal_reason != "completed"` → exit 1 (`irregular_termination`). A completed build/checkpoint that is missing its `## Items closed` report section triggers one bounded `--resume` follow-up to have the Executor emit the report (no code/git change requested).

### 4.4 Consumer (`/rl-iteration-consumer`)

The **closure authority** — the only role that verifies evidence and commits registry closures. Its eight-phase Role Call:

1. Load six inputs: `execution_result_NNNN.json`, `execution_report_NNNN.md`, `initiative_narrative.md`, the work registry (+ content hash), `seed.verification_bindings`, `session_plan_NNNN.md`.
2. **JSON gate (FR-019, 4 checks):** JSON well-formed with required fields; `terminal_reason == "completed"`; `permission_denials[]` empty. Failure → iteration FAILED (`irregular_termination` or `auto_mode_denial`), **no closures committed**, escalate.
3. Parse the report's "Items closed" section.
4. **Verify each declared closure** against `seed.verification_bindings[plan.shape]` — four aspects: artefact exists at the declared evidence path; the named audit-skill's captured output is clean; version bump where the shape requires; changelog entry where required. It **reads** the binding skill's captured output and re-checks the substrate itself — it never re-runs the skill and never infers a close from the report's claim (*no-infer-from-result*).
5. **Commit or leave open** (see §5).
6. Append the iteration summary to the narrative (`total_cost_usd`, `session_id`, denial summary, closures-committed count).
7. Update `state_snapshot.json` (cumulative `tokens_usd_consumed`, last iteration index, last classification).
8. Log to `role_call_log.jsonl` and exit.

---

## 5. Work registry & the gap-closure model

The **work registry** is the seed-named markdown file that is the single source of truth for "what's left." It has **one row per discrete, independently-closeable work item.** The loop drives one open item per iteration; the completion predicate watches for zero open.

### 5.1 Register shape

Live gap-register tables (the shape `stop_check.sh` parses) use columns:

```
| ID | Name | Gap description | Priority | Prerequisites | Resolution path |
```

The **Priority cell** doubles as the open/closed marker:

- **`**P1**` / `**P2**` / `**P3**`** — the item is **OPEN** at that work-ordering priority. `stop_check.sh`'s `zero_open_gaps` evaluator counts these; any nonzero count means "not done."
- **`**RESOLVED**`** — the item is **closed.** The Consumer writes this, together with a Resolution-path citation back to the closing iteration (`iterations/NNNN`), on which the `every_closure_cites_iteration` predicate keys.

> **Reconciling two conventions.** The `rl-project-intake` skeleton generates a richer register with a dedicated **Status** column (`OPEN` / `IN_PROGRESS` / `RESOLVED`) alongside Priority; the generic `registry_zero_open` evaluator can key on that Status column via `params.filter: "status != closed"`. The **live** Auto_Build-style registers instead fold status into the Priority cell (P1/P2/P3 vs RESOLVED). Both are supported; the seed's `completion_predicate[].params` tells `stop_check.sh` which to read. Mental model either way: **P1/P2/P3 = open, RESOLVED = closed.**

### 5.2 The single-writer invariant

**The Consumer is the sole closure writer of the registry.** The Planner only *reads* the registry to pick targets; the Answerer never touches it; the Executor is barred from it via the read-only/plan discipline. Closures are committed **exclusively** in the Consumer's Phase 5. To protect this, the Consumer content-hashes the registry when it reads it (Phase 1) and re-checks the hash before it writes (Phase 5) — any drift means someone edited the registry mid-run, which is a defect → **HALT + escalate** (never a silent merge). The orchestrator independently refreshes `work_registry_hash_at_snapshot` after each iteration so the §6.3 resume protection stays live.

### 5.3 Verified vs unverified (the gap-closure decision)

- **Verified** (all binding aspects pass) → write the registry closure `{item_id, status: closed, closing_iteration, closing_evidence_paths[], closure_timestamp}` — the Priority cell flips to `**RESOLVED**`.
- **Unverified** → leave the row **untouched** (stays open at its P1/P2/P3 priority), append a five-field **discrepancy note** `{item_id, plan_approach, failure_reason, evidence_path, iteration_index}` to the narrative, and **increment the item's fail count** (FR-012).
- **Empty binding** (`verification_bindings[shape]` is `[]`) → the audit-clean aspect is N/A; the item commits on the other aspects alone (a *defer* posture, not a failure). A **missing** bindings key (vs empty) is instead a Phase-1 HALT.

### 5.4 fail_counts and the ≥3 escalation

The **canonical** fail-count store is the `fail_counts[]` tail block in `initiative_narrative.md` (`{item_id, count, last_failure_iteration, last_reason}`), read by the Planner every iteration. The Consumer also writes a **synchronized projection** to `<state_dir>/fail_counts.json`, read by the orchestrator's deterministic **P4-07 guard**. The two writes are an atomic pair — if the projection write fails, the Consumer HALTs (staleness risk). The Consumer only *increments*; escalation is owned by two independent guards:

- The **Planner** (FR-013): before drafting a corrective plan for an item at count ≥3, it escalates `gate_human` instead.
- The **orchestrator** P4-07 guard: after the Consumer, if any item's count ≥3 it writes a `fail_counts_threshold` escalation and HALTs (exit 3).

This bounds retry churn: a deterministically-failing item cannot loop forever — it surfaces to the operator after three attempts.

---

## 6. State & gates

### 6.1 The `state/` directory (IOS §6.1)

Everything the run knows lives under `<seed.workspace_root>/<seed.state_dir_relative>/` (convention: `Sub_Projects\<sub-project>\state\`, a permitted write exception per CLAUDE.md v1.26):

```
state/
├── seed.md                     # the seed, copied once at bootstrap, never modified
├── initiative_narrative.md     # append-only log + fail_counts[] tail block
├── state_snapshot.json         # resumability (see §6.2)
├── spend.json                  # running cumulative LLM spend
├── fail_counts.json            # Consumer-written projection (orchestrator P4-07 reads it)
├── orchestrator.lock           # single-instance guard (holder pid)
├── iterations/
│   └── NNNN/
│       ├── session_plan_NNNN.md
│       ├── planner.json / planner.stdout / planner.stderr
│       ├── gate_request_NNNN_M.json  / gate_response_NNNN_M.json   (flat, v1.5)
│       ├── mcp_config.json
│       ├── execution_result_NNNN.json
│       ├── execution_report_NNNN.md
│       ├── consumer.json          # orchestrator-owned; Consumer must NOT write this
│       └── role_call_log.jsonl
├── escalations/                # gate_request copies, gate_escalation_*.md, failed-iteration records
├── gates/                      # (legacy) state-level gate dir
├── commands/                   # operator command channel (pause / bump_budget / query)
└── logs/                       # orchestrator.log, events.jsonl, auto_resolve.log,
                                # heartbeat.log, notifications.log, verification_spawn_denials.log
```

### 6.2 State snapshot & resumability (IOS §6.2 / §6.3)

`state_snapshot.json` carries `current_iteration_index`, `work_registry_hash_at_snapshot` (sha256), `last_role_call_summary`, and `pending_gate` (null, or `{iteration, gate_request, written_at}`). On startup the orchestrator:

1. **No snapshot** → bootstrap: create the dir scaffolding, copy the seed to `state/seed.md`.
2. **Snapshot present** → verify `work_registry_hash_at_snapshot` against the current registry. **Mismatch → HALT** (registry was edited outside the orchestrator).
3. **`pending_gate` non-null** → look in the pending iteration for a matching `gate_response_*.json`. Present → re-run `execute_with_gates.sh` for that iteration (the operator's answer is now inlined), then the Consumer; clear `pending_gate`. Absent → re-dispatch the notification and block again.
4. Else → continue the main loop from the next iteration.

This is what makes a `gate_human` block **recoverable**: the orchestrator exits cleanly (exit 0), the operator writes a `gate_response`, and the next launch resumes from the snapshot.

### 6.3 Gate classes

| Class | Meaning | Resolution |
|-------|---------|------------|
| **`gate_dc`** (delegated-choice; the default) | A decision the Answerer may make autonomously | `/rl-operator-answerer` resolves it pre-execution if confidence ≥ `confidence_threshold` (default 0.7) **and** it is reversible + in-scope. |
| **`gate_human`** | A decision reserved for the operator | Orchestrator writes an escalation + `pending_gate`, notifies, and **blocks with no timeout** (operator may take days). Resolved by the operator writing a `gate_response`, then relaunch. |

**Classification** is driven by `seed.gate_policy.pre_classification[]` — a first-match-wins DSL evaluated in priority order: `gate_id:<id>` > `cluster:<name>` > `contains:<substring>` (case-insensitive). No match → default `gate_dc`. The **Answerer self-demotes** a `gate_dc` to `gate_human` when: it pre-classes human, confidence is below threshold, the action is **irreversible** (promotion across a read-only boundary, a schema-migration apply, an IMP retirement…), it is **out of scope**, or the gate/response is malformed. Three consecutive sub-threshold confidences in one iteration escalate the whole iteration.

**The `gate_response` JSON (FR-008, 4 fields):** `selected_option` **XOR** `custom_text`; a non-empty `reasoning`; a `confidence` float; and `classification_check`. It must be RFC-8259-valid — paths in string fields use **forward slashes** (a lone Windows `\` is an invalid JSON escape); `execute_with_gates` treats a malformed response as *unresolved* and blocks the gate to the operator.

### 6.4 Read-only boundaries (FR-017)

`seed.read_only_paths[]` (always including the universally-read-only `Project_Docs_Current` corpus snapshot) are scanned after the Executor runs; any write under them is a **terminal HALT (exit 3)**. This is the hard guarantee that a run cannot corrupt the shared corpus.

---

## 7. Admission & scheduling (supervisor)

A Project's journey is a nine-state lifecycle machine (`transitions.py`, FR-001), with only legal transitions enforced at the DB write boundary:

```
pending_approval → candidate → admitted → running → { paused_gate | paused_budget | paused_safety | complete | failed }
     (operator Approve)  (gate)   (spawn)      │            (paused_* → running = resume)
                                               └── complete/failed → candidate  (operator re-open)
```

**Admission** (`admission.py`, the only path `candidate → running`) evaluates preconditions in short-circuit order and produces exactly one outcome (never partial):

0. **Dependency hold** — a `depends_on` prerequisite not yet `complete` → left `candidate`, retried.
1. **Seed validity** — a SEVERE seed-validator finding → **reject** (`seed_invalid`).
2. **Non-empty registry** — no open work item → reject (`empty_registry`).
3. **Slug collision** — `initiative.slug` equals a running `project_id` → reject (`slug_collision`).
4. **Blast radius** — no writable scope derivable from the seed → reject (`unresolvable_blast_radius`); else provision it.
5. **Safety floor** — read-only-corpus / kill-switch / concurrency-ceiling checks. A ceiling-exceeded refusal becomes an `admitted` **hold** (spawns nothing this cycle); other refusals reject.

The registry row is written **before** the spawn, so a spawn failure always leaves a reconcilable row.

**Scheduling** (`scheduler.py`, pure/DB-free) selects the next dispatch from runnable Projects:

- **Starvation guard first** (FR-025) — a Project skipped ≥5 rounds is promoted.
- **Priority** (FR-023) — highest `priority` wins.
- **Closest-to-done** tie-break (FR-024) — fewest open work items.

`run_schedule_fill_step()` repeats the single dispatch up to `max_dispatches_per_cycle` so a cold fleet ramps to the ceiling in one cycle. Governors (all opt-in via env unless noted):

- **Concurrency ceiling** — `OL_SUPERVISOR_CONCURRENCY_CEILING` (default `DEFAULT_CONCURRENCY_CEILING`); the live running-count is the authoritative floor. One Max account has sustained ≥12 concurrent heavy runs, so the ceiling — not the API — is the governor.
- **Usage-window pacing** (pause-not-kill) — `OL_SUPERVISOR_USAGE_5H_CEILING_USD`, `OL_SUPERVISOR_USAGE_WEEKLY_CEILING_USD`: a rolling-window breach **pauses new dispatch** (running Runs continue) and raises one escalation.
- **Kill-switch** (FR-036) — a `<state_dir>/KILL_SWITCH` sentinel file refuses **all** new dispatch this cycle.
- **Emergency spend backstop** (hard) — `OL_SUPERVISOR_EMERGENCY_SPEND_CEILING_USD`: on breach, engages the kill-switch.
- **Cost forecast guard** (warn-only) — `OL_SUPERVISOR_FORECAST_CEILING_USD`: warns when projected fleet total breaches.

---

## 8. How to run it

### 8.1 Prerequisites

- `bash` (Git Bash on Windows), `jq`, `yq` (≥4) on PATH.
- Claude Code CLI authenticated (`claude -p` / `claude --print`).
- `CLAUDE_SKILLS_DIR` pointing at the tree **containing** `.claude/skills/` (the `rl-*` and `cf-*` skills live in a sibling tree, passed to every role via `--add-dir`). Default: `K:/Claude Code Factory/V3/Project_Docs`.
- For the supervisor only: `PROD_DB_URL` (Postgres registry) and `OL_SUPERVISOR_WORKSPACE_ROOT` (**required** — without it, candidate enrichment no-ops and *nothing dispatches*), plus `OL_SUPERVISOR_ORCHESTRATOR` (path to `orchestrator.sh`).
- `SUPABASE_ACCESS_TOKEN` exported if the seed's MCP config includes a supabase server.

### 8.2 Write the seed

Copy `Sub_Projects\Ralph Loop\design\seed.template_v1_4.md`, substitute every `<placeholder>`, and work the §2 substitution checklist. A seed is a single markdown file: **YAML frontmatter** (all machine-readable fields — the orchestrator reads *only* the frontmatter, canonical on any conflict) followed by a documentation body. Key fields:

| Field | Purpose |
|-------|---------|
| `seed_schema_version` | Currently `1.4`. Orchestrator refuses a major it doesn't support. |
| `initiative.slug` / `.title` / `.owner` / `.description` | Identity. **`slug` MUST match a seeded `projects.project_id`** or Phase-Z follow-up INSERTs fail on the FK. |
| `workspace_root` | Absolute root for all writes. |
| `state_dir_relative` | Per-initiative runtime dir (`Sub_Projects\<sub-project>\state\`). |
| `work_registry` | Bare filename of the registry `.md` (scan-newest resolves `<base>_v*.md`). |
| `read_only_paths[]` | Paths the Executor must not write (always includes `Project_Docs_Current`). |
| `context_documents[]` | Docs loaded into the Planner/Answerer prompts every Role Call. |
| `target_order[]` | Optional ordered cluster identifiers the Planner walks. |
| `session_shape_catalog[]` | `{name, template_pointer}` shapes the Planner picks from. |
| `verification_bindings` | Per-shape Consumer verification-skill map (`[]` = defer posture). |
| `completion_predicate[]` | The done-test(s): `registry_zero_open` / `artefact_exists` / `skill_clean` / `doc_review_clean`. `params.path` must resolve to a real file. |
| `gate_policy.pre_classification[]` | Gate classification DSL (`{pattern, class, auto_resolve?}`). |
| `gate_policy.confidence_threshold` | Answerer auto-resolve floor (default 0.7). |
| `budget.{iterations_max, tokens_usd, hang_timeout_seconds, per_call_usd_cap, max_turns_per_call}` | Caps. |
| `mcp_servers[]` | Executor MCP servers as objects `{name, command, args, env}` (bare strings crash the config generator). |
| `strict_mcp_config` | `true` (default) → Executor sees only declared servers, not the operator's `~/.claude.json`. |
| `permission_posture` | Executor permission mode, e.g. `--permission-mode auto`. |
| `notification_channel` | e.g. `gmail_smtp:default`, `wintoast:default`, `slack_webhook:…`. |

Optional (later-schema) fields: per-role model overrides `planner_model` / `consumer_model` / `answerer_model` / `executor_model` (schema 1.2), and a `heartbeat.{workstream_id, env_vars}` block (schema 1.3) for per-iteration `workstreams`-row updates.

> **Two hook-supported fields not yet in `seed.template_v1_4.md`:** `plan_review_model` (read by `plan_review.sh`, default `sonnet`) and `checkpoint_permission_posture` (read by `execute_with_gates.sh` to relax posture for `integration_checkpoint` shapes only). They are honoured if present; the template just hasn't caught up. Add them explicitly if you need them.

### 8.3 Launch

**Standalone (single initiative):**

```bash
bash "K:/.../Python_Executions/ralph/orchestrator.sh" "K:/path/to/your/seed.md"
```

Bootstrap copies the seed into `state/seed.md`, scaffolds the dirs, and runs until `INITIATIVE_COMPLETE`, a `gate_human` block, `BUDGET_EXHAUSTED`, or a HALT. Relaunching the same command **resumes** from the snapshot. To restart clean, `rm -rf <state_dir>` first (see §9 on when that's safe).

**Fleet (supervisor):**

```bash
export PROD_DB_URL=…            OL_SUPERVISOR_WORKSPACE_ROOT="K:/.../Sub_Projects"
export OL_SUPERVISOR_ORCHESTRATOR="K:/.../Python_Executions/ralph/orchestrator.sh"
python -m supervisor                 # continuous; --once for a single cycle; --interval N; --max-cycles N
```

The supervisor preflights the DB schema, re-attaches to any live orchestrators, then admits + spawns Projects up to the ceiling every cycle.

### 8.4 Operate a running run

The **control panel** talks to the fleet and to individual shards:

```bash
python -m supervisor.control_panel status              # live fleet dashboard
python -m supervisor.control_panel metrics [--fleet]   # event/cost summary
python -m supervisor.control_panel pause               # queue a pause command for a shard
python -m supervisor.control_panel bump <new_cap_usd>  # raise a run's budget cap
python -m supervisor.control_panel query               # ask a shard for its register state
python -m supervisor.control_panel learnings|promote|reject|apply <key>   # Run-Auditor findings
```

`pause` / `bump_budget` / `query_register_state` are written as JSON files into `<state_dir>/commands/` and consumed **asynchronously** by the running orchestrator at its next iteration boundary (`command_dispatch`). An operator `bump` never *lowers* the seed cap; a pause is honoured even on a failing iteration. These are per-shard; the fleet-wide `KILL_SWITCH` sentinel and env governors (§7) are separate.

**Answering a `gate_human`:** when a run blocks, it emails/notifies with the gate. Write a valid `gate_response_<iter>_<gate>.json` (§6.3) into the pending iteration dir (or `escalations/`), then relaunch the orchestrator (standalone) — or let the supervisor's next cycle detect the cleared gate and resume (`paused_gate → running`).

---

## 9. Failure & recovery

### 9.1 Fitness screening (before you ever launch)

`/rl-project-intake` scores a candidate against the seven-check **RL-Fitness Rubric** (decomposable / verifiable / bounded predicate / gate-able / durable substrate / bounded blast radius / LLM-doable per iteration; the first three-plus-substrate are hard gates). If it fails a hard gate it writes an `RL_Improvements_Needed_<slug>.md`, raises a follow-up, and creates **no** project. If it passes it derives the slug, writes the seed + registry, and inserts the `projects` row at `pending_approval` — then **stops**. It never admits, spawns, or runs anything; the operator approves in the control panel and the supervisor takes over.

### 9.2 In-run failure modes (how the loop handles them)

The default is **stop-on-fail** (NFR-004): no silent recovery, no auto-retry except where explicitly stated.

| Failure | Handling |
|---------|----------|
| Planner can't draft (3 attempts) | Escalate `gate_human`. |
| Plan-review non-convergence (5 rounds) | Escalation file + `gate_human` block (reconciles `paused_gate`). |
| Per-item fail_count ≥3 | Planner escalates `gate_human`; orchestrator P4-07 guard HALTs (exit 3). |
| Executor crash / non-zero exit | Iteration FAILED + escalate; **no** auto-retry. |
| Executor hang (`hang_timeout_seconds`) | Reconciled as a stall → `paused_gate`. |
| Write to a read-only path | **HALT (exit 3)** — terminal (FR-017). |
| `terminal_reason != completed` | FAILED `irregular_termination` + escalate. |
| `permission_denials` (deliverable-blocking) | `auto_mode_denial` escalation with 3 operator options (revise plan / one-iteration fallback posture / suspend). |
| `permission_denials` (all verification-spawn) | Logged + continue (Consumer still runs). |
| Budget / iteration cap exceeded | `BUDGET_EXHAUSTED` (exit 2). |
| Spend-limit / quota 429 | Surfaced as a `paused_gate` (not churned into repeated paid 429s), clean exit awaiting budget. |
| Registry hash drift mid-run | **HALT** — a defect (only the orchestrator writes during a run). |
| Process killed mid-iteration | Resume from the last snapshot on relaunch. |

### 9.3 The In-Run Bug Workflow ("Orch 4-step process")

When *you* find a bug in the loop machinery or a plan during a live run, do **not** perform in-flight surgery — mid-iteration edits corrupt commits, closures, and state. The four-step recovery (CLAUDE.md v1.36 §Ralph Loop In-Run Bug Workflow; IOS §12.1):

1. **Let the orchestrator finish, or kill it if obviously stuck.** No mid-iteration code edits, no `git commit` during a live run, no patched re-dispatch with in-flight state.
2. **Capture the bug as a FUP, fix it, commit + push.** Use `cf-followup-tracker` for the FUP INSERT; standard `cf-git` discipline for the fix commit + push. **Fix the underlying *code*, never an in-flight test workaround** — a SKIP/env-guard or tweaked assertion bypasses the bug and false-PASSes on the next environment. (Iterative in-flight test edits + declaring PARTIAL via SKIPs is the anti-pattern.)
3. **Re-run the orchestrator** against either **(a)** the *same* substrate with the `state/` directory cleared — the default for logic-only bugs that emitted no closures/rows (**reversible**, sandboxed) — or **(b)** a *fresh* substrate, when the prior run committed bad closures or `followups`/`imps` rows that must be rolled back first (**potentially irreversible** — an operator-decision gate).
4. **The re-run IS the validation pass.** No separate test pass; iteration N's clean emission validates the fix landed.

**Scope (v1.35):** this applies to *any* plan-execution error — a test fail, a runtime probe fail, a SQL constraint fire, any step's done-when criterion failing — not just orchestrator dispatch.

**Sibling-class inline-fix (v1.35):** if a bug surfacing mid-run is (a) clearly the *same defect class* as something the current plan already targets, (b) mechanically fixable with the same patch shape, and (c) reversible (git-tracked) — fix it **inline**; do not over-conservatively HALT-and-defer on strict in/out-of-scope grounds. INSERT the FUP first, commit + push, re-run against fresh/cleared substrate. Only HALT-and-defer if the bug is architecturally novel, irreversible, or genuinely a different system. (Example: `ralph/hooks/*` sharing a defect with an in-scope `ralph/orchestrator.sh` qualifies for inline-fix.)

---

## 10. The Learn loop (supervisor, read-only)

Every supervisor cycle's Learn step runs the **Run-Auditor** over completed Runs (read-only, findings-only): it looks for Answerer gates that were escalated to `gate_human` yet resolved identically across ≥3 runs (candidate `auto_resolve`/DSL rules), verification bindings that uniformly pass (over-verification) or fail (binding defect), session shapes that keep needing reviewer revision, and work-items that repeatedly re-enter the correction loop. Findings carry a `routes_to` adoption route and are surfaced to the operator as one-confirm offers (deduped so each is raised once). The operator triages them via the control panel (`learnings` / `promote` / `reject` / `apply`); `apply` dispatches an accepted finding to its authoring skill. A separate effect-measurement pass later checks whether an *applied* learning actually helped, and flags it if it shows no effect or a regression. The auditor itself **writes nothing** to substrate — it only proposes.

---

## 11. Key files & locations

**Loop engine — `…\Factory_V3\Python_Executions\ralph\`**

| Path | What it is |
|------|-----------|
| `orchestrator.sh` | The per-initiative controller (main loop, resumability, cost wrapper, gate/fail-count guards). |
| `hooks/stop_check.sh` | Completion-predicate evaluator. |
| `hooks/plan_review.sh` | Reviewer ⇄ `--revise` inner loop (≤5 rounds). |
| `hooks/execute_with_gates.sh` | Gate broker + Executor + read-only/permission-denial post-gates. |
| `hooks/budget_check.sh` | Iteration + cumulative-spend caps. |
| `lib/seed.sh` | `read_seed_field` — the canonical seed accessor (frontmatter only). |
| `lib/notify.sh`, `lib/events.sh`, `lib/heartbeat.sh`, `lib/command_dispatch.sh` | Notifications, event log, heartbeats, operator command channel. |
| `schemas/*.schema.json` | JSON schemas for `mcp_config`, `gate_request`, `execution_result`. |
| `supervisor/` | The fleet host (`__main__.py`, `cycle.py`, `admission.py`, `scheduler.py`, `reconcile.py`, `spawn.py`, `registry.py`, `control_panel.py`, `preflight.py`, `run_signals.py`, `run_auditor.py`, `transitions.py`, …). |

**Skills — `…\Factory_V3\.claude\skills\`** (resolved via `CLAUDE_SKILLS_DIR`; scan-newest for the version dir)

| Skill | Role |
|-------|------|
| `rl-initiative-planner_v1.5.1` | Planner (§5.1). |
| `rl-iteration-consumer_v1_5` | Consumer / closure authority (§5.4). |
| `rl-operator-answerer_v1_2` | Answerer for `gate_dc` (§5.3). |
| `rl-project-intake_v1.0` | Pre-loop fitness scoring + seed/registry generation. |
| `cf-session-plan-reviewer_v1.20` | Reviewer (invoked by `plan_review.sh`). |

**Specs & templates — `…\Factory_V3\Sub_Projects\Ralph Loop\design\`**

| Path | What it is |
|------|-----------|
| `Initiative_Orchestrator_Spec_v1_9.md` | The authoritative spec (roles, lifecycle, gates, failure modes, §5.4 Consumer, §8 seed schema). |
| `seed.template_v1_4.md` | The seed template operators fill in. |
| `OLD/` | Superseded spec revisions (v1.0–v1.8). |

**Cross-cutting governance — `…\Factory_V3\Sub_Projects\CLAUDE.md`** (v1.36): the §Ralph Loop In-Run Bug Workflow (the 4-step recovery, scope, fix-code-not-test, sibling-class inline-fix), the `state\` write exception, and the canonical-seed pointer.

---

*End of Ralph Loop User Guide v1.0.*
