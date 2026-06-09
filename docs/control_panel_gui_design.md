# RL Outer Loop — Control Panel GUI Design

| | |
|---|---|
| **Version** | 1.0 (Draft / Proposed) |
| **Date** | 2026-06-08 |
| **Status** | Design only — no code. Supersedes nothing; the CLI (`supervisor.control_panel`) remains the headless surface. |
| **Host (decided)** | Local web app: FastAPI read/action API + the existing Vite/React scaffold (`Ralph-dev/flowchart/`), run on the operator's machine against the dev/prod DB. |
| **Scope** | An integrated operator console that replaces the 14-verb CLI mental model with a task-oriented GUI. The GUI adds **no new decision logic** — it is a presentation + action layer over the already-tested pure cores and the `Registry`. |
| **Related** | `Control_Panel_User_Guide_v1.1.md` (current CLI), `RL_OL_Learning_Effect_Measurement_Spec_v1.0.md`, `RL_OL_Fleet_Analytics_and_ABS_Onramp_Spec_v1.0.md`, Outer-Loop Spec §4.4 / §8 (attention) / §13.1 (completion). |

---

## 1. Problem statement

The control panel grew command-by-command into 14 verbs: `status, metrics, pause, query, bump, learnings, corrections, promote, reject, apply, effects, events, forecast, onramp-abs, events-prune`. Each was a reasonable addition; together they impose four costs on the operator:

1. **Polling.** Nothing tells the operator where to look — they must run `status`, then `learnings`, then `effects`, then `events` to discover whether anything needs them.
2. **Verbs + keys.** Acting requires memorising a verb and copy-pasting an opaque `finding_key` / `project_id` between commands.
3. **Hand-authored intent.** `pause` / `bump` / `query` write command JSON with semantics the operator must know — and the channel is asynchronous, which the CLI does not make visible.
4. **A fragmented workflow.** The find → adopt → measure improvement loop is one conceptual flow but is split across six disconnected verbs (`learnings`, `promote`, `apply`, `reject`, `effects`, `corrections`).

The CLI is organised around *what the code can do*. The GUI is organised around *what the operator is trying to do*.

## 2. Operator jobs-to-be-done

| Job | Today's verbs | Frequency |
|---|---|---|
| **Respond** to what needs a human (gates, stalls, budget breaches, ready learnings, regressions) | scattered across `status`, `learnings`, `effects`, + email | every visit |
| **Watch** fleet health | `status`, `metrics` | every visit |
| **Steer** a project (pause, budget, priority, provision work) | `pause`, `bump`, `query`, `onramp-abs` | occasional |
| **Improve** (review learnings → adopt → confirm it helped) | `learnings`, `promote`, `apply`, `reject`, `effects`, `corrections` | per cycle |
| **Investigate** a project's history | `events`, `metrics --fleet` | on incident |

## 3. Design principles

1. **Attention-first, not command-first.** The home screen is a single prioritised **Needs You** inbox that unifies every "a human is required" signal. The operator never polls.
2. **No verbs, no keys.** Every action is a button on the object it acts on. `finding_key`, `project_id`, and command JSON are never typed.
3. **Progressive disclosure.** One glance answers "does anything need me?" One click drills from fleet → project → run → events. Detail is never on screen until asked for.
4. **Honest asynchrony.** The orchestrator command channel is consumed on the orchestrator's next cycle. The UI shows real command state (`queued → acked → applied`) — never fake immediacy.
5. **Recommended option first.** Actionable cards lead with the recommended, reversible, one-confirm option (FR-032), mirroring the attention scheduler.
6. **The GUI owns no logic.** All reads route through `Registry` + the existing pure functions; all writes route through the existing write seams. The CLI and GUI are two clients over one tested core.

## 4. Screen specifications

