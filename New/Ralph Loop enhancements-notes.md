Ralph Loop enhancements

8 17:08 UTC-8 | Avl Capacity: ~38% | Context compression: ~62%

Let me re-anchor the original goal before outlining, so the plan serves *that* and not a drifted version of it ([M13]/[I13]).

**The original goal** wasn't "fly a pilot" or "close FUPs" — those are steps that emerged. It was your opening problem: *the multi-step AI↔CC round-trips that eat a day or more per job — you personally acting as planner, consumer, and message bus between interactive CC sessions, hand-writing continuation prompts each time.* The goal is **to stop being that wire** — to run your real 4–6-iteration jobs headless so the loop does the planning/consuming/dispatching while you do other work.

The pilot proved the path *works*. But a proven path on a throwaway ≠ the goal met. The goal is met when a **real** job of yours drains unattended. Here's the outline from where you are now to there.

------

**Plan: from proven-pilot → original goal (real work runs headless, unattended)**

**Phase 1 — Close the two proven-run defects (removes known re-triggers).**

- **1a. FUP-0931** — `initiative_narrative.md` has no bootstrapping owner; you hand-seeded it for the pilot. Until fixed, every future run risks the same HALT. Decide fix-site (intake-scaffold step vs. seed-template convention vs. Planner precondition) → implement via CC.
- **1b. FUP-0930** — the report-recovery net is shape-hardcoded (only partially generalized for `doc_stub` at `9596b68`). Lower urgency than 0931 for *doc-shaped* work, but it bites the moment a real job uses a different shape. Sequence: 0931 before the next run; 0930 before real work that isn't doc-stub-shaped.

**Phase 2 — Prove the last unproven segment: `gate_human` async-resume.**

- The pilot proved clean-drain *and* refuse-to-fake-close, but **not** the async answer-and-resume drill (§5.1). A second trivial pilot engineered to raise one real `gate_human` on purpose — drop a `gate_response` file, watch the ~30s cycle resume. This is the only remaining unexercised part of the loop, and real work *will* hit gates. Prove it on a throwaway first.

**Phase 3 — Make operation genuinely unattended (daemonize).**

- The supervisor is still a hand-started foreground process — you must be watching for it to run, which re-introduces the babysitting the goal exists to kill. Register `python -m supervisor` as an NSSM service (your stack already uses NSSM) → resident, reconciles every ~30s whether or not you're at the terminal. **This is the step that converts "I launched a run" into "the fleet drains while I work."** Earned now (you've watched a clean flight).

**Phase 4 — Fly the first REAL initiative headless (the goal itself).**

- Pick one actual 4–6-iteration job you currently hand-carry. Pressure-test it against the four hard RL-fitness gates (decomposable / headlessly-verifiable / bounded predicate / durable substrate) *before* intake. Run intake → approve → let the daemonized supervisor drain it. **This is the moment the original goal is met** — a real job done without you being the wire.

**Phase 5 — Scale to throughput (the goal, amplified).**

- Once one real job runs unattended, seed several and let them drain concurrently (`uq_ralph_runs_active_per_project` guarantees one active run per project; projects run in parallel). This inverts the multi-day slog into background throughput — the fleet works while you do design/framing, which is the only part that stays yours.

------

**Two honest boundary notes** ([M13]):

- **The framing step never automates.** Every phase-4/5 job still needs you to decide the goal and its verification up front. The loop kills the *iterate-to-goal* wire, not the *frame-the-goal* work. That residue is correct, not a shortfall — and it's minutes, not days.
- **NOT-FIT work stays manual.** Jobs with taste-judged "done," or that need Claude.ai-surface authoring interleaved with execution (two-surface boundary), correctly fail intake and stay interactive. The goal is "real *loopable* work runs headless," not "all work."

**Sequencing recommendation** (recommendation, not decision — prioritization is yours): **1a → 2 → 3 → 4**. Rationale: 1a removes a re-trigger that would muddy phase 2; phase 2 proves the last unknown on a throwaway; phase 3 makes phase 4 actually unattended rather than supervisor-babysat; phase 4 is the goal. 1b and 5 slot in by shape-of-work and appetite respectively.

There's also the standing **runway-collision decision** (FR/NFR-inversion vs. Ralph-Loop-multi-project yield order) in the background — it gates the *bigger* RL buildout but **not** this path. Phases 1–4 need nothing from it. Worth noting only so it doesn't silently block you later.

Recommend: start with **Phase 1a (FUP-0931)** — I'll investigate the three bootstrap fix-sites against the actual intake SKILL + `seed.template_v1_4` + orchestrator spec (M365 read), recommend one, and stage a CC session-plan or paragraph-prompt to implement it. Proceeding to that investigation unless you redirect. Reason: it's the smallest step that de-risks every subsequent phase and sequences correctly ahead of the gate-rehearsal pilot.

One question before I start drafting anything for 1a, since it changes the fix-site and I won't guess ([I7]): **should `initiative_narrative.md` be bootstrapped by the intake scaffold (created once at intake, alongside the seed+registry), or by the orchestrator at run-start (created if absent before iteration 1)?** I lean **orchestrator-at-run-start** — it's the more robust site (covers re-runs and hand-seeded initiatives too, not just intake-born ones) — but the fix could reasonably live either place, and it's your architecture call.