---
name: cf-ralph-run-tracker
description: >
  Governs every write to the `ralph_runs` Run Registry as its documented
  sole-writer discipline (Ralph Loop Outer Loop Spec v1.3 §5.4-§5.5, FR-009 to
  FR-014, NFR-006/NFR-007), mirroring cf-followup-tracker's DB-write discipline
  for `followups`. Use this skill whenever writing, reviewing, or reconciling a
  `ralph_runs` row — the spawn-row INSERT at orchestrator spawn (FR-009),
  terminal reconciliation on process exit (FR-011/FR-012 exit-code-to-status
  map), re-attach by orchestrator_pid (FR-013), exact-decimal cost
  (FR-014/NFR-007) — or verifying the OLB-02 supervisor/registry.py host methods
  conform. Also triggers on phrases like 'record a Run row', 'reconcile a
  ralph_runs status', 'map the orchestrator exit code', or 'who writes
  ralph_runs'. Always use before touching any ralph_runs write path. Does NOT
  trigger on edits to Ralph_Loop_Outer_Loop_Spec itself — use cf-spec-writer;
  does NOT write `followups` (use cf-followup-tracker); does NOT write the
  `projects` Registry lifecycle state.
---

# Code Factory — Ralph Run Tracker Conventions

The documented sole-writer discipline for the `ralph_runs` Run Registry (Ralph
Loop Outer Loop Spec v1.3 §5.4), the queryable record of every orchestrator Run
— identity, owning Project, lifecycle status, terminal cost. This skill is the
**write authority** the spec names at §5.5: it owns the `ralph_runs` write
contract exactly as cf-followup-tracker owns `followups`. It is a
**governance/discipline** skill, not a runtime executor: the Supervisor runs as
a standalone Python host (NFR-004) with no Claude MCP surface at runtime, so the
OLB-02 `supervisor/registry.py` methods `record_run` (FR-009) and
`update_run_status` (FR-011/FR-012) are the conforming runtime **mechanism**.
The skill is the authority; registry.py is its host-process implementation.

The Run Registry is a **pointer into orchestrator file state, never a duplicate
of in-run truth** (§5.4; Initiative_Orchestrator_Spec §2.2 file-only by design).

---

## Surface

| Surface | Status |
|---|---|
| Claude Code | **Primary** — design/governance authority. Consulted when authoring or reviewing any `ralph_runs` write path (registry.py host methods, or any future writer). |
| Runtime in the Supervisor | **Not invoked.** The standalone Python host (NFR-004) writes `ralph_runs` via `supervisor/registry.py`, which conforms to this discipline. The skill is never called at runtime. |
| Claude Desktop / claude.ai | Supported for review/authoring; no runtime write surface. |

---

## Path Matrix

This skill's canonical authoring home is the git-tracked dev clone (gate `olb03-tracker-skill-form-and-substrate` option A: the governance SKILL.md lives beside the supervisor code it governs). It is staged for operator promotion to the shared skills tree; the Executor never promotes in-place (gate `olb03-skill-promotion` option A).