### 4.1 Home — the Action Inbox (default screen)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Outer Loop Supervisor        ● live · synced 3s ago · $42.18 today · ⚙   │
├────────────┬──────────────────────────────────────────────────────────────┤
│  ▸ Home  ④ │   NEEDS YOU                                      sort: urgency ▾│
│  ▸ Fleet   │   ┌────────────────────────────────────────────────────────┐ │
│  ▸ Improve │   │ 🔴 GATE  oltest_c2 · "proceed to Phase 1?"               │ │
│  ▸ Spend   │   │     planner asks; answerer unsure (conf 0.61)            │ │
│  ▸ Events  │   │     ▸ recommended: Proceed    [Proceed] [Hold] [Details] │ │
│            │   ├────────────────────────────────────────────────────────┤ │
│  FLEET     │   │ 🟠 STALL  oltest_d2 · no heartbeat 42m (pid 81004 alive) │ │
│  3 running │   │     [Investigate] [Pause] [Force-reap]                   │ │
│  1 stalled │   ├────────────────────────────────────────────────────────┤ │
│  1 gate    │   │ 🟡 LEARNING  add answerer rule · gate stopped needing me │ │
│  $42 / $400│   │     seen across 4 runs → cf-spec-writer                  │ │
│            │   │     [Adopt] [Reject] [Why?]                              │ │
│            │   ├────────────────────────────────────────────────────────┤ │
│            │   │ 🟣 REGRESSED  spec_review_loop tune made it worse        │ │
│            │   │     revise-rate 0.20 → 0.90 over 3 runs                  │ │
│            │   │     [Revert] [Keep anyway] [Details]                     │ │
│            │   └────────────────────────────────────────────────────────┘ │
│            │   Nothing else needs you. Fleet is healthy. ✓                 │
└────────────┴──────────────────────────────────────────────────────────────┘
```

**Replaces:** polling `status` + `learnings --status proposed` + `effects` + watching email.
**Behaviour:** cards sorted by urgency tier (top-tier safety/kill-switch first, per the attention scheduler). Each card is self-contained with inline actions and the recommended option first. Acting resolves or advances the card and decrements the rail badge `Home ④`.

**Inbox signal sources** (what feeds the queue):

| Card type | Source |
|---|---|
| 🔴 Gate (operator decision) | gate-escalation events / gate_request artefacts + attention store escalations (`awaiting operator`) |
| 🟠 Stall / no-heartbeat | fleet snapshot `heartbeat_state == STALLED`; reconcile stall outcome |
| 🔴 Budget breach | spend backstop / forecast ceiling escalation |
| 🟡 Learning ready | `read_audit_findings` where `status == proposed` |
| 🟣 Regressed / no-effect adoption | `read_audit_effects` where `outcome in {regressed, no_effect}` (the `*fleet*` escalations that have no project row) |
| ⚪ Correction churn alert | `read_correction_summary` items over a churn threshold |

### 4.2 Fleet — watch + steer (no command JSON)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  FLEET                                              ● refresh 5s · pause ⏸ │
├─────────────────────────────────────────────────────────────────────────┤
│  PROJECT      LIFECYCLE   RUN        ATTN  OPEN   COST    ♥        ┄        │
│  ▸ oltest_c2  running     executing   1    7    $12.41   ok    [Pause][$] │
│  ▾ oltest_d2  running     STALLED     0    3    $ 8.07   42m!   [Pause][$] │
│      ┌─ run #418  spawned 14:02 · pid 81004 · $8.07 · binding cf-x: pass  │
│      │  events ▸   cost breakdown ▸   gate history ▸   seed ▸             │
│      └─ [Investigate timeline]   [Bump budget ___]   [Set priority ___]   │
│  ▸ oltest_d3  candidate    —          0    5     —      —      [Admit]    │
├─────────────────────────────────────────────────────────────────────────┤
│  3 running / ceiling 3 · headroom 0 · 1 stalled · total $42.18 (info)     │
│  + Provision work (ABS on-ramp) ▸                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

**Replaces:** `status`, `metrics`, `pause`, `bump`, `query`, `onramp-abs`.
**Behaviour:** the FR-058/059 fleet table, each row expandable to a project detail (active run, runs history, cost, gates, lifecycle timeline, seed). `pause` becomes a toggle; `bump` an inline field; `query` an on-demand "refresh from register"; the cost column carries the FR-063 non-binding footnote. Each control shows command state (`queued → acked → applied`). "Provision work" is the `onramp-abs` wizard with a dependency-graph preview before `--apply`.

### 4.3 Improve — the find → adopt → measure loop as one board

The highest-value consolidation: the six improvement verbs become one left-to-right kanban where Applied cards carry their **measured effect**.

```
┌──────────────┬──────────────┬───────────────┬──────────────────────────────┐
│  PROPOSED ②  │  ACCEPTED ①  │   APPLIED ③   │      MEASURED (effects)       │
├──────────────┼──────────────┼───────────────┼──────────────────────────────┤
│ answerer rule│ shape tune   │ over-verif    │ ✅ confirmed  gate 1.0→0.0    │
│ gate ×4 runs │ →plan-review │ drop cf-x     │ ✅ confirmed  spend saved*    │
│ →cf-spec-wr  │ [Apply ▸]    │ [dispatched]  │ 🟣 regressed  shape 0.2→0.9   │
│ [Adopt][✗]   │              │               │    [Revert]                   │
└──────────────┴──────────────┴───────────────┴──────────────────────────────┘
   correction churn ▸  OLB-07: 5 attempts / 2 projects / deepest L4
   * over-verification: spend saved; cannot prove no defect now slips (D3 limitation)
