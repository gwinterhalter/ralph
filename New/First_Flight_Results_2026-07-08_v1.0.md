# Supervised First Flight — Results Report

| | |
|---|---|
| **Pilot** | `first_flight_docstub` (trivial 3-item doc-stub drain) |
| **Date flown** | 2026-07-08 |
| **Runbook** | `Supervised_First_Flight_Runbook_v1.3.md` (this run produced its v1.3 lessons) |
| **Outcome** | ✅ **SUCCESS** — drained to `INITIATIVE_COMPLETE` (after two environment fixes + one real defect fix) |
| **Blast radius** | Nil — all writes sandboxed to `Sub_Projects/first_flight_docstub/`; the other 15 fleet candidates were never dispatched; nothing promoted |
| **Total spend** | ~$14.6 (defect-surfacing run $5.21 + successful run $9.39) |

---

## 1. Result (ground-truth verified)

| Check | Evidence |
|---|---|
| Completion signal | `INITIATIVE_COMPLETE: all completion_predicate[] passed` — `state/logs/orchestrator.log` |
| All items closed | Registry FFD-01/02/03 all `**RESOLVED**`; closure log rows cite `iterations/0001..0003` |
| Real deliverables | `artifacts/01_overview.md`, `02_glossary.md`, `03_checklist.md` (complete, self-consistent docs) |
| Per-iteration reports | `state/iterations/0001..0003/execution_report_000N.md` (Executor-authored, each with `## Items closed`) |
| Registry drained | `registry_zero_open` predicate passed → INITIATIVE_COMPLETE |
| DB reconciled | prod `projects.lifecycle_state = complete`; fleet complete-count 21 → 22; candidates 15 → 15 (untouched) |
| Iterations | 3 (one item per iteration), ~13 min each |

## 2. Phase-4 debrief — division of labour

**What the loop did with zero human hand-driving (the wire you no longer are):**
- 3 Planner decisions — read registry/seed/narrative, selected the next OPEN item, authored a session plan.
- 3 Executor builds — wrote 3 genuinely complete stub docs (the glossary correctly documents the pilot's own machinery).
- 3 Consumer closures — verified each artefact, wrote the execution report, flipped `P1/P2/P3 → RESOLVED`, updated the narrative + closure log.
- Completion detection + clean exit.
- **One correct gate escalation** — when the closure path was structurally broken, the Planner *diagnosed it and stopped* rather than faking a close. That refusal-to-fake is the single most important safety property demonstrated.

**What genuinely needed a human:** the framing (pilot choice), the approve decision, and — because a real defect surfaced — one fix call. Short list ⇒ the mode switch is validated.

## 3. What it took to get there (not a first-try clean pass)

| Blocker | Root cause | Fix |
|---|---|---|
| `Unknown command: /rl-initiative-planner` | role skills discovered only from the `Factory_V3` cwd | run orchestrator from `Factory_V3` (runbook v1.3 §1.2) |
| `.claude` permissions ignored → Executor writes denied | `Factory_V3` not a trusted workspace | set `hasTrustDialogAccepted: true` in `~/.claude.json` (backup saved) |
| Consumer HALT `failed_report_missing` (iter 0001) | my `doc_stub` shape said "create only the artifact, nothing else" → Planner forbade the required `execution_report`; harness recovery net didn't cover `doc_stub` | shape now requires the report (v1.3 §2); `verification_bindings.doc_stub: []`; seeded `initiative_narrative.md`; harness net extended to `doc_stub` (commit `9596b68`) |

## 4. Defects found (tracked)

- **FUP-0930** — `execute_with_gates` report-recovery net is shape-hardcoded; generalize it (partially closed for `doc_stub` at `9596b68`).
- **FUP-0931** — `initiative_narrative.md` is a required Planner input that no role bootstraps; assign an owner.

## 5. Raw evidence (on disk)

- Orchestrator log: `Sub_Projects/first_flight_docstub/state/logs/orchestrator.log`
- Per-iteration Executor reports: `state/iterations/000{1,2,3}/execution_report_000N.md`
- Narrative (per-iteration summaries + fail_counts): `state/initiative_narrative.md`
- Registry + closure log: `Sub_Projects/first_flight_docstub/first_flight_docstub_Registry.md`
- Deliverables: `Sub_Projects/first_flight_docstub/artifacts/*.md`

## 6. Verdict

The autonomous path — intake → approve → headless drain-to-done — **works**. The run also proved the loop refuses to commit a false closure and escalates instead. Recommended next step: a second trivial pilot engineered to raise a `gate_human` on purpose, to rehearse the async answer-and-resume drill in isolation (runbook §5.1) before flying real work.

*Results report v1.0 — first supervised first flight, 2026-07-08.*
