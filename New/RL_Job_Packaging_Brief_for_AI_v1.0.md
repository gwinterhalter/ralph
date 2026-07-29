# Ralph Loop — Job Packaging Brief (for an AI agent)

**Version:** 1.0 · **Date:** 2026-07-09
**Purpose:** Hand this to an AI so it can **package** a candidate body of work into a runnable Ralph
Loop job. "Package" = FIT-screen the candidate, then produce three artifacts (seed + work registry +
a `pending_approval` projects row). **Scaffold, do not start** — nothing you produce runs until the
human operator Approves it.

> This brief distills the `rl-project-intake` skill (v1.0) and the Ralph Loop User Guide (§7.1, §8.2,
> §9.1, §6.4). If any of those live sources are reachable, prefer them — they are authoritative and
> version over time. Do not invent field names, paths, or lifecycle values from memory (§7 Guardrails).

---

## 0. TL;DR

1. **Clarify** the candidate with structured questions until every hard-gate rubric input is known.
2. **Score** it against the 7-criterion RL-Fitness Rubric (§4). Any **hard gate** = NO → **NOT-FIT**.
3. **NOT-FIT** → write an `RL_Improvements_Needed_<slug>.md` + raise a follow-up. Create **no** project.
4. **FIT** → scaffold `Sub_Projects/<slug>/` with a **seed** + **work registry**, insert a `projects`
   row at **`pending_approval`**, then **STOP** and report. Never admit/spawn/run anything.

---

## 1. The skill & authoritative sources

- **Skill:** `rl-project-intake` (v1.0) — `…/Factory_V3/.claude/skills/rl-project-intake_v1.0/`.
- **Process/rubric authority:** `Sub_Projects/ol-build/new/RL_Project_Intake_and_Evaluation_Spec_v1.0.md`.
- **Templates the FIT branch fills:** `references/seed_skeleton.md`, `references/registry_skeleton.md`.
- **Operator guide:** `Python_Executions/ralph/Ralph_Loop_User_Guide_v1.1.md` (§9.1 intake, §8.2 seed).
- **Invocation note:** the `rl-*` skills resolve as a **Skill** only from a cwd where
  `Factory_V3/.claude/skills/` is discoverable (e.g. a `Factory_V3` cwd). From other cwds (e.g. the
  `ralph` git repo), they are **markdown instruction docs you follow manually**, not tool-invocable.

---

## 2. What packaging produces — the triad

| Artifact | Location | Role |
|---|---|---|
| **Seed** (`<slug>_seed.md` or similar) | `Sub_Projects/<slug>/` | The **manifest**: YAML frontmatter with every machine-readable field the orchestrator reads (§5). |
| **Work registry** (`registry.md` / `<Name>_Registry.md`) | `Sub_Projects/<slug>/` | The **backlog**: one row per discrete, independently-closeable item. "Done" = zero open rows. |
| **`projects` DB row** at `pending_approval` | Postgres `projects` table (via `PROD_DB_URL`) | Registers the job in the fleet, parked until the operator Approves. |

The `state/` runtime dir is **not** created at packaging — the orchestrator scaffolds it at first launch.

---

## 3. Procedure

### Step 1 — Clarify (ASK; never score a one-liner)
Gather, via structured questions (one or more rounds), every rubric input. A missing hard-gate input
is a **question, never an assumption**:
- **Goal & deliverable** — what concrete artifact(s) does "done" produce (spec / code / docs / schema)?
- **Decomposability** — can the work be a registry of discrete, independently-closeable items (each id,
  priority, prerequisite)? Roughly how many?
- **Verifiability** — for each item, what objective check confirms closure *without a human* (a test,
  an artefact-exists predicate, a verification binding)?
- **Completion predicate** — what defines the WHOLE initiative as done (zero open items / artefact exists)?
- **Ambiguities** — are open questions discrete (answerable as gates) or continuous (need a human every step)?
- **Substrate** — does it read/write durable files or DB (not ephemeral chat)?
- **Blast radius** — which paths/tools must it write; can that be scoped safely?
- **Budget** — rough per-iteration LLM cost + a total cap.

Re-ask until R1, R2, R3, R5 are unambiguous. If the submitter can't answer a hard-gate question, that
itself is evidence — often **NOT-FIT (insufficient definition)**.

### Step 2 — Score against the RL-Fitness Rubric (§4)
State the verdict + a per-criterion YES/NO with one-line evidence each.