```

**Replaces:** `learnings`, `promote`, `apply`, `reject`, `effects`, `corrections`.
**Behaviour:** columns map to the `proposed → accepted → applied → (measured)` lifecycle. "Adopt" = `set_finding_status(accepted)`; "Apply" dispatches the named cf-* authoring skill (under its own review — the supervisor never edits the spec); the Measured column reads `run_audit_effects` and surfaces `confirmed / no_effect / regressed / pending` with the before→after metric and the D3 limitation note. "Why?" shows the evidence and (future) the diff the skill will attempt. Correction churn sits beneath as the chronic-defect indicator.

### 4.4 Spend

**Replaces:** `forecast`, `events-prune`.
Spend-to-completion forecast (per-item vs per-run basis, confidence by sample size) as a chart; today's burn vs ceiling with the warn line; retention (`events-prune`) as an admin control with a dry-run count first.

### 4.5 Events

**Replaces:** `events`, `metrics --fleet`.
A filterable, searchable fleet timeline (project / type / time range) with one-click "jump to this run's timeline."

## 5. Command → GUI mapping (complete)

| CLI today | GUI home |
|---|---|
| `status`, `metrics` | Fleet table + Home health strip |
| `pause`, `bump`, `query` | Inline Fleet row controls (toggle / field / refresh) |
| `learnings`, `promote`, `apply`, `reject` | Improve kanban columns + card actions |
| `effects`, `corrections` | Improve "Measured" column + churn strip |
| gate escalations / stalls / regressions (email-only today) | Home inbox cards |
| `forecast`, `events-prune` | Spend |
| `events`, `metrics --fleet` | Events timeline |
| `onramp-abs` | Fleet "+ Provision work" wizard (with graph preview) |

## 6. Architecture

```
React (Vite scaffold, flowchart/)  ──HTTP + SSE──►  supervisor/api.py  (thin FastAPI)
   Home / Fleet / Improve / Spend / Events             │
                                                        ├─ READS  → Registry.read_* + pure cores
                                                        │           (render_* become JSON serializers)
                                                        └─ ACTIONS → existing write seams only
