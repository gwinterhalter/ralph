---
seed_schema_version: 1.4
initiative:
  slug: example-initiative
  title: Example Initiative
  owner: gwinterhalter

# Optional model overrides (schema 1.2; FUP-0721). When declared, the orchestrator passes
# `--model <value>` to the matching role-call invocation. When absent, the CLI default
# applies (whatever the user's `claude` session resolves). Recommended values:
# `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`. Per-role overrides let an
# initiative pin a model for determinism + per-role cost tuning (e.g. Sonnet for the
# Executor's bulk write work, Opus for the Planner / Answerer judgement, Haiku for the
# Consumer's mechanical projection).
# executor_model: claude-sonnet-4-6
# planner_model: claude-sonnet-4-6
# consumer_model: claude-sonnet-4-6
# answerer_model: claude-sonnet-4-6

# Optional per-iteration workstreams heartbeat (schema 1.3; FUP-0798). When declared, the
# orchestrator UPSERTs the named workstreams row at iteration begin + close so the operator
# has a DB-queryable signal of progress during long-running headless RL runs (rather than
# leaving last_session_label / next_session_blocker / metadata stale at the launch state).
# The env_vars indirection holds env-var NAMES the operator exports at machine scope; the
# orchestrator indirect-looks-up the values (same pattern as notification_channel.primary_env_vars).
# Backward-compat: omit the heartbeat: block entirely OR omit workstream_id → silent skip.
# heartbeat:
#   workstream_id: 42                         # integer; the workstreams.workstream_id PK
#   env_vars:
#     project_url: CF_SUPABASE_PROJECT_URL    # e.g. https://eybdbshxswutgaaylpol.supabase.co
#     service_role_key: CF_SUPABASE_SERVICE_ROLE_KEY
workspace_root: K:/Claude Code Factory/V3/initiatives/example
read_only_paths:
  - K:/Claude Code Factory/V3/Project_Docs/Project_Docs_Current
mcp_servers:
  # Per-server entries shape `{name, command, args, env}` (mirrors `claude --mcp-config`
  # documented format). The `generate_mcp_config` hook (Phase 4a P4-01) reads this block,
  # merges with per-iteration `mcp_additions.json` if present, and writes
  # `iterations/NNNN/mcp_config.json` consumed by `claude --print --mcp-config`.
  - name: example
    command: example-mcp-server
    args: []
    env: {}
state_dir_relative: state
work_registry: K:/Claude Code Factory/V3/initiatives/example/work_registry.md
context_documents:
  - K:/Claude Code Factory/V3/initiatives/example/context/overview.md
session_shape_catalog:
  - name: spec_edit
    template_pointer: shape_spec_edit
  - name: code_change
    template_pointer: shape_code_change
verification_bindings:
  spec_edit:
    - cf-doc-reviewer
  code_change:
    - cf-code-review
    - cf-pytest
completion_predicate:
  - name: registry_drained
    check_kind: registry_zero_open
    params:
      registry: K:/Claude Code Factory/V3/initiatives/example/work_registry.md
  - name: release_notes_present
    check_kind: artefact_exists
    params:
      path: K:/Claude Code Factory/V3/initiatives/example/RELEASE_NOTES.md
gate_policy:
  pre_classification:
    - pattern: "cluster:high-risk"
      class: gate_human
    # FUP-0791 (schema 1.4): optional auto_resolve field on a pre_classification entry. When
    # the matching pattern fires AND auto_resolve is set, the broker writes a gate_response
    # directly with the named option_id, skipping both gate_dc Answerer escalation and
    # gate_human operator-wait paths. Use for recurring A/A operator patterns (e.g. cf-* skill
    # build sub-gates "deliverable-scope = A=SKILL.md+tests" and "section-8-4-spec-wiring =
    # A=defer to follow-on spec_bump"). Operator override: bump the seed to remove the
    # auto_resolve field if a future iteration needs a different shape. Backward-compat:
    # entries without auto_resolve resolve through the existing classify-and-escalate path.
    # Example:
    # - pattern: "contains:deliverable-scope"
    #   class: gate_human
    #   auto_resolve: A
    # - pattern: "contains:section-8-4-spec-wiring-coupling"
    #   class: gate_human
    #   auto_resolve: A
  confidence_threshold: 0.7
