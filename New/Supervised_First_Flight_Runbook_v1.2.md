# Supervised First Flight Runbook

| | |
|---|---|
| **Version** | 1.2 |
| **Date** | 2026-07-08 |
| **Status** | PROMOTE → `Sub_Projects\Ralph Loop\design\Supervised_First_Flight_Runbook_v1.2.md`. Container-delivered from a browser-surface session (filesystem MCP absent); operator promotes from `Ralph Loop\New\`. Strip nothing — no staging header to remove. |
| **Purpose** | Convert the operator from **interactive CC babysitting** of Ralph Loop runs (mode "C") to a **watched headless run** (mode "A") by flying ONE deliberately trivial, unambiguously RL-fit initiative end-to-end — intake → approve → supervisor-drains-to-done — without touching CC during iteration. This run also exercises the two never-yet-fired operational paths (the `pending_approval` insert and the approve endpoint on live data). |
| **Applies once** | This is a *proving* run, not real work. After a clean first flight, real initiatives follow the same path and the supervisor can be daemonized (out of scope here — see §7). |
| **Related** | `Ralph_Loop_User_Guide_v1.0.md`; `RL_Project_Intake_and_Evaluation_Spec_v1.0.md`; `rl-project-intake_v1.0/SKILL.md`; `Initiative_Orchestrator_Spec_v1_9.md`; `RL_OL_Control_Panel_GUI_Design_v1.0`. |

## Changelog

| Version | Date | Notes |
|---------|------|-------|
| 1.2 | 2026-07-08 | **Corrected two ground-truth errors that would have stranded the operator at the two never-fired checkpoints.** (1) **Env var:** the intake insert keys off **`PROD_DB_URL`**, not `OL_SUPERVISOR_DB_URL` — `Registry.from_env()` reads `DB_URL_ENV = "PROD_DB_URL"` (`supervisor/registry.py`), and the row must live in the SAME DB the supervisor + control-panel read. Fixed §1 P4 + §3. (Root cause: the `rl-project-intake_v1.0` SKILL carried the same stale var; patched in the same pass.) (2) **Checkpoint 1 SQL:** removed `metadata->'proposal'` — the live `projects` table has **no `metadata` column** (`metadata` is on `ralph_runs`), and `upsert_project` writes no proposal; the proposal/rubric-verdict/seed_path live in the scaffolded **seed file**, verified there instead. Also: standardized the completion `check_kind` to the canonical **`registry_zero_open`** (per `seed.example.md` enum; `zero_open_gaps` is not a real value); corrected the Related link filename; clarified the cycle-step list; fixed the footer version stamp. |
| 1.1 | 2026-07-08 | Grounded §1.1 preflight + §5 env block against the canonical `seed.template_v1_4.md` (schema 1.4, read via M365). ADD §1.1 seed-schema preflight: slug↔projects-row FK ordering (FUP-0808), `SUPABASE_ACCESS_TOKEN` machine-scope requirement (else supabase MCP dies at iter-0001), `verification_bindings: []` + `inline_per_session_plan` as the *supported* trivial-pilot default (no custom bindings needed), and the `completion_predicate.params.path` real-filename rule (FUP-0813). ADD `SUPABASE_ACCESS_TOKEN` to the §5 launch env checks. No structural/section changes; additive only. |
| 1.0 | 2026-07-08 | First draft. Grounded in operator-verified ground truth (2026-07-08): supervisor is a hand-started foreground process (not resident); approve endpoint wired end-to-end but never fired on live data; `pending_approval` in the deployed `projects_lifecycle_state_check`; live rows complete:21 / candidate:15 / failed:3 / pending_approval:0. |

---

## 0. Why this runbook exists

You built a headless, chat-free, self-resuming autonomous loop — and you operate it by hand, interactively, through CC. Every pain you feel (per-iteration wire, continuation prompts, day-plus latency) is a symptom of driving interactively a system designed to run without you. The loop stores **nothing in chat**; interactive operation reintroduces the chat that the design deliberately removed, and that chat is what exhausts around iteration 10–15 and forces continuation prompts.

The fix is not code and not design. It is a **mode switch**, and it is available today: all three preconditions are verified green (migration landed, approve endpoint wired, supervisor runnable). This runbook flies that switch once, on something that doesn't matter, so you can trust it before you rely on it.

**The correct division of labour** (per intake spec D1 + NFR-005):

- **CC, interactive — by design:** the intake Q&A only. This is where an interactive LLM with `AskUserQuestion` lives. You keep using CC *here*.
- **GUI, one click:** Approve (`pending_approval → candidate`).
- **Supervisor, headless, no human:** admit → spawn detached orchestrator → drain to done, escalating only true `gate_human`.

The behavioural change is narrow: **stop at the `pending_approval` handoff instead of continuing to hand-drive iterations.** You are not abandoning CC — you are learning where to let go of it.

---

## 1. Preconditions (verified 2026-07-08 — re-confirm at run time per [I2])

| # | Fact | Verified state | Re-check before flight |
|---|------|----------------|------------------------|
| P1 | `pending_approval` in the live `projects_lifecycle_state_check` | **Landed** (prod `eybdbshxswutgaaylpol`) | `SELECT` the constraint again — it is the single fact everything downstream rests on. |
| P2 | Approve endpoint `POST /api/projects/{id}/approve` → `candidate` | **Wired** front-to-back; **exercised once on live data via a marked test row (2026-07-08), then cleaned up** | Confirm the webui is running and reachable before you need to click Approve. |
| P3 | Supervisor `python -m supervisor` | **Runnable, not resident.** Foreground process; needs `PROD_DB_URL` + `OL_SUPERVISOR_WORKSPACE_ROOT` | Confirm both env vars are set in the shell you will launch from. |
| P4 | `rl-project-intake` skill scaffold + insert (`upsert_project(... lifecycle_state='pending_approval')`) | **Implemented; never fired via the *skill* against prod** | This is the run's first real checkpoint (§3). Treat the first intake insert as unproven until you see the row. |

**The one genuine first-flight risk** is P4: the intake skill's insert path (`Registry.from_env().upsert_project` via `supervisor.registry`) has never produced a live `pending_approval` row *through the skill*. `Registry.from_env()` reads **`PROD_DB_URL`** (`DB_URL_ENV` in `supervisor/registry.py`) — the SAME DB the supervisor and control-panel read. A malformed seed, a missing scaffold field, or an unset/mis-pointed `PROD_DB_URL` surfaces *here* and nowhere earlier. The skill's own Failure Protocol HALTs and reports "the DB insert is owed" rather than faking success — so a failure is loud, not silent. Checkpoint 1 (§3) verifies the row landed clean before you go further.

> **Critical:** intake must write to the **same** `PROD_DB_URL` the supervisor (§5) and the webui use. If the intake shell points `PROD_DB_URL` at a different DB (e.g. a Supabase branch), the row lands where the control-panel can't see it and the Approve card (§4) never appears.

### 1.1 Seed-schema-1.4 preflight (from `seed.template_v1_4.md`, the canonical seed shape)

The intake skill scaffolds a seed from the canonical `seed.template_v1_4.md` (schema 1.4, `Sub_Projects\Ralph Loop\design\`). Three of its baked-in lessons are load-bearing pre-launch gates — confirm each for the pilot:

- **Slug ↔ projects-row FK ordering (FUP-0808, HARD).** `initiative.slug` in the seed MUST equal a real `projects.project_id` row *before* the orchestrator runs, or the Executor's Phase-Z `cf-followup-tracker` INSERTs fail `PG-23503` on `fk_followups_project`. The intake→approve flow satisfies this by construction (the `pending_approval` row *is* that projects row, and it exists before you launch the supervisor) — but if you ever hand-seed outside intake, this is the trap. For the first flight: confirm the seed's `slug` string is byte-identical to the `project_id` you saw at Checkpoint 1.
- **`SUPABASE_ACCESS_TOKEN` env var (HARD, or supabase MCP dies at iter-0001).** The Executor's supabase MCP reads the PAT from the launching shell's `SUPABASE_ACCESS_TOKEN` (machine-scope recommended). If unset, the run fails on its first iteration. Set it alongside `PROD_DB_URL` / `OL_SUPERVISOR_WORKSPACE_ROOT` (§5). *(For a doc-only pilot with no DB writes, you may instead omit the whole `supabase` entry from `mcp_servers[]` — simpler, and removes this dependency entirely.)*
- **Verification default is `[]` + `inline_per_session_plan` — this is correct, not a gap.** A trivial pilot needs NO custom `verification_bindings`; the empty-list default means the inline `cf-doc-reviewer \fix2` baked into each session plan handles closure verification (defer-by-empty-bindings, Track B / FUP-0756). Do not hand-author bindings for the first flight — the default is the supported path and lowers your framing burden.

One more, quieter than the above but worth a glance: **`completion_predicate[].params.path` must be a real bare filename** the orchestrator can scan-newest (`registry_zero_open` on your registry file) — NEVER a descriptive sentinel like "register closure entries" (FUP-0813), which silently blocks `INITIATIVE_COMPLETE` detection. Intake fills this from your answers, but eyeball it at Checkpoint 1: the predicate's `path` should be your actual registry filename.

---

## 2. Phase 0 — Pick the pilot (framing is the only human-irreducible step)

The loop automates *iterate-to-goal*. It does **not** automate *decide-the-goal-and-its-verification*. That framing is yours, and for a first flight it must be deliberately trivial.

**Pilot selection criteria — all must hold:**

- **3–4 registry items, no more.** Small enough that a full drain is minutes of watching, and blast radius is nil if it misbehaves.
- **Every closure headlessly verifiable** by an `artefact_exists` predicate or a cf-pytest binding — never "looks right". If any item needs your eye to confirm, pick a different pilot.
- **A mechanical completion predicate** — "zero open items in the registry" (the `registry_zero_open` `check_kind`).
- **Bounded, sandboxed writable paths** — the pilot writes only under its own `Sub_Projects/<slug>/` tree; `read_only_paths[]` covers everything else (a read-only violation is a HALT, which you *want* as a safety proof).
- **Nothing you care about.** If a bad outcome would cost you real work, it is the wrong pilot.

**Good pilot shapes:** a 3-item doc-stub-creation drain (each item = "file X exists at path Y"); a tiny skill-scaffold backlog; a 4-row gap register whose closures are all "artefact exists". **Bad pilots:** anything spec-authoring on the Claude.ai surface (two-surface boundary — the headless Executor can't do Claude.ai authoring), anything taste-judged, anything touching `Project_Docs_Current\` or prod schema.

> If you cannot name a trivial RL-fit pilot in five minutes, that itself is worth noticing — it may mean your real work skews toward the NOT-FIT residue (§6), in which case the *rubric*, not this runbook, is the tool you need first.

---

## 3. Phase 1 — Intake (CC, interactive — as you already do)

Run `/rl-project-intake` in a CC session and answer the Q&A. Expect questions on: goal/deliverable, decomposability (item count), per-item headless verifiability, completion predicate, gate-ability of ambiguities, durable substrate, blast radius, budget. Answer for the *trivial pilot* — the hard gates (R1 decomposable / R2 verifiable / R3 bounded predicate / R5 durable substrate) should all be trivially YES by construction.

Expected outcome: **RL-FIT**, then the skill scaffolds `Sub_Projects/<slug>/` (seed + work registry) and inserts the projects row at `pending_approval` via `Registry.from_env().upsert_project` (reading `PROD_DB_URL`). The skill then **STOPS** — it never admits, spawns, or runs anything.

> **CHECKPOINT 1 — the never-exercised insert.** Before touching the GUI, verify the row landed clean (this is the P4 risk):
> ```sql
> SELECT project_id, lifecycle_state, priority, folder_path
> FROM projects WHERE lifecycle_state = 'pending_approval';
> ```
> Confirm: exactly your pilot's row; `lifecycle_state = 'pending_approval'`; `priority` as answered; and `folder_path` pointing at the scaffolded `Sub_Projects/<slug>/` folder.
>
> **Note:** the proposal / rubric verdict / `seed_path` are **not** stored on the `projects` row — `upsert_project` writes only `project_id, display_name, folder_path, status, lifecycle_state, priority, depends_on` (there is no `projects.metadata` column). Verify those proposal details in the **scaffolded seed file** under `Sub_Projects/<slug>/` instead. Also confirm the registry file has exactly the item count you enumerated (the skill's Completeness Reconciliation should have HALTed on a delta — verify it didn't silently scaffold a subset).
>
> **If the row is absent or malformed:** the intake insert is owed. This is the expected place for a first-flight problem to appear. The usual cause is `PROD_DB_URL` unset or pointed at the wrong DB (the row must be in the same DB the supervisor + webui read). Fix the cause, re-run intake — do NOT hand-INSERT a row to paper over it, and do NOT proceed to approve.

---

## 4. Phase 2 — Approve (GUI, one click — the first *operator-initiated* row through the endpoint)

Open the control panel. The `pending_approval` project shows as an **"Approval needed"** card (Home → Approval needed) plus a Fleet row with the `pending_approval` badge. Click **Approve** — this fires `POST /api/projects/{id}/approve`, flipping `lifecycle_state` `pending_approval → candidate`.

> **CHECKPOINT 2 — the approve endpoint on live data.**
> ```sql
> SELECT project_id, lifecycle_state FROM projects WHERE project_id = '<pilot_slug>';
> ```
> Confirm `lifecycle_state = 'candidate'`. (A 409 from the endpoint means an illegal transition or unknown project — the row wasn't at `pending_approval`, i.e. Checkpoint 1 wasn't actually green.)

Do **not** start the supervisor before this. A `pending_approval` row is invisible to admit/schedule by design; only `candidate` is admittable.

---

## 5. Phase 3 — Ignite the supervisor (FOREGROUND, WATCHED — do not touch CC from here)

Launch in a terminal you keep visible, with both env vars set:

```
# confirm the environment first — all three must be non-empty
echo $PROD_DB_URL                    # supervisor refuses / no-ops without it
echo $OL_SUPERVISOR_WORKSPACE_ROOT   # supervisor refuses / no-ops without it
echo $SUPABASE_ACCESS_TOKEN          # Executor's supabase MCP dies at iter-0001 without it
                                     #   (skip only if the pilot's seed omits the supabase MCP entry)