| Path | Kind | Note |
|---|---|---|
| `<project_root>\.claude\skills\cf-ralph-run-tracker_v{N.M}\` | Operator canonical install (versioned) | The shared skills tree (`CLAUDE_SKILLS_DIR`). The operator promotes the staged copy here after the Mode A audit closes; never written in-place by the Executor (gate_human `olb03-skill-promotion`). |
| `K:\Claude Code Factory\V3\Ralph-dev\supervisor\skills\cf-ralph-run-tracker\` | Git substrate (canonical authoring home) | The committed source, beside `supervisor\registry.py` it governs (gate `olb03-tracker-skill-form-and-substrate` option A); the per-iteration git commit substrate. |
| `<sub-project>\ol-build\new\Skill_Updates\cf-ralph-run-tracker_v{N.M}\` | Promotion-staging copy | Byte-identical staging copy for operator promotion; outside the git tree (lives in Project_Docs). |
| `/mnt/skills/user/cf-ralph-run-tracker/` | Runtime mount | Read-only at runtime on the Claude surface; **never** a canonical write target. |
| `tests\regression_eval.json` | Tests dir (in git substrate + staging copy) | Regression eval set carried with the skill. |

---

## Core Principles

1. **Substrate is the `ralph_runs` table — and only `ralph_runs`.** Every
   write this discipline governs targets `ralph_runs` exclusively. It never
   writes `projects` lifecycle state (that is the Supervisor's Project Registry
   path, `set_lifecycle_state`), never `followups` (cf-followup-tracker), never
   `events` or `workstreams`.
2. **Authority, not runtime writer (NFR-004 + NFR-006).** This skill is the
   §5.5 sole **write authority**; the runtime **mechanism** is
   `supervisor/registry.py`. No orchestrator Run, role skill, or Executor
   session writes a `ralph_runs` row directly (NFR-006). A `running`
   orchestrator writes no Registry row of its own.
3. **Exactly two write points (§5.6).** A `ralph_runs` row is written at
   exactly two moments: **spawn** (FR-009 INSERT) and **terminal
   reconciliation** (FR-011/FR-012 UPDATE). There is **no** per-iteration
   liveness write and **no** per-iteration liveness column. `status` is the
   only lifecycle column and it moves only at these two points.
4. **`status` is the discrete-lifecycle surface; freshness lives elsewhere
   (§5.6).** `ralph_runs.status` (`running | complete | budget_exhausted |
   failed | halted`) is the authoritative *discrete lifecycle* surface,
   event-sourced from observed orchestrator exits. The *continuous
   liveness/staleness* signal is the event stream's last `phase_complete`
   (Comprehensive_Event_Log_Spec §15) — a separate authority. `status` is
   **fed by** that staleness signal on the abnormal-termination path; it is
   never a second freshness store, and the event stream is never a second
   status store.
5. **Money is exact decimal, never float (FR-014/NFR-007).**
   `terminal_cost_usd` is stored as exact NUMERIC preserving cent precision.
   Cost is summed from iteration costs and recorded once, at terminal
   reconciliation. A floating-point value is a defect, not a rounding choice.
6. **Soft Project reference, no hard FK (FR-010).** `project_slug` references
   the owning Project by slug, resolving to exactly one `projects.project_id`,
   **without** a database foreign key. The reference is validated by resolution,
   not enforced by a constraint.
7. **Idempotent at spawn; reconcile-once at exit.** Spawn carries an
   `idempotency_key` so a re-driven spawn does not double-insert. Terminal
   reconciliation targets only the Project's currently-`running` row and is a
   one-way move to a terminal status; a row already terminal is never
   re-reconciled.

---

## When NOT to Use

| Condition | Alternative |
|---|---|
| Writing / transitioning a `projects` lifecycle state row | The Supervisor's Project Registry path (`set_lifecycle_state`, Spec §5.2/§5.3) — same host module, different table and discipline |
| Inserting / updating a `followups` row | `cf-followup-tracker` (sole writer for `followups`) |
| Inserting / updating an `imps` row | `cf-imp-writer` |
| Emitting or reading `events` (the event stream / staleness signal) | The Event-Log emit path (Comprehensive_Event_Log_Spec); the staleness signal is read as input to Reconcile, never written here |
| Adding a per-iteration liveness / heartbeat column to `ralph_runs` | RETIRED by §5.6 — `ralph_runs` carries no freshness column; freshness is the event stream's `phase_complete` |
| Editing `Ralph_Loop_Outer_Loop_Spec` itself | `cf-spec-writer` — this skill enforces the spec's `ralph_runs` contract; it does not author the spec |
| Changing the `RegistryPort` Protocol seam or registry.py write methods | HALT and re-gate — the OLB-01 seam and OLB-02 mechanism are frozen contracts; this discipline conforms to them, it does not rewrite them |

---

## Writes-allowed columns (enumerated, exhaustive)

The deployed `ralph_runs` shape (verified live 2026-06-04; Ralph_Runs_Table_Migration v1.0). Every column a conforming writer touches, and when.

| Column | Direction | Constraint / rule |
|---|---|---|
| `ralph_runs.run_id` | **NEVER** | `uuid` PK, DB default `gen_random_uuid()`; never client-minted |
| `ralph_runs.project_slug` | WRITE on INSERT (spawn) | `text NOT NULL`; the soft Project reference (FR-010) resolving to exactly one `projects.project_id`; no hard FK; never changed after spawn |
| `ralph_runs.seed_path` | WRITE on INSERT (spawn) | `text NOT NULL`; the owning Project's seed path recorded at spawn (FR-009) |
| `ralph_runs.orchestrator_pid` | WRITE on INSERT (spawn) | `integer`; the spawned process id (FR-009); the primary re-attach identity (FR-013), disambiguated by the `metadata` process-start-time |
| `ralph_runs.status` | WRITE on INSERT (defaults `running`) + WRITE on terminal UPDATE | `text NOT NULL`; one of `running \| complete \| budget_exhausted \| failed \| halted` per `ralph_runs_status_check`; moved only at the two §5.6 write points; never an out-of-set value |
| `ralph_runs.idempotency_key` | WRITE on INSERT (spawn) | `text`; spawn idempotency token so a re-driven spawn does not double-insert |
| `ralph_runs.spawned_at` | WRITE on INSERT (spawn) | `timestamptz`; the spawn-confirmation timestamp (FR-009 acceptance: a spawn row has a recorded `spawned_at`) |
| `ralph_runs.terminated_at` | WRITE on terminal UPDATE | `timestamptz`; set once at terminal reconciliation (FR-011); NULL while `running` |
| `ralph_runs.terminal_cost_usd` | WRITE on terminal UPDATE | `numeric`; exact-decimal summed iteration cost (FR-014/NFR-007); never float; set once at terminal reconciliation |
| `ralph_runs.metadata` | WRITE on INSERT (spawn) + MAY append at terminal | `jsonb NOT NULL` default `{}`; carries the FR-013 process-start-time disambiguator and any non-columnar run facts; never a second status or freshness store |
| `ralph_runs.created_at` | **NEVER** | `timestamptz NOT NULL` DB default `now()` |
| `ralph_runs.updated_at` | Writer sets `now()` on the terminal UPDATE only | `timestamptz NOT NULL` DB default `now()`; carries no lifecycle meaning — it is a write-stamp, not a liveness column (§5.6) |
| any other column | **NOT TOUCHED** | If a write needs a column not listed here, HALT — the operation does not belong to this discipline |

---

## Write-point map (the two write points)

Every `ralph_runs` mutation is one of exactly two operations. No third write point exists (§5.6).

### Write point 1 — Spawn row (FR-009): INSERT

| Aspect | Rule |
|---|---|
| Trigger | Admission spawns an orchestrator for a Project and the spawn is confirmed |
| Required fields | `project_slug` (FR-010 soft ref), `seed_path`, `spawned_at` (spawn confirmation), `orchestrator_pid` |
| `status` | Defaults to `running` (DB default); the row is born `running` |
| `metadata` | Carries the FR-013 process-start-time disambiguator for later re-attach |
| Idempotency | `idempotency_key` present so a re-driven spawn does not double-insert |
| Acceptance (FR-009) | A Run row exists referencing that Project with a recorded `spawned_at` |
| Host mechanism | `supervisor/registry.py::record_run(project_id, run)` — writes `project_id` as the `project_slug` soft reference (FR-010) |

### Write point 2 — Terminal reconciliation (FR-011/FR-012): UPDATE

| Aspect | Rule |
|---|---|
| Trigger | The orchestrator process exits |
| Columns set | `status` (terminal), `terminated_at`, `terminal_cost_usd` (summed, exact decimal) |
| Target | Only the Project's currently-`running` Run row (`WHERE project_slug = … AND status = 'running'`) |
| Exit-code → status map (FR-012) | completion → `complete`; budget exhaustion → `budget_exhausted` (distinct from `failed`); HALT → `halted`; crash or failed iteration → `failed` |
| Abnormal path (§5.6 + FR-013) | A `running` Run stale beyond threshold (no clean exit code, `phase_complete` stopped) is driven `running → failed` (process gone, FR-013 re-attach fails) or `running → halted` (process alive but wedged), fed by the event-stream staleness signal |
| Re-attach (FR-013) | On Supervisor restart with a `running` Run, the recorded `orchestrator_pid` **plus** the `metadata` process-start-time disambiguator must confirm the live process before re-attach; otherwise reconcile `failed` |
| Acceptance (FR-011) | After a clean INITIATIVE_COMPLETE exit the row reads `complete`, `terminated_at` is set, and `terminal_cost_usd` reflects the summed iteration cost |
| Host mechanism | `supervisor/registry.py::update_run_status(project_id, status)` — validates `status` against the §5.4 CHECK set before the UPDATE |

---

## Failure Protocol

Halt-and-report is always preferred to silent recovery.

| Situation | Action |
|---|---|
| A write needs a column outside the enumerated Writes-allowed set | HALT; the operation does not belong to this discipline |
| A status value outside `running \| complete \| budget_exhausted \| failed \| halted` | HALT; reject before the write (registry.py raises `ValueError`); never coerce to a near-value |
| A third write point is proposed (per-iteration status / liveness write) | HALT; §5.6 permits only spawn + terminal reconciliation; `ralph_runs` has no liveness column |
| `budget_exhausted` collapsed into `failed` (or vice versa) | HALT; FR-012 keeps them distinct — budget exhaustion is not a crash |
| `terminal_cost_usd` would be stored as a float | HALT; FR-014/NFR-007 mandate exact NUMERIC; fix the type, never round-and-store |
| `project_slug` resolves to zero or more than one `projects.project_id` | HALT; FR-010 requires exactly-one resolution; surface the ambiguity, never guess a Project |
| A hard FK on `project_slug` is proposed | HALT; FR-010 is deliberately a soft reference (OL-3); a hard FK is a design change requiring re-gate |
| A direct `ralph_runs` write by an orchestrator Run, role skill, or Executor session | HALT; NFR-006 sole-writer — only the conforming Supervisor mechanism writes |
| Re-attach without the FR-013 secondary disambiguator (pid only) | HALT; pid reuse is a hazard; require the `metadata` process-start-time match or reconcile `failed` |
| Changing the `RegistryPort` seam or registry.py write-method signatures | HALT and re-gate; the OLB-01/OLB-02 contract is frozen |

---

## Authorities & Anti-Guess

This discipline's emissions trace to named authoritative sources; never write a `ralph_runs` value from memory when the authority is queryable. On any unverifiable value, HALT — never invent a default.

| Emitted item | Authoritative source | Verify rule |
|---|---|---|
| `ralph_runs` column set + types | Live `code_factory.ralph_runs` schema (Ralph_Runs_Table_Migration v1.0); `supervisor/registry.py` `RALPH_RUNS_INSERT_COLUMNS` allowlist | Column names come from the fixed allowlist, never from caller keys; verify against `information_schema.columns` before any schema-dependent claim |
| `status` enum | `ralph_runs_status_check` CHECK + `registry.py` `RUN_STATUSES` frozenset | Live CHECK is authoritative; an out-of-set value is rejected before the write; never substitute |
| Exit-code → status map | Spec v1.3 §5.4 FR-012; Initiative_Orchestrator_Spec §7.2 exit codes | Map per FR-012 exactly (`budget_exhausted` distinct from `failed`); never collapse codes |
| Spawn required fields | Spec v1.3 §5.4 FR-009 | `project_slug` + `seed_path` + `spawned_at` + `orchestrator_pid` are mandatory at spawn; a missing field is a HALT, not a NULL |
| `terminal_cost_usd` value | Summed iteration cost (the Run's own iteration cost records) | Exact NUMERIC (FR-014/NFR-007); recorded once at terminal reconciliation; never a floating-point approximation |
| `project_slug` resolution | Live `projects.project_id` | Must resolve to exactly one Project (FR-010); zero or many → HALT |
| Re-attach identity | Recorded `orchestrator_pid` + `metadata` process-start-time | Both must match the live process (FR-013); pid alone is insufficient |
| Runtime write mechanism | `supervisor/registry.py` (OLB-02) | The skill is the authority; registry.py is the conforming mechanism (NFR-004). Verify the host methods conform; never assert the skill writes at runtime |

---

## Input Preconditions

This is a governance authority, not a source-consuming transform; it has **no external source artefact** it ingests. Its preconditions are the live substrate and the OLB-02 mechanism it governs:

- **`ralph_runs` table absent or schema-drifted from the documented shape** → HALT; verify the live schema (`information_schema.columns`) and the `ralph_runs_status_check` CHECK before governing any write.
- **`supervisor/registry.py` host methods drift from the FR-009/FR-011/FR-012 contract** → surface the drift as a finding (Phase Z followup); never silently bless a non-conforming mechanism.
- **A caller asks this discipline to write `projects`, `followups`, `events`, or any non-`ralph_runs` table** → HALT; route to the owning skill/path per When NOT to Use.
- **A `project_slug` that cannot be resolved to exactly one `projects.project_id`** → HALT (FR-010); never proceed on an unresolved or ambiguous Project reference.

## Completeness Reconciliation

This discipline governs a fixed, enumerable contract — not an open-ended set — so completeness is verified as full contract coverage, not a requested-N/produced-M count:

- **Contract coverage**: every FR-009 through FR-014 obligation, the §5.6 no-liveness-column boundary, NFR-006 sole-writer, and NFR-007 exact-decimal MUST each be encoded in the Writes-allowed and Write-point sections. A missing obligation is a HALT, never a silent omission.
- **Write-point closure**: exactly two write points (spawn + terminal reconciliation). If a proposed change implies a third, HALT (§5.6) — never extend the write surface silently.
- **Column closure**: the Writes-allowed table is exhaustive over the live `ralph_runs` columns. A column present in the live schema but absent from the table is a reconciliation gap → HALT and reconcile before governing writes.

---

## Pre-decided Conventions

| Decision | Answer |
|---|---|
| Skill name | `cf-ralph-run-tracker` |
| Initial version | 1.0 (first build) |
| Surface (primary) | Claude Code (design/governance authority); not invoked at runtime — the standalone Python host writes via registry.py |
| Skill family | governance / sole-writer discipline (mirrors cf-followup-tracker) |
| Substrate authority | Live `code_factory.ralph_runs` (Ralph_Runs_Table_Migration v1.0); Spec v1.3 §5.4-§5.6 |
| Live substrate | Supabase project `code_factory`; table `ralph_runs` |
| Writes-allowed table | `ralph_runs` ONLY |
| Runtime mechanism | `supervisor/registry.py` `record_run` (FR-009) + `update_run_status` (FR-011/FR-012); NFR-004 standalone host |
| Write points | Exactly two — spawn INSERT (FR-009) + terminal reconciliation UPDATE (FR-011/FR-012); no per-iteration write (§5.6) |
| `status` vocabulary | 5 values per `ralph_runs_status_check` (`running / complete / budget_exhausted / failed / halted`) |
| `project_slug` policy | Soft reference (FR-010); resolves to exactly one `projects.project_id`; NO hard FK |
| Cost policy | `terminal_cost_usd` exact NUMERIC (FR-014/NFR-007); never float |
| Liveness column | NONE (§5.6); freshness is the event stream's `phase_complete` (Event-Log §15) |
| Sole-writer | `cf-ralph-run-tracker` is the §5.5 write authority for `ralph_runs`, mirroring cf-followup-tracker for `followups` (NFR-006) |
| Spec-enforcer anti-trigger | Does NOT trigger on edits to `Ralph_Loop_Outer_Loop_Spec` — use cf-spec-writer |

---

## Skill Production / Audit Checklist

Before promoting (operator gate) or after any future bump, confirm:

- [ ] Every write this discipline governs targets `ralph_runs` only (never `projects`, `followups`, `events`, `workstreams`)
- [ ] All six FR-009 through FR-014 obligations encoded in the Writes-allowed + Write-point sections
- [ ] Exactly two write points (spawn INSERT + terminal reconciliation UPDATE); no third / per-iteration write (§5.6)
- [ ] `status` vocabulary matches `ralph_runs_status_check` (`running / complete / budget_exhausted / failed / halted`); `budget_exhausted` distinct from `failed` (FR-012)
- [ ] `project_slug` documented as a soft reference resolving to exactly one `projects.project_id`, NO hard FK (FR-010)
- [ ] `terminal_cost_usd` documented as exact NUMERIC, never float (FR-014/NFR-007)
- [ ] §5.6 no-liveness-column boundary stated; freshness deferred to the event stream's `phase_complete`
- [ ] NFR-006 sole-writer declared; registry.py (OLB-02) named as the conforming runtime mechanism (NFR-004)
- [ ] Spec-enforcer anti-trigger present in the description (`Ralph_Loop_Outer_Loop_Spec` → cf-spec-writer)
- [ ] Authorities & Anti-Guess, Input Preconditions, Completeness Reconciliation sections present
- [ ] `tests/regression_eval.json` present with ≥5 cases incl. ≥1 negative, ≥1 anti-guess, ≥1 precedent-check
- [ ] Path Matrix documents operator-canonical, git-substrate, staging, and runtime-mount paths
- [ ] cf-skill-reviewer Mode A audit run; 0 SEVERE / 0 WARN before promotion

---

## Authorities & Anti-Guess, Eval Baseline

Eval baseline (cf-skill-builder Phase 6): skill-creator `run_loop` is **not available** on this orchestrated Executor surface — the eval-baseline run is SKIPPED this build and logged as a Phase Z followup (`cf-ralph-run-tracker eval baseline pending — run from Claude Code skill-creator surface`), per cf-skill-builder Phase 6 fallback. The authored description is carried as-is.

---

## Phase 0a — Build Precedent Record

This is the structured precedent record the auditor (cf-skill-reviewer SR-PREC-04) substitutes for live `conversation_search` on the Claude Code surface.

### v1.0 (2026-06-04)

**Surface:** Claude Code (orchestrated Executor, ol_build iter-0003). `conversation_search` structurally unavailable → SR-PREC-04 substitution applies; this record is the precedent input.

**Search terms (would-be):** `cf-ralph-run-tracker`, `ralph_runs sole writer`, `Run Registry write authority`, `cf-followup-tracker governance pattern`, `Spec §5.5 tracker skill`.

**Precedent found:** `cf-followup-tracker` (currently v3.5.6) — the canonical "skill is the sole DB-writer for table X" governance pattern. Ralph_Loop_Outer_Loop_Spec v1.3 §5.5 explicitly names the future `cf-ralph-run-tracker` as the sole write authority for `ralph_runs` "exactly as cf-followup-tracker is for `followups`."

**Delta classification:** EQUIVALENT (mirror). The new skill applies the approved cf-followup-tracker sole-writer discipline to a different table (`ralph_runs` vs `followups`) and a different write mechanism (a standalone Python host, registry.py, NFR-004 — vs cf-followup-tracker's own MCP writes). No scope drift from the precedent's discipline; the spec mandates the separate skill. This is **not** a fork (SR-ANTI-03): the two govern disjoint tables and the spec names them as distinct authorities.

**Operator approval marker:** Resolved by gate `olb03-tracker-skill-form-and-substrate` option A (governance SKILL.md, registry.py conforms) and gate `olb03-skill-promotion` option A (stage-only; operator promotes) — both inlined into Session Plan 0003 and consumed at §5.

**Phase 0b — V4 Governance gate:** PASS. **Recurrence:** per-Run — every orchestrator spawn (FR-009) + terminal reconciliation (FR-011/FR-012). **Time saved / leverage:** prevents NFR-006 sole-writer violations and FR-012 exit-code mis-mapping across every Run; high-cost-prevented (a mis-attributed or float-cost Run row corrupts the Run Registry's queryable record). **Why not docs:** the spec mandates a *write-authority* skill, not a reference page; the discipline is consulted at every `ralph_runs` write path. **Closest sibling:** cf-followup-tracker — an update is insufficient because the tables, enums, and write mechanism differ and the spec names a separate authority.

---

## Change History

| Version | Date | Author | Summary |
|---------|------|--------|---------|
| 1.0 | 2026-06-04 | Claude Code | Initial build (ol_build iter-0003, OLB-03). Documents the sole-writer discipline for `ralph_runs` (Spec v1.3 §5.4-§5.5): FR-009 spawn-row, FR-010 soft `project_slug` reference, FR-011 terminal reconciliation, FR-012 exit-code→status map, FR-013 re-attach by `orchestrator_pid` + process-start-time, FR-014/NFR-007 exact-decimal cost, §5.6 no-liveness-column boundary, NFR-006 sole-writer. Mirrors cf-followup-tracker's DB-write discipline for `followups`. The OLB-02 `supervisor/registry.py` host methods (`record_run`/`update_run_status`) are bound as the conforming runtime mechanism (NFR-004); no registry.py rewrite. Authored via cf-skill-builder 9-phase discipline; cf-skill-reviewer Mode A 0 SEVERE / 0 WARN. Phase 6 eval baseline SKIPPED (skill-creator run_loop unavailable on the orchestrated Executor surface) — logged as a Phase Z followup per cf-skill-builder Phase 6 fallback. |