budget:
  iterations_max: 20
  tokens_usd: 50.00
  hang_timeout_seconds: 1800
# FUP-0863: default to the DELIVERING channel (gmail_smtp) so a "needs operator answer"
# escalation actually reaches you (email -> phone push). The scalar `gmail_smtp:default` form
# auto-resolves the F_GMAIL_SMTP_USER / _TO / _APP_PASSWORD / _HOST / _PORT env vars and falls
# back to a desktop win11toast when SMTP is unset. `wintoast:default` (the old default) is
# DESKTOP-ONLY — it cannot reach a phone and is invisible on a headless/unattended host.
notification_channel: "gmail_smtp:default"
permission_posture: "--permission-mode auto"
---

# Example Initiative — Seed (template)

This is the **example seed** documenting the §8.5 normative contract of
`Initiative_Orchestrator_Spec_v1_4.md`. Copy it, replace the placeholder
values in the YAML frontmatter, and point the orchestrator at the copy:
`bash ./orchestrator.sh <path-to-your-seed.md>`.

## Field reference (frontmatter is canonical; this body is documentation only)

| Field | Required | Meaning |
|---|---|---|
| `seed_schema_version` | yes | Schema format version; orchestrator refuses a seed whose major exceeds its supported major (§8.3). Current: `1.4`. |
| `initiative.slug` / `.title` / `.owner` | yes | Kebab-case id, human title, operator handle. |
| `workspace_root` | yes | Absolute path; root for all initiative writes. |
| `read_only_paths[]` | yes | Absolute paths the orchestrator and every subprocess must never write to. Always include `Project_Docs_Current`. |
| `mcp_servers[]` | yes | Per-server entries `{name, command, args, env}` (mirrors `claude --mcp-config` documented format). The `generate_mcp_config` hook merges these with per-iteration `mcp_additions.json` (if present) and writes `iterations/NNNN/mcp_config.json` consumed by `claude --print --mcp-config`. Empty array `[]` is acceptable for initiatives requiring no MCP servers. |
| `state_dir_relative` | yes | Path relative to `workspace_root` holding orchestrator state (`iterations/`, `gates/`, `escalations/`, `logs/`, `state_snapshot.json`) per §6.1. |
| `work_registry` | yes | Absolute path to the file the orchestrator updates on closure. |
| `context_documents[]` | yes | Files loaded into Planner/Answerer system-prompt context. |
| `session_shape_catalog[]` | yes | Named shapes the Planner may select; each pairs a `name` with a `template_pointer`. |
| `verification_bindings.<shape>[]` | yes | Per-shape list of verification skills the Consumer invokes. |
| `completion_predicate[]` | yes | Ordered named checks; orchestrator emits `INITIATIVE_COMPLETE` when all pass. Each entry: `name`, `check_kind`, `params`. |
| `gate_policy.pre_classification[]` | yes | Patterns (per §10.2 DSL) that pre-class a gate as `gate_human`. Each entry: `pattern` (required; `gate_id:<id>` / `cluster:<name>` / `contains:<substring>`) + `class` (required; `gate_human` or `gate_dc`) + `auto_resolve` (optional; schema 1.4 — when set, broker writes a gate_response directly with the named option_id, skipping Answerer/operator). |
| `gate_policy.pre_classification[].auto_resolve` | no (schema 1.4) | Optional string holding the gate `selected_option` value the broker auto-fills when this pattern matches. Closes FUP-0791 — recurring A/A operator patterns (e.g. cf-* skill_build deliverable-scope = A=SKILL.md+tests; section-8-4-spec-wiring = A=defer to follow-on spec_bump) no longer interrupt the run. Backward-compat: entries without `auto_resolve` use existing classify-and-escalate path. Audit trail: every auto-resolve appends a row to `state_dir/logs/auto_resolve.log`. |
| `gate_policy.confidence_threshold` | yes | Float in [0,1]; default `0.7`. |
| `budget.iterations_max` / `.tokens_usd` / `.hang_timeout_seconds` | yes | Iteration ceiling; USD ceiling; per-Executor no-progress timeout. |
| `notification_channel` | yes | Declarative target for the notification one-liner. |
| `permission_posture` | yes | Exact flag set passed to the Executor. Default `--permission-mode auto`; fallback `--permission-mode acceptEdits --dangerously-skip-permissions`. |
| `executor_model` | no (schema 1.2) | Optional `--model` value passed to the `claude --print` Executor invocation (`hooks/execute_with_gates.sh`). Absent → orchestrator omits `--model`, CLI default applies. Closes FUP-0721 — Plan-side model bindings (e.g. SA Q3 "Sonnet 4.6") are now enforceable rather than aspirational. |
| `planner_model` | no (schema 1.2) | Optional `--model` value passed to the `claude -p` Planner Role Call (`orchestrator.sh` `run_claude_json` wrapper at line 183). Absent → CLI default. |
| `consumer_model` | no (schema 1.2) | Optional `--model` value passed to the `claude -p` Consumer Role Call (`orchestrator.sh` lines 108 + 291). Absent → CLI default. |
| `answerer_model` | no (schema 1.2) | Optional `--model` value passed to the `claude -p` Operator-Answerer Role Call (`hooks/execute_with_gates.sh` line 128). Absent → CLI default. |
| `heartbeat.workstream_id` | no (schema 1.3) | Optional integer naming the `workstreams.workstream_id` PK the orchestrator UPSERTs at iteration begin + close (`orchestrator.sh` lines ~195 / ~351). Closes FUP-0798 — Plan-side operator awareness during long-running headless RL runs is now DB-queryable without manually reading `state_dir/state_snapshot.json`. Absent → silent skip (logged to `state_dir/logs/heartbeat.log`). |
| `heartbeat.env_vars.project_url` | no (schema 1.3) | Optional string holding the NAME of an env var the operator exports at machine scope; the orchestrator indirect-looks-up the value (the Supabase project URL e.g. `https://eybdbshxswutgaaylpol.supabase.co`). Same indirect-via-env-var-name pattern as `notification_channel.primary_env_vars`. Required iff `heartbeat.workstream_id` declared; absent → silent skip. |
| `heartbeat.env_vars.service_role_key` | no (schema 1.3) | Optional string holding the NAME of an env var holding the Supabase service-role key (sensitive; never echoed/logged). Required iff `heartbeat.workstream_id` declared; absent → silent skip. |