python -m supervisor --interval 30
```

Run it in the **foreground the first time** — you want to watch it reconcile, admit, schedule, and spawn in real time. (Daemonizing via NSSM is the step *after* you trust it — §7. Never daemonize something you have never watched run.)

**This is the watch-don't-drive section.** Read each state transition; do not reach for CC to drive it. The supervisor cycles every ~30s through five steps — Reconcile → Schedule (admission happens inside Schedule) → Attend → Guard → Learn.

**State cheat-sheet — what each transition means (so you read, not drive):**

| You see | It means | Your action |
|---------|----------|-------------|
| `candidate → admitted` | Schedule picked your pilot; concurrency slot granted | Watch. |
| `admitted → running` | Supervisor spawned `orchestrator.sh` **detached** (outlives the supervisor cycle) | Watch. |
| Iteration `NNNN` dirs appearing under `state/iterations/` | Planner → Reviewer → Executor → Consumer turning | Watch. Registry rows flip `P1/P2/P3 → RESOLVED` as the Consumer verifies closures. |
| `running` (steady) | The loop is draining the registry, one item per iteration | Watch. This is the whole point — it is doing your old manual loop for you. |
| `running → paused_gate` | A `gate_human` — an ambiguity the Answerer wouldn't auto-resolve (irreversible / low-confidence / out-of-scope) | Answer it async — §5.1. Do NOT open CC to intervene. |
| `running → paused_budget` | Iteration or spend cap hit | Expected only if your budget was tiny; raise the cap in the seed and relaunch. |
| `running → paused_safety` / HALT | A read-only-path violation or registry-hash drift | **This is a safety proof working.** Stop, inspect — do not override. For a trivial pilot this should not fire; if it does, the pilot wasn't as bounded as thought. |
| `running → complete` | `registry_zero_open` predicate passed — the initiative drained | Done. Go to §6. |
| `running → failed` | An unrecoverable failure (executor crash, irregular termination) | Inspect the narrative + escalation; this is stop-on-fail (NFR-004), not silent recovery. |

### 5.1 Answering a `gate_human` async (the trust-earning moment)

When the run blocks on `paused_gate`, it notifies you (email / wintoast / slack per the seed) with the gate. Resolve it **without opening an interactive CC session**:

1. Read the gate request in the pending iteration dir (`state/iterations/NNNN/gate_request_NNNN_M.json` or under `escalations/`).
2. Write a valid `gate_response_<iter>_<gate>.json` (§6.3 of the orchestrator spec — `selected_option` or `custom_text`) into the pending iteration dir (or `escalations/`).
3. Do nothing else. The supervisor's next ~30s cycle detects the cleared gate and resumes (`paused_gate → running`).

**Watch that resume happen.** This is the moment you prove to yourself the loop self-resumes from disk without you driving it — the single most important thing a first flight demonstrates. You dropped a file; the machine picked the loop back up. No chat, no continuation prompt, no re-hydration.

> For the trivial pilot, ideally engineer it to raise **zero** `gate_human` gates (fully specified, no ambiguity) so the first flight is a clean drain — *then* run a second tiny pilot that deliberately raises one gate, purely to rehearse the async-answer-and-resume drill in isolation.

---

## 6. Phase 4 — Debrief (the inventory that tells you what's really RL-fit)

After `complete`, write down two lists:

1. **What the loop did alone** that you would previously have hand-driven in CC — every planner decision, every report-read, every next-step dispatch, every state-carry. This is the wire you no longer are.
2. **What genuinely needed you** — the framing (Phase 0), the one Approve click, any `gate_human` answers. If that list is short, the mode switch is validated.

Then apply the lesson to your *real* work: for each recurring multi-step job you currently babysit, ask the rubric's four hard gates (decomposable / headlessly-verifiable / bounded predicate / durable substrate). The ones that pass are your headless backlog. The ones that fail are the manual residue (§6.1) — and that is correct, not a shortfall.

### 6.1 The NOT-FIT residue (what correctly stays manual)

Some work legitimately cannot go headless, and intake will fail it on a hard gate rather than let you launch a doomed run:

- **Taste-judged "done"** (R2/R3 fail) — success needs your eye.
- **Claude.ai-surface authoring interleaved with execution** — the two-surface discipline means the headless Executor drives CC only; spec/design authoring on the Claude.ai surface can't run inside the loop. (Playwright-driving Claude.ai was already rejected.)
- **Continuous human steering** (R4 hard-fail in practice) — a human needed at every step, not just at discrete gates.

For these, `/rl-project-intake` returns NOT-FIT and writes an `RL_Improvements_Needed_<slug>` spec naming the *specific* capability gap — so even a rejection feeds the roadmap. Do not force these through the loop; keep them interactive.

---

## 7. After a clean first flight (out of scope here — pointers only)

- **Daemonize the supervisor** (Gap A's real close): register `python -m supervisor` as an NSSM service (your stack already uses NSSM) so it is resident, not hand-started. Do this only *after* you have watched it run clean at least once.
- **Concurrency:** the supervisor manages a *fleet* — the concurrency invariant (`uq_ralph_runs_active_per_project`) guarantees one active run per project, but multiple projects run in parallel. Once trusted, seed several initiatives and let them drain concurrently while you do other work.
- **The Learn loop:** the supervisor's read-only Run-Auditor proposes DSL/auto-resolve rules from repeated gate patterns — over time this shrinks the `gate_human` interruptions further. Triage its findings in the control panel.

---

## 8. One-screen summary

```
FRAME (you, minutes)          →  pick a trivial 3–4 item RL-fit pilot
INTAKE (CC, interactive)      →  /rl-project-intake  →  RL-FIT  →  scaffold + pending_approval row (PROD_DB_URL)
  └ CHECKPOINT 1              →  verify the row landed clean (never-fired insert)
APPROVE (GUI, one click)      →  pending_approval → candidate
  └ CHECKPOINT 2              →  verify the flip
IGNITE (supervisor, watched)  →  python -m supervisor --interval 30   [foreground, do NOT touch CC]
  └ WATCH                     →  candidate→admitted→running→…→complete
  └ gate_human?               →  drop gate_response_*.json, watch it self-resume  ← the trust moment
DEBRIEF (you)                 →  inventory what ran alone vs. what needed you
```

*End of Supervised First Flight Runbook v1.2.*
