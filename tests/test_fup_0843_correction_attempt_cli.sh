#!/usr/bin/env bash
# tests/test_fup_0843_correction_attempt_cli.sh — FUP-0843 (part 1) closure regression test.
#
# Validates the `correction_attempt` subcommand of the lib/events.sh CLI — the executor-side
# emit primitive for the L1–L4 correction loop (spec v1.3 §6.2: role=executor, payload
# {attempt, level, item_id}). The cf-correction-agent skill (FUP-0843 part 2) calls this once
# per patch-escalation attempt so §13 Q8 loop-churn (revise_round + correction_attempt) counts
# the correction half. Mirrors the FUP-0838 audit_enter/audit_exit CLI hooks.
#
# Coverage:
#   Check 0 — the correction_attempt case exists in lib/events.sh (guards against removal)
#   Check 1 — in-run emit writes exactly 1 correction_attempt event (role=executor)
#   Check 2 — payload {attempt(numeric), level, item_id} + subject + 9-field §4.1 envelope
#   Check 3 — standalone (RL_STATE_DIR unset) → exit 0, no log (silent no-op, §6.3)
#
# Run: bash tests/test_fup_0843_correction_attempt_cli.sh
# Exit 0 = all PASS; exit 1 = any FAIL.

set -uo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RALPH_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
EVENTS="$RALPH_ROOT/lib/events.sh"

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

pass=0; fail=0; failed_checks=()
check() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then pass=$((pass+1)); echo "  PASS — $desc"
  else fail=$((fail+1)); failed_checks+=("$desc"); echo "  FAIL — $desc" >&2; echo "    expected: $expected" >&2; echo "    actual:   $actual" >&2; fi
}

echo ""
echo "Check 0 — correction_attempt subcommand present in lib/events.sh"
check "case label present" "1" "$(grep -cE '^[[:space:]]*correction_attempt\)' "$EVENTS" 2>/dev/null || echo 0)"

echo ""
echo "Check 1 — in-run emit writes exactly 1 correction_attempt (role=executor)"
RL_STATE_DIR="$SCRATCH" EVENT_PROJECT_ID="rl_test" EVENT_SLUG="rl_test" EVENT_ITER="3" \
  bash "$EVENTS" correction_attempt 2 L3 first-fail
LOG="$SCRATCH/logs/events.jsonl"
check "exactly 1 event emitted" "1" "$(wc -l < "$LOG" | tr -d ' ')"
line="$(tail -1 "$LOG")"
check "event_type == correction_attempt" "correction_attempt" "$(printf '%s' "$line" | jq -r '.event_type')"
check "role == executor" "executor" "$(printf '%s' "$line" | jq -r '.role')"

echo ""
echo "Check 2 — payload + subject + envelope"
check "payload.attempt == 2 (numeric)" "2" "$(printf '%s' "$line" | jq -r '.payload.attempt')"
check "payload.attempt is a JSON number" "number" "$(printf '%s' "$line" | jq -r '.payload.attempt | type')"
check "payload.level == L3" "L3" "$(printf '%s' "$line" | jq -r '.payload.level')"
check "payload.item_id == first-fail" "first-fail" "$(printf '%s' "$line" | jq -r '.payload.item_id')"
check "subject_id == first-fail" "first-fail" "$(printf '%s' "$line" | jq -r '.subject_id')"
check "subject_kind == correction" "correction" "$(printf '%s' "$line" | jq -r '.subject_kind')"
check "9-field §4.1 envelope present" "true" \
      "$(printf '%s' "$line" | jq -e 'has("event_uuid") and has("schema_version") and has("project_id") and has("initiative_slug") and has("iteration_index") and has("role") and has("event_type") and has("ts_utc") and has("payload")' >/dev/null && echo true || echo false)"

echo ""
echo "Check 3 — standalone no-op (RL_STATE_DIR unset)"
NOOP_DIR="$(mktemp -d)"
( unset RL_STATE_DIR; bash "$EVENTS" correction_attempt 1 L1 x ); rc=$?
check "exit 0 when no sink" "0" "$rc"
check "no events.jsonl created outside a sink" "0" "$(find "$NOOP_DIR" -name events.jsonl 2>/dev/null | wc -l | tr -d ' ')"
rm -rf "$NOOP_DIR"

echo ""
echo "============================================"
echo "FUP-0843 CLI test summary: $pass PASS, $fail FAIL"
echo "============================================"
if (( fail > 0 )); then
  echo "FAILED checks:" >&2
  for c in "${failed_checks[@]}"; do echo "  - $c" >&2; done
  exit 1
fi
exit 0