```

**Reuse — the GUI adds no logic.** Every endpoint is a thin adapter:

| Endpoint area | Backed by (already exists) |
|---|---|
| Fleet snapshot | `build_full_fleet_snapshot`, `Registry.read_active_runs / read_all_projects / read_cumulative_spend_usd` |
| Inbox | aggregation over attention escalations, fleet stalls, `read_audit_findings(status=proposed)`, `read_audit_effects(outcome≠confirmed)`, `read_correction_summary` |
| Learnings / effects / corrections | `read_audit_findings`, `read_audit_effects`, `read_correction_summary`, `summarize_finding_statuses`, `summarize_effect_outcomes` |
| Forecast | `forecast_fleet`, `read_learning_records`, `open_work_counts_for` |
| Events | `read_events_db`, `summarize_events` |
| Pause / bump / query | `write_command` (unchanged JSON the orchestrator consumes) |
| Promote / reject | `set_finding_status` |
| Apply | `build_dispatch_command` + the existing dispatch + `_dispatch_succeeded` guard |
| Provision (on-ramp) | `abs_chain_plan`, `Registry.upsert_project` |
| Retention | `Registry.prune_events` |

**Invariants preserved** (the GUI does not weaken the mechanical-supervisor stance):

- **NFR-006** — `Registry` remains the sole writer; the API calls its methods, adds none.
- **NFR-005 / FR-053** — the GUI never edits a spec; "Apply" dispatches the named cf-* skill, which applies under its own review.
- **D1 surface-only** — a regressed/no-effect adoption is *offered* a Revert button; it is never auto-reverted.
- **FR-058 / OLB-16** — the live fleet snapshot stays a read-only projection; SSE replaces manual refresh but not the contract.
- The CLI is unchanged and remains the headless / automation / cron surface.

**API contract sketch** (read = GET, action = POST; all JSON):

```
GET  /api/fleet                      → snapshot rows + rollup
GET  /api/inbox                      → prioritised needs-you cards
GET  /api/learnings?status=          → findings (lifecycle)
GET  /api/effects                    → measured effects
GET  /api/corrections                → per-item churn
GET  /api/forecast                   → spend-to-completion
GET  /api/events?project=&type=&limit= → fleet events
GET  /api/stream                     → SSE: snapshot + inbox deltas (push)
POST /api/projects/{id}/pause        → write_command(pause)         → {command_id, state}
POST /api/projects/{id}/budget       → write_command(bump_budget)   → {command_id, state}
POST /api/findings/{key}/promote     → set_finding_status(accepted)
POST /api/findings/{key}/reject      → set_finding_status(rejected)
POST /api/findings/{key}/apply       → dispatch cf-* skill          → {applied|left-accepted, output}
POST /api/effects/{key}/revert       → routes to authoring skill (re-propose); surface-only
POST /api/onramp/abs?apply=          → abs_chain_plan / upsert_project
```

**Security.** The console controls real spend → at minimum bind to localhost, single-operator assumption, and never expose the DSN to the browser (the API holds it). Action endpoints log the issuer (`--by` equivalent) for the future operator action log.

## 7. Future functions the GUI unlocks

- **Dependency graph** of `depends_on` / ABS chain — the Vite scaffold is a graph renderer; this is its real first job.
- **Gate conversation view** — the request, the answerer DSL that fired, the operator decision, and the *outcome* of that decision (closes the loop by pairing gates with effect-measurement).
- **Apply-preview diff** — show what the cf-* skill will change before dispatch.
- **Operator action log** — who approved / reverted / paused what, when (accountability; nothing records this today).
- **Cost drill-down** per role / per closed item (ties to the forecast basis refinement).
- **Notification routing** — per-signal in-app-only vs email vs both.
- **Multi-initiative grouping** — collapse the fleet by initiative.

## 8. Phasing

1. **Read API + Fleet + Home (read-only).** Biggest visibility win, zero write risk. Prove the SSE feed and the inbox aggregation.
2. **Inline actions.** pause / bump / promote / reject / adopt with honest command-state tracking.
3. **Improve kanban + effects.** The loop-as-one-board.
4. **Spend, Events, dependency graph, operator action log.**

Each phase is independently shippable and leaves the CLI fully functional.

## 9. Open decisions

- **D-GUI-1 — Auth model.** Localhost-only single operator (simplest) vs token auth for remote access. *Recommendation: localhost-only for v1; the console controls real spend.*
- **D-GUI-2 — Live transport.** SSE (one-way push, simplest) vs WebSocket (needed only if the UI grows bidirectional streaming). *Recommendation: SSE.*
- **D-GUI-3 — Command-state visibility.** How deeply to surface the async command lifecycle (`queued → acked → applied`) — requires the orchestrator to ack consumed commands (it may not today). *Recommendation: start with `queued` + best-effort `applied`-on-next-snapshot; add ack as a follow-on.*
- **D-GUI-4 — Effect "Revert".** A revert is itself a change routed to a cf-* skill (re-propose / relax), not a literal undo. Confirm this framing is acceptable vs a manual operator task.

## 10. Non-goals

- Not replacing the CLI (it stays the headless/automation surface).
- Not adding decision logic to the front end (presentation + action only).
- Not auto-applying or auto-reverting anything (the supervisor stays mechanical; the operator confirms).
- Not a multi-tenant / hosted product — a single-operator local console.
