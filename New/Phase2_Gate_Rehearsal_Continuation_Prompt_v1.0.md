# Continuation Prompt — Phase 2: `gate_human` Async-Resume Rehearsal

**Produced:** 2026-07-09 ~09:36 UTC-8 | **Session goal at handoff:** draft the Phase 2 session plan (design locked; drafting not yet started).
**Delivery note:** container-delivered from a browser surface (filesystem MCP absent this session). Successor: verify your own write surface before any K: write.

---

## 1. Header / orientation

You are continuing a multi-session effort whose **original goal** is to stop the operator being the manual wire between interactive CC sessions — i.e. run real 4–6-iteration jobs **headless** (intake → approve → supervisor drains to done) so the loop does the planning/consuming/dispatching. That goal is NOT yet met; a proving-pilot path has been validated and defects cleared. **This turn's job:** produce the **Phase 2 session plan** — a gate-rehearsal pilot that deliberately raises one `gate_human` to prove the async answer-and-resume drill (the only loop segment the first flight left unexercised).

The design for that plan is **locked** (§11 below). What remains is authoring the plan itself via `cf-session-plan-producer` → `cf-session-plan-reviewer` loop ([I3]), then the [M19] accuracy pass, then container-deliver for CC.

## 2. State of play (where the multi-phase plan stands)

Phase ladder to the original goal (established this session):
- **Phase 1 — close proven-run defects: ✅ DONE.** FUP-0930 (report-recovery net generalized to a deny-list, all shapes) + FUP-0931 (`initiative_narrative.md` bootstrapped idempotently at orchestrator BOOTSTRAP). 54 checks green; both `applied` in registry; commit `83bf9f9`; branch `harness/doc-stub-report-recovery` **merged to fork `main`** (operator-confirmed 2026-07-09).
- **Phase 2 — prove `gate_human` async-resume: ⧗ IN PROGRESS (this handoff).** Design locked; plan not yet drafted.
- **Phase 3 — daemonize supervisor (NSSM), make operation unattended: PENDING.**
- **Phase 4 — fly first REAL initiative headless (goal met): PENDING.**
- **Phase 5 — scale to concurrent fleet throughput: PENDING.**

First-flight result (2026-07-08): pilot `first_flight_docstub` drained to `INITIATIVE_COMPLETE`, 3 items / 3 iterations, ~$9.4 successful run. Proved clean-drain AND refuse-to-fake-closure (a real HALT-on-broken-closure, correctly escalated). Did NOT prove async answer-and-resume — that is Phase 2's whole purpose.

## 3. Outstanding items

- **[PRIMARY]** Draft the Phase 2 session plan to the §11 locked design.
- Run it through `cf-session-plan-reviewer` to zero warranted findings ([I3]).
- Run the [M19]/[I29] semantic-accuracy pass before claiming the plan done (it is a hand-authored operational deliverable → in-scope for the gate).
- Container-deliver (or K: write if filesystem MCP is present) for CC execution.

## 4. Next-up (immediate first actions for the successor)

1. Re-query substrate (§8) — do NOT trust the snapshot values below.
2. Invoke `cf-session-plan-producer` for the Phase 2 plan, feeding the §11 locked design.
3. Loop with `cf-session-plan-reviewer` per [I3].
4. [M19] accuracy pass → deliver.

## 5. Anti-drift (what this session is NOT)