### Step 3a — NOT-FIT branch
Write `Sub_Projects/ol-build/new/RL_Improvements_Needed_<slug>_v1.0.md` naming the **specific** RL
capability gap(s) that would make it fit (e.g. "a non-code artefact verifier binding", "a web-research
session shape", "a human-in-loop review shape", "a longer budget tier"), each tied to the failing
criterion. Raise a `ralph_loop` follow-up pointing at it. **Create no project.** Stop and report.

### Step 3b — FIT branch (scaffold + insert)
1. Derive a `slug` (lowercase_underscores) + `display_name` from the goal; **confirm the slug with the
   operator**. It MUST equal the `projects.project_id`. Collision with an existing id → ask for a new slug.
2. Create `Sub_Projects/<slug>/` with:
   - a **seed** from `seed_skeleton.md`, filled from the answers (§5 fields), and
   - a **work registry** from `registry_skeleton.md`, **one row per decomposed item** (id / priority /
     prerequisite). **Completeness:** N items enumerated ⇒ exactly N rows, or flag the delta and HALT.
3. Insert the `projects` row at **`pending_approval`** (NOT `candidate`). Prefer the registry API over a
   raw INSERT:
   ```
   python -c "import os; from supervisor.registry import Registry; \
     Registry.from_env().upsert_project('<slug>', folder_path=r'<abs folder>', priority=<n>, \
     depends_on=[], lifecycle_state='pending_approval')"
   ```
   `Registry.from_env()` reads **`PROD_DB_URL`** — it must point at the SAME DB the supervisor +
   control-panel read, or the operator's Approve won't see the row. (Use a Supabase branch DSN to avoid
   writing prod, or the prod ref when the operator directs.)
4. **STOP.** Report: the scaffold path, the registry item count, and "awaiting operator Approve in the
   control panel (Home → Approval needed → Approve, which sets `lifecycle_state=candidate`)."

---

## 4. The RL-Fitness Rubric (the gate — not taste)

| # | Criterion (the loop structurally needs it) | Gate |
|---|---|---|
| R1 | Decomposable into a work registry of discrete, independently-closeable items | **hard** |
| R2 | Each item objectively verifiable headlessly (a binding / predicate) | **hard** |
| R3 | A bounded completion predicate (a definable "done") | **hard** |
| R4 | Ambiguities are gate-able (discrete), not continuous human steering | soft |
| R5 | Durable file/DB substrate (not ephemeral chat) | **hard** |
| R6 | Bounded / scopable blast radius (safe autonomous edits) | soft |
| R7 | Per-iteration work is LLM-doable within a budget cap | soft |

**Verdict:** all hard gates YES → **RL-FIT** (or **RL-FIT-WITH-MITIGATIONS** if a soft gate is NO but
mitigable — record the mitigation, e.g. narrowed writable paths or a higher budget tier). Any hard gate
NO → **NOT-FIT**.

Good fits: draining a spec's gap register to zero, a skill-build backlog, a migration-authoring sweep.
Not fits: "make the dashboard nicer", "add auth" — unbounded, taste-driven, or not decomposable.

---

## 5. The seed manifest — key frontmatter fields

The orchestrator reads **only** the YAML frontmatter (canonical on any conflict with the body).

| Field | Purpose |
|---|---|
| `seed_schema_version` | Currently `1.4`. |
| `initiative.slug` / `.title` / `.owner` / `.description` / `.project_id` | Identity. **`slug` MUST equal a seeded `projects.project_id`** (Phase-Z follow-up INSERTs FK on it). |
| `workspace_root` | Absolute root for all writes (`…/Sub_Projects/<slug>`). |
| `state_dir_relative` | Per-initiative runtime dir, e.g. `state/`. |
| `work_registry` | Bare filename of the registry `.md` (scan-newest resolves `<base>_v*.md`). |
| `read_only_paths[]` | Paths the Executor must not write. **MUST include `…/Sub_Projects/Factory_Design/design/Project_Docs_Current`** (see §7 FR-034). |
| `writable_paths[]` | The blast-radius write scope (default: the project folder). |
| `completion_predicate[]` | The done-test(s): `registry_zero_open` / `artefact_exists` / `skill_clean` / `doc_review_clean`; `params.path` must resolve to a real file. |
| `session_shape_catalog[]` | `{name, template_pointer}` shapes the Planner picks from. |
| `verification_bindings` | Per-shape Consumer verification-skill map (`[]` = defer posture; a **missing** key HALTs). |
| `gate_policy.pre_classification[]` / `.confidence_threshold` | Gate-classification DSL + Answerer auto-resolve floor (default 0.7). |
| `budget.{iterations_max, tokens_usd, per_call_usd_cap, max_turns_per_call, hang_timeout_seconds}` | Caps. |
| `mcp_servers[]` | Executor MCP servers as objects `{name, command, args, env}` (bare strings crash the generator). |
| `strict_mcp_config` | `true` (default) → Executor sees only declared servers. |
| `permission_posture` | Executor permission mode, e.g. `--permission-mode auto`. |
| `notification_channel` | e.g. `gmail_smtp:default`, `wintoast:default`, `slack_webhook:…`. |
| Optional | Per-role model overrides `planner_model` / `plan_review_model` (default `sonnet`) / `answerer_model` / `consumer_model` / `executor_model`; `heartbeat.{workstream_id, env_vars}`. |

---

## 6. The work-registry shape

One row per discrete, independently-closeable item. The **Priority cell doubles as the open/closed marker**:

```
| ID | Name | Gap description | Priority | Prerequisites | Resolution path |
```

- `**P1**` / `**P2**` / `**P3**` = **OPEN** at that work-ordering priority (the completion predicate counts these).
- `**RESOLVED**` = **closed** (written only by the Consumer at run time, with an `iterations/NNNN` citation).

(The `rl-project-intake` skeleton may instead use a dedicated `Status` column `OPEN`/`IN_PROGRESS`/
`RESOLVED`; the seed's `completion_predicate[].params` tells the loop which convention to read. Mental
model either way: P1/P2/P3 = open, RESOLVED = closed.)

---

## 7. Hard guardrails (do not violate)

- **Scaffold, do not start.** Insert at `pending_approval` and STOP. Never set `candidate`/`admitted`/
  `running`, never Approve, never spawn or run an orchestrator. (Operator-only; NFR-005 boundary.)
- **FR-034 (admission invariant).** The seed's `read_only_paths[]` **MUST** list `Project_Docs_Current`.
  A seed omitting it is **rejected at admission** (`read_only_invariant_violation`) and silently never
  dispatches. The current skeleton defaults it; a hand-written seed must include it.
- **Anti-confabulation.** Derive slug / folder / priority / seed / registry from the **answers +
  templates + DB reads** — never from memory. Unknown required value ⇒ ask, `STUB` as `NEEDS_REVIEW`, or HALT.
- **Slug integrity.** `slug` = `projects.project_id`; a collision with an existing id → ask for a new slug, never clobber.
- **Completeness reconciliation.** Operator enumerates N items ⇒ registry has exactly N rows, or flag the delta and HALT — never scaffold a subset.
- **DB honesty.** `PROD_DB_URL` unset/unreachable or `upsert_project` errors ⇒ HALT after writing the
  scaffold files and report the owed insert — never fake success. The row must live in the DB the
  supervisor + control-panel read.
- **One-shot work is not a fit.** If the work has no iteration benefit, do it inline / as a script — don't package it.

---

## 8. After packaging — the lifecycle handoff (context, not your job)

```
pending_approval ──(operator Approve)──▶ candidate ──(supervisor admits)──▶ running ──▶ complete
   ^you stop here^
```

Your responsibility **ends** at `pending_approval`. The operator Approves in the control panel
(→ `candidate`); the supervisor's Schedule step reads `candidate` only and then admits + spawns exactly
one detached `orchestrator.sh` for the project. Nothing you packaged runs without that explicit Approve.

---

## 9. Report template (what to hand back after a FIT scaffold)

```
RL job packaged: <slug> (<display_name>)
  Verdict:        RL-FIT [| RL-FIT-WITH-MITIGATIONS: <mitigation>]
  Scaffold:       Sub_Projects/<slug>/  (seed: <file>, registry: <file>)
  Registry items: <N> open
  Projects row:   inserted @ pending_approval in <DB> (PROD_DB_URL)
  Next:           awaiting operator Approve (control panel → Approval needed → Approve)
```

For **NOT-FIT**: report the verdict, the per-criterion evidence, and the `RL_Improvements_Needed_<slug>.md` path.

---

*End of Ralph Loop Job Packaging Brief v1.0. Authoritative sources may have advanced — re-ground against
the `rl-project-intake` skill and the User Guide before relying on any exact field name, path, or command.*
