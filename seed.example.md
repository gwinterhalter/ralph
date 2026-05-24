---
seed_schema_version: 1.1
initiative:
  slug: example-initiative
  title: Example Initiative
  owner: gwinterhalter
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
  confidence_threshold: 0.7
budget:
  iterations_max: 20
  tokens_usd: 50.00
  hang_timeout_seconds: 1800
notification_channel: "wintoast:default"
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
| `seed_schema_version` | yes | Schema format version; orchestrator refuses a seed whose major exceeds its supported major (§8.3). Current: `1.1`. |
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
| `gate_policy.pre_classification[]` | yes | Patterns (per §10.2 DSL) that pre-class a gate as `gate_human`. |
| `gate_policy.confidence_threshold` | yes | Float in [0,1]; default `0.7`. |
| `budget.iterations_max` / `.tokens_usd` / `.hang_timeout_seconds` | yes | Iteration ceiling; USD ceiling; per-Executor no-progress timeout. |
| `notification_channel` | yes | Declarative target for the notification one-liner. |
| `permission_posture` | yes | Exact flag set passed to the Executor. Default `--permission-mode auto`; fallback `--permission-mode acceptEdits --dangerously-skip-permissions`. |

Optional fields (§8.2): `target_order[]`, `initiative.description`, `initiative.target_completion_estimate`.

## `completion_predicate[].check_kind` enum (§8.4)

`registry_zero_open` (registry has no open items) · `artefact_exists` (a named file exists) · `skill_clean` (a named skill run returns clean against a target) · `doc_review_clean` (cf-doc-reviewer returns zero findings on a named doc).

## Change History

| Version | Date | Author | Summary |
|---------|------|--------|---------|
| 1.0 | 2026-05-20 | Claude Code | Initial seed template transcribing the §8.5 normative contract (Ralph Loop Phase 3, Step 12). All §8.1 required fields present; `check_kind` values drawn from the §8.4 enum. |
| 1.1 | 2026-05-23 | Claude Code | Phase 4a (P4-01): added `mcp_servers[]` frontmatter block + body field-reference row. Per-server shape `{name, command, args, env}` consumed by the new `generate_mcp_config` hook in `execute_with_gates.sh`. This is a §8.1 extension to `Initiative_Orchestrator_Spec_v1_4.md`; surfaced as Spec v1.5 candidate via Phase Z `phase4a_seed_mcp_servers_spec_extension`. |