Optional fields (§8.2): `target_order[]`, `initiative.description`, `initiative.target_completion_estimate`, `executor_model`, `planner_model`, `consumer_model`, `answerer_model`, `heartbeat.workstream_id`, `heartbeat.env_vars.project_url`, `heartbeat.env_vars.service_role_key`.

## `completion_predicate[].check_kind` enum (§8.4)

`registry_zero_open` (registry has no open items) · `artefact_exists` (a named file exists) · `skill_clean` (a named skill run returns clean against a target) · `doc_review_clean` (cf-doc-reviewer returns zero findings on a named doc) · `db_zero_open` (a SQL-scoped set in the corpus database is empty; `params.scope_sql` REQUIRED, `params.table` defaults to `public.followups`; fails CLOSED on an unreachable database, a missing `SUPABASE_DB_PASSWORD`, a query error or an unparseable result — added 2026-08-08).

## Change History

| Version | Date | Author | Summary |
|---------|------|--------|---------|
| 1.4 | 2026-06-02 | Claude Code | **FUP-0791 closure** — adds optional `auto_resolve: <option_id>` field on `gate_policy.pre_classification[]` entries. When the matching pattern fires AND auto_resolve is set, the `hooks/execute_with_gates.sh` broker writes `gate_response_NNNN_MMMM.json` directly with the named `selected_option`, skipping both `gate_dc` Answerer escalation and `gate_human` operator-wait paths. Closes the recurring A/A operator pattern observed 3 times in Phase 6 S2 (iter-0003 G24+G25, iter-0005 G26, iter-0006 G28; each instance cost ~$2-4 Answerer compute + operator wait-state + orchestrator BLOCK/resume cycle). Operator override preserved: bump the seed to remove the `auto_resolve` field if a future iteration needs a different shape. Audit trail: every auto-resolve appends a row to `state_dir/logs/auto_resolve.log` (gate_id, matched pattern, matched pc index, auto-resolved option, timestamp). Wiring: `hooks/execute_with_gates.sh` Pass-1 broker loop gains `matched_pc_idx` tracking + post-loop auto_resolve handling block that writes the gate_response and `continue`s before the gate_human deferral / gate_dc Answerer paths. Backward-compat: entries without `auto_resolve` use existing classify-and-escalate behaviour unchanged. Field-reference table extended (gate_policy.pre_classification row note + new auto_resolve row). No required-field change; no schema-major bump. Surfaced as IOS §8.1 schema-1.4 candidate via FUP-0791 closure metadata. |
| 1.3 | 2026-06-02 | Claude Code | **FUP-0798 closure** — adds optional `heartbeat:` block at the frontmatter top with 3 fields: `heartbeat.workstream_id` (integer; the workstreams.workstream_id PK to UPSERT) + `heartbeat.env_vars.project_url` (env var NAME holding the Supabase project URL) + `heartbeat.env_vars.service_role_key` (env var NAME holding the service-role key). When all 3 declared AND env vars exported at machine scope, the orchestrator UPSERTs `last_session_label` / `next_session_blocker` / `metadata.heartbeat_at` / `metadata.heartbeat_iter` / `metadata.heartbeat_phase` at iteration begin + close. When any field absent or env var unset, silent skip (logged to `state_dir/logs/heartbeat.log`). Wiring: NEW `lib/heartbeat.sh` with the `heartbeat_workstream()` function (PostgREST PATCH with metadata merge; non-fatal on any error — never propagates curl rc up the orchestrator); `orchestrator.sh` sources lib/heartbeat.sh + calls `heartbeat_workstream` at iteration begin (line ~195) + close (line ~351). Backward-compat: absent OR partial heartbeat block → no-op + log. Closes the operator-awareness gap that the auto_build_spec_closure 22-hour run surfaced ("I was unaware auto-build-spec had begun"). Field-reference table +3 rows. §8.2 optional fields list extended. No required-field change; no schema-major bump. Surfaced as IOS §8.1 schema-1.3 candidate via FUP-0798 closure metadata. |
| 1.2 | 2026-06-02 | Claude Code | **FUP-0721 closure** — adds 4 optional model-override fields (`executor_model`, `planner_model`, `consumer_model`, `answerer_model`) at the frontmatter top with a commented-out example block. When declared, the orchestrator passes `--model <value>` to the matching role-call invocation; when absent, the CLI default applies (no breaking change to existing schema-1.1 seeds). Wiring sites: `hooks/execute_with_gates.sh` line 220 (Executor `claude --print`) + line 128 (Answerer `claude -p`); `orchestrator.sh` `run_claude_json` wrapper at line 43-60 + Planner dispatch line 183 + Consumer dispatches lines 108 + 291. Closes the Plan-side model-binding enforceability gap (e.g. SA Q3 "Sonnet 4.6" was previously aspirational; the orchestrator inherited the CLI default). Field-reference table gains 4 rows. §8.2 optional fields list extended. No required-field change; no schema-major bump. Surfaced as IOS §8.1 schema-1.2 candidate via FUP-0721 closure metadata. |
| 1.1 | 2026-05-23 | Claude Code | Phase 4a (P4-01): added `mcp_servers[]` frontmatter block + body field-reference row. Per-server shape `{name, command, args, env}` consumed by the new `generate_mcp_config` hook in `execute_with_gates.sh`. This is a §8.1 extension to `Initiative_Orchestrator_Spec_v1_4.md`; surfaced as Spec v1.5 candidate via Phase Z `phase4a_seed_mcp_servers_spec_extension`. |
| 1.0 | 2026-05-20 | Claude Code | Initial seed template transcribing the §8.5 normative contract (Ralph Loop Phase 3, Step 12). All §8.1 required fields present; `check_kind` values drawn from the §8.4 enum. |
