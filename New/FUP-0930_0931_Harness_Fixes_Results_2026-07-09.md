# Harness Fixes — FUP-0930 + FUP-0931 — Results Report

| | |
|---|---|
| **Date** | 2026-07-09 |
| **Origin** | First supervised first flight (`first_flight_docstub`, 2026-07-08) — the two residual harness gaps it surfaced |
| **Branch / commit** | `harness/doc-stub-report-recovery` — `83bf9f9` (supersedes stopgap `9596b68`) |
| **Files** | `hooks/execute_with_gates.sh`, `orchestrator.sh`, `tests/test_fup_0930_0931_harness_fixes.sh` |
| **Outcome** | ✅ Both fixed and tested — **54 checks green, 0 fail**; both follow-ups marked `applied` in the registry |

---

## FUP-0930 — report-recovery net generalized

**Defect.** `hooks/execute_with_gates.sh`'s FUP-0854 report-recovery net (which re-asks the Executor to write a missing `execution_report_NNNN.md` so the Consumer can commit closures) was gated on a **hardcoded shape allowlist** (`component_build|integration_checkpoint|skill_build`, +`doc_stub` via the stopgap). Any new/custom shape whose plan omitted the report HALTed `failed_report_missing` with no recovery.

**Root-cause confirmation.** The Consumer (`rl-iteration-consumer`) requires `execution_report_NNNN.md` for **every** shape it processes — Failure Protocol **Row 5** HALTs on a missing/unparseable report with no shape exemption (the v1_4 empty-binding rule relaxes only the *verification* aspect, not the report itself).

**Fix.** Invert the allowlist to a deny-list: fire the recovery for **all** shapes except `noop`/empty (where the Consumer is skipped and no Executor ran). New/custom shapes are now covered with no per-shape edit.

## FUP-0931 — `initiative_narrative.md` bootstrapped at launch

**Defect.** `initiative_narrative.md` is a **required input for both** the Planner (Inputs Contract: last K summaries + `fail_counts` tail) **and** the Consumer (Row 1 "input load failure" HALT), but **no role created it**. A fresh initiative HALTed on iteration 0001. (The first flight only completed because the file was hand-seeded.)

**Fix.** `orchestrator.sh` now seeds a minimal skeleton (`# Initiative Narrative`, `## Iteration summaries`, `## fail_counts`) at BOOTSTRAP **only when absent** — idempotent; the Consumer overwrites it with real per-iteration summaries thereafter. Chosen over an intake-side scaffold because it covers **every** launch path (intake-created or hand-seeded).

---

## Testing

**New — `tests/test_fup_0930_0931_harness_fixes.sh` (20 checks, all PASS):**
- *0930 behavioural* — the exact case logic selects the recovery branch for all 9 real shapes **plus** a brand-new custom shape, and exempts `noop`/empty (12 checks).
- *0930 static* — the real file has the `""|noop)` skip branch + `*)` catch-all and no longer carries the hardcoded allowlist line (3 checks).
- *0931 behavioural* — the **real** `orchestrator.sh` (driven by a mock `claude`, zero spend) seeds the narrative on a fresh launch with the correct headers and logs the FUP-0931 line; and a pre-existing narrative is **not** clobbered (idempotency) (5 checks).

**Regression — existing integration suites re-run green (34 checks):**
- `test_fup_0815_orchestrator_integration.sh` — 17 PASS (orchestrator bootstrap + loop + pause).
- `test_fup_0851_0852_failure_and_gate_handling.sh` — 7 PASS (execute_with_gates failure/gate/threshold paths).
- `test_fup_0774_rl_integration.sh` — 10 PASS (RL role integration, gate demote/defer/escalate).

Plus `bash -n` clean on both edited scripts.

**Total: 54 checks, 0 fail.** All tests use the mock-`claude` PATH-shim — **no real API spend**.

## Registry

`FUP-0930` and `FUP-0931` → `status='applied'`, `applied_at=2026-07-09`, notes cite commit `83bf9f9` + the tests.

## Not yet done (your call)

- **Push/merge** `harness/doc-stub-report-recovery`. Its two new commits vs. `main` are `9596b68` (stopgap) + `83bf9f9` (general fix); the branch's base commit `8a6c031` (webui) is already merged content on the fork main, so a PR would show only the harness changes. I can push + open a PR to your fork, optionally squashing the stopgap into the general fix first.
- The `.bak-firstflight` backups (`execute_with_gates.sh.bak-firstflight`, `~/.claude.json.bak-firstflight`) remain for safety — removable on request.

*Results report — FUP-0930/0931 harness fixes, 2026-07-09.*