- NOT flying a real initiative (that's Phase 4). Phase 2 is a **throwaway rehearsal** whose only purpose is the async-resume drill.
- NOT daemonizing the supervisor (Phase 3).
- NOT re-proving clean-drain or refuse-to-fake (first flight already did).
- Do NOT re-open the FR/NFR-inversion vs. Ralph-Loop yield-order decision — it does NOT gate Phases 1–4 and is the operator's call.

## 6. Files / sources (all verified readable this session via M365 unless noted)

- **Runbook v1.3** (`Supervised_First_Flight_Runbook_v1_3.md`) — §1.2 runtime gate (cwd+trust), §5 fleet caution, §5.1 async-answer drill. **Authoritative operational source.**
- **`seed.template_v1_4.md`** — the `gate_policy.pre_classification[]` mechanism + the `auto_resolve` trap (see §11). Ground-truth for the gate design.
- First-flight results (`First_Flight_Results_2026-07-08_v1.0.md`) + harness-fix results (`FUP-0930_0931_Harness_Fixes_Results_2026-07-09.md`).
- Ralph Loop User Guide **v1.0** (2026-07-02, in `\ralph`) is authoritative; the dot-prefixed `.v1_1` (2026-05-28) is an OLDER archived copy despite the higher number — do not use it.
- Orchestrator spec: current is **v1.9** per runbook citation, but only v1.5/1.7/1.8 (in `design\OLD\`) surfaced in M365 search this session; v1.9 NOT read. The seed template + runbook v1.3 §5.1 gave the operator-facing gate mechanics without it. If the successor needs the internal `gate_dc`/`gate_human` classifier schema, probe live `Ralph Loop\design\` for v1.9 first ([M17]).

## 7. Open questions for the operator

- None blocking. (Merge state resolved: `harness/doc-stub-report-recovery` is on `main`.)

## 8. DB / substrate snapshot (EXPECTED — re-query at session open, do not trust)

Values below are point-in-time from prior turns; per [I2] treat as expected-to-verify:
- `projects` lifecycle CHECK includes `pending_approval` (verified 2026-07-08). Re-`SELECT` the constraint.
- Prior fleet counts (2026-07-08, post-first-flight): complete **22**, candidate **15**, failed **3**, pending_approval **0**. **Re-query** — Phase 1 work and any other runs may have changed these.
- FUP-0930 / FUP-0931 → `status='applied'`, `applied_at=2026-07-09`. Verify still applied.
- Supabase project `eybdbshxswutgaaylpol`, DB `code_factory`. Use `PROD_DB_URL` (the intake insert, supervisor, and webui all read the SAME `PROD_DB_URL` — a mismatch means the Approve card never appears).

## 9. Anti-staleness obligations (mandatory re-checks before acting)

- **Merge state:** confirm `harness/doc-stub-report-recovery` is actually on `main` (git log) before pinning "runs on main" in the plan.
- **Skill versions:** scan `<available_skills>` for `cf-session-plan-producer` / `cf-session-plan-reviewer` current versions; `SELECT * FROM followups WHERE category='skill' AND status='pending'` for open issues against them ([I1]).
- **Seed schema:** confirm `seed_schema_version: 1.4` is still current and `gate_policy.pre_classification[].auto_resolve` semantics unchanged before relying on the §11 gate design.
- **Runbook version:** confirm v1.3 is still the highest Supervised_First_Flight_Runbook before quoting its §-numbers.
- Do NOT trust any version anchor in this prompt without a live re-query ([I2]).

## 10. Entry conditions (must hold before drafting)

- The Phase 2 pilot must run on the **fixed harness** (now `main` — merge confirmed).
- Runtime gate (runbook v1.3 §1.2): the plan MUST instruct running from **`Factory_V3` cwd** with the workspace **trusted** in `~/.claude.json`, or role-calls won't resolve / Executor writes get denied.
- Fleet caution (runbook v1.3 §5): to keep the rehearsal scoped, the plan runs the **orchestrator directly on the pilot seed** (not the fleet supervisor, which would dispatch all 15 candidates).

## 11. Scope of the locked design (feed this to cf-session-plan-producer)

**Objective:** a throwaway pilot that raises exactly one `gate_human`, to rehearse: notification-reaches-phone → operator drops a `gate_response` → orchestrator self-resumes on the next ~30s cycle.

**Locked design decisions:**
1. **Gate mechanism = deterministic `gate_policy.pre_classification[]` entry** — `pattern: "contains:<token>"` (or `cluster:<n>`), `class: gate_human`. Fires on cue; does not depend on the Answerer confidence roll.
2. **CRITICAL TRAP — the entry MUST NOT set `auto_resolve`.** Per `seed.template_v1_4.md` (FUP-0791, schema 1.4): if `auto_resolve` is present, the broker writes the `gate_response` itself and skips the operator-wait entirely → the drill never happens. The rehearsal REQUIRES a bare `gate_human` entry with no `auto_resolve`.
3. **Pilot shape = 2-item doc-stub drain** on the proven harness. Item 1 closes clean; item 2's session shape/plan raises a gate matching the `pre_classification` pattern. (Custom shape MUST still require the Executor to write `execution_report_<ITER>.md` with `## Items closed` — runbook v1.3 §2, else Consumer HALTs `failed_report_missing`.)
4. **`notification_channel: "gmail_smtp:default"`** — keep the template default (FUP-0863: email→phone). Proving the phone-reachable escalation is PART of the drill; `wintoast:default` is desktop-only and invisible headless.
5. **`mcp_servers: []`, `verification_bindings.doc_stub: []`** — the first-flight-proven trivial-pilot defaults (no supabase dependency, no reviewer-binding stall trap P4-07).
6. **Expected flow to assert in the plan:** item 1 → RESOLVED; item 2 → `running → paused_gate`; email notification; operator reads `state/iterations/NNNN/gate_request_*.json`, writes `gate_response_<iter>_<gate>.json` (`selected_option` or `custom_text`) into the iteration dir; next ~30s cycle → `paused_gate → running`; item 2 → RESOLVED; `registry_zero_open` → `INITIATIVE_COMPLETE`.
7. **Success criterion:** the operator observes the self-resume after dropping the file — that specific observation is the whole point of Phase 2.

## 12. Anti-pre-commit

- Do NOT begin the Phase 3 NSSM daemonization or any Phase 4 real-initiative work — those are gated behind a successful Phase 2 rehearsal.
- Do NOT run the fleet supervisor for this rehearsal (would dispatch the 15 live candidates = real spend); orchestrator-direct-on-seed only.
- Do NOT set `auto_resolve` on the rehearsal gate (defeats the entire exercise — see §11.2).
- The plan itself is a hand-authored operational deliverable → run the [M19]/[I29] accuracy pass before claiming it done.

---

*Continuation prompt v1.0 — Phase 2 gate-rehearsal, produced 2026-07-09.*
