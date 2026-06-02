---
name: btw
description: Operator-issued by-the-way command — scribes pause / bump / status into the live orchestrator state_dir. CC has zero discretion; orchestrator obeys operator intent transcribed by CC. FUP-0815.
---

# /btw — Operator-Command Scribe

**OPERATOR-INITIATED. CC has zero discretion. CC is a SCRIBE, not a COMMANDER.**

The operator issues `\btw <verb> [args]`; CC writes the correctly-shaped command-JSON
into the live initiative's `$STATE_DIR/commands/` directory and reads back the
orchestrator's response. No interpretation, no routing decisions, no policy. The
orchestrator's `lib/command_dispatch.sh` (FUP-0815) consumes the command at the next
iter-boundary and emits a `<command_id>.response.json` sibling.

## Shorthand → command map

| Operator shorthand | Command file written | Schema (validated pre-write) |
|---|---|---|
| `\btw pause` | `pause_<iso8601>.json` | `K:/Claude Code Factory/V3/ralph/schemas/command_pause.schema.json` |
| `\btw bump <usd>` | `bump_budget_<iso8601>.json` | `K:/Claude Code Factory/V3/ralph/schemas/command_bump_budget.schema.json` |
| `\btw status` | `query_register_state_<iso8601>.json` | `K:/Claude Code Factory/V3/ralph/schemas/command_query_register_state.schema.json` |

## Execution (always — no operator confirmation needed for any step)

### 1. DISCOVER the live state_dir

Scan `K:/Claude Code Factory/V3/Project_Docs/Sub_Projects/*/state/state_snapshot.json`
(the live-tree convention per CLAUDE.md v1.27 §File System + active seed v1.5+;
re-resolved from the older FUP-0812 `.orchestrator_runtime/` anchor).

- If **exactly one** match → bind that dir as `$STATE_DIR`.
- If **multiple** matches → list candidate dirs, surface "which initiative?" question via
  `AskUserQuestion`, await operator pick. NEVER silent-pick.
- If **zero** matches → HALT with "no live initiative detected"; do NOT write anything.

**Defensive guard (FUP-0815):** assert the resolved dir contains BOTH `state_snapshot.json`
AND `seed.md`; REJECT any path lacking either (prevents writing into a half-initialised
tree). The discovery test `test_btw_scribe_path_discovery.sh` 7.C / 7.C-inverse asserts this.

**Stale-tree rejection (FUP-0812):** REJECT any candidate path containing the substring
`_orchestrator/` — that pattern is the stale pre-v1.5.1 tree convention and is never the
live state-tree. The discovery test 7.D asserts this.

### 2. WRITE the command-JSON

Compose the JSON per the shorthand→command map. Populate fields:

- `command_id`: `<verb>_<iso8601_utc>` (e.g., `pause_2026-06-02T22-15-00Z`).
- `issued_by`: `"operator-cc-scribe"`.
- `issued_at`: ISO-8601 UTC timestamp (`date -u +%Y-%m-%dT%H:%M:%SZ`).
- Verb-specific fields:
  - `pause`: optional `reason`.
  - `bump_budget`: REQUIRED `new_cap_usd` (positive number); optional `reason`.
  - `query_register_state`: optional `include_fields` array (projection filter).

**Dry-validate BEFORE writing** to `$STATE_DIR/commands/`:

```bash
TMP_INSTANCE="$(mktemp --suffix=.json)"
# compose JSON into $TMP_INSTANCE
bash "K:/Claude Code Factory/V3/ralph/lib/validate_artefact.sh" \
  "K:/Claude Code Factory/V3/ralph/schemas/command_<verb>.schema.json" \
  "$TMP_INSTANCE"
# On non-zero exit → HALT; surface validator stderr; do NOT write to $STATE_DIR/commands/.
```

On validation PASS → `mv "$TMP_INSTANCE" "$STATE_DIR/commands/${command_id}.json"`.

### 3. CONFIRM ingestion

Poll `$STATE_DIR/commands/<command_id>.response.json` once per second for up to 30
seconds. On response present:

- Read `.status` and key `.details` fields.
- Surface to operator:
  - `pause_honored_at_iter_boundary` → "paused at iter N (orchestrator clean-exit)".
  - `deferred_pending_gate_in_flight` → "deferred — gate G-X in flight; re-issue \\btw pause after gate clears" (FUP-0797 BINDING).
  - `budget_override_written` → "budget bumped to $N (effective on next claude -p pre-call)".
  - `register_state_snapshot` → snapshot summary (last iter, budget cap/spent/override, pending_gate).
  - `schema_validation_failed` → validator error (this should NOT happen if step 2 dry-validated correctly).
  - `unknown_command_type` → bug (this should NOT happen if shorthand map is current).

On 30s timeout → surface "no response in 30s — orchestrator may be idle / between iter boundaries; re-run `\btw status` once the next iter boundary fires".

## Files-this-command-may-touch (BOUNDED)

- **WRITE:** `$STATE_DIR/commands/<command_id>.json` (single file per invocation).
- **READ:** `$STATE_DIR/commands/<command_id>.response.json` (poll for response), `$STATE_DIR/state_snapshot.json` (discovery guard), `$STATE_DIR/seed.md` (discovery guard), `K:/Claude Code Factory/V3/Project_Docs/Sub_Projects/*/state/state_snapshot.json` (discovery scan).
- **Nothing else.** No git ops, no DB writes, no edits outside `$STATE_DIR/commands/`.

## Cross-references

- `K:/Claude Code Factory/V3/ralph/lib/command_dispatch.sh` — consumer of the command-JSON; per-command dispatch logic.
- `K:/Claude Code Factory/V3/ralph/schemas/command_*.schema.json` — three contracts the scribe validates against.
- `K:/Claude Code Factory/V3/ralph/tests/test_btw_scribe_path_discovery.sh` — discovery-rule tests (4 sub-cases).
- FUP-0815 — channel spec.
- FUP-0797 — Consumer-confirm BINDING (pause defer rule).
- FUP-0812 — stale `_orchestrator/` tree rejection rule.
- CLAUDE.md v1.27 §File System — live-tree convention authority.
