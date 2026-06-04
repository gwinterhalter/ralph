#!/usr/bin/env bash
# tests/test_fup_0844_iteration_failed_recoverable.sh — FUP-0844 closure regression test.
#
# Validates that orchestrator.sh emits an `iteration_failed` event for a RECOVERABLE
# Consumer-side inline-verification failure — an item the Consumer marked failed THIS
# iteration (incremented fail_counts via the inline_per_session_plan closure check) that
# stayed BELOW the >=3 HALT threshold, so the gap remains open and retries on the normal
# iteration_end path.
#
# Before FUP-0844: orchestrator.sh wired iteration_failed only to orchestrator-level HALT/
# escalate branches (budget-exhaust, no-plan, execute_with_gates exit-1, read-only,
# fail_counts>=3). The recoverable <3 failure took the normal path and emitted NOTHING, so
# real per-reason failures were invisible to events.jsonl and the spec v1.3 §13 Q6
# failure-rate-by-reason analytics. Empirically surfaced by the RL_Test v1.5 full-event-type
# drain (T6 cluster:first-fail): fail_counts.first-fail=1 at iter-0006, 0 iteration_failed
# events in the log. After: the fail_counts guard's else-branch emits one iteration_failed
# (reason=inline_closure_verification_failed) per item whose most-recent failure is this
# iteration and whose count is <3.
#
# This test extracts the LITERAL emit block from the committed orchestrator.sh at runtime
# (so it exercises the real code, not a copy) and replays it against fixtures.
#
# Coverage:
#   Check 0  — the FUP-0844 emit block is present in orchestrator.sh (guards against removal)
#   Check 1  — a recoverable <3 failure recorded THIS iteration emits exactly 1 iteration_failed
#   Check 2  — the emitted event carries the correct reason / item_id / fail_count + 9-field envelope
#   Check 3  — at the RECOVERY iteration (no new failure this iter) → 0 emits (no double-fire)
#   Check 4  — a stale failure from a PRIOR iteration is NOT re-emitted this iteration
#   Check 5  — a >=3 item is NOT emitted by this branch (the >=3 HALT path owns it; filter is count<3)
#   Check 6  — numeric iteration compare tolerates the 0006-vs-6 zero-pad mismatch (both directions)
#
# Run: bash tests/test_fup_0844_iteration_failed_recoverable.sh
# Exit 0 = all PASS; exit 1 = any FAIL (which checks failed printed to stderr).

set -uo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RALPH_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
ORCH="$RALPH_ROOT/orchestrator.sh"

# shellcheck source=../lib/events.sh
source "$RALPH_ROOT/lib/events.sh"

SCRATCH="$(mktemp -d)"
BLOCKFILE="$(mktemp)"
trap 'rm -rf "$SCRATCH" "$BLOCKFILE"' EXIT
mkdir -p "$SCRATCH/logs"

# Extract the LITERAL committed emit block (the while…done loop) from orchestrator.sh by its
# stable anchors. If the markers ever change, Check 0 fails loudly rather than silently passing.
sed -n '/while IFS=.*read -r _fc_item/,/done < <(jq -r --arg it/p' "$ORCH" > "$BLOCKFILE"

pass=0
fail=0
failed_checks=()

check() {
  local desc="$1"; local expected="$2"; local actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    pass=$((pass+1)); echo "  PASS — $desc"
  else
    fail=$((fail+1)); failed_checks+=("$desc")
    echo "  FAIL — $desc" >&2
    echo "    expected: $expected" >&2
    echo "    actual:   $actual" >&2
  fi
}

LOG="$SCRATCH/logs/events.jsonl"

# Replay the committed block in-process against a fixture at a given iteration.
run_block() {  # args: <iter> <fixture_path>
  : > "$LOG"
  STATE_DIR="$SCRATCH"; EVENT_PROJECT_ID="rl_test"; EVENT_SLUG="rl_test"
  ITER="$1"; FAIL_COUNTS_FILE="$2"
  # shellcheck source=/dev/null
  source "$BLOCKFILE"
}

emit_count() { wc -l < "$LOG" | tr -d ' '; }

# ---- Check 0: the emit block is present (regression guard against removal/refactor) ----
echo ""
echo "Check 0 — FUP-0844 emit block present in orchestrator.sh"
block_lines="$(grep -c 'inline_closure_verification_failed' "$BLOCKFILE" 2>/dev/null || echo 0)"
check "extracted block references reason=inline_closure_verification_failed" "1" "$block_lines"
check "extracted block calls emit_event with iteration_failed" \
      "1" "$(grep -c 'emit_event .* "iteration_failed"' "$BLOCKFILE" 2>/dev/null || echo 0)"

# ---- Check 1: recoverable <3 failure recorded THIS iteration → exactly 1 emit ----
echo ""
echo "Check 1 — recoverable <3 failure this iteration emits exactly 1 iteration_failed"
FIX1="$SCRATCH/fc1.json"
cat > "$FIX1" <<'JSON'
[{"item_id":"first-fail","count":1,"last_failure_iteration":"0006","last_reason":"inline closure content-gate FAILED: T6-PENDING required T6-RECOVERED"}]
JSON
run_block "0006" "$FIX1"
check "exactly 1 iteration_failed emitted" "1" "$(emit_count)"

# ---- Check 2: correct payload + 9-field envelope ----
echo ""
echo "Check 2 — emitted event payload + envelope correct"
line="$(tail -1 "$LOG")"
check "event_type == iteration_failed" "iteration_failed" "$(printf '%s' "$line" | jq -r '.event_type')"
check "role == orchestrator"           "orchestrator"     "$(printf '%s' "$line" | jq -r '.role')"
check "iteration_index == 6 (numeric)" "6"                "$(printf '%s' "$line" | jq -r '.iteration_index')"
check "payload.reason == inline_closure_verification_failed" "inline_closure_verification_failed" \
      "$(printf '%s' "$line" | jq -r '.payload.reason')"
check "payload.item_id == first-fail"  "first-fail"       "$(printf '%s' "$line" | jq -r '.payload.item_id')"
check "payload.fail_count == 1 (number)" "1"              "$(printf '%s' "$line" | jq -r '.payload.fail_count')"
check "9-field §4.1 envelope present" "true" \
      "$(printf '%s' "$line" | jq -e 'has("event_uuid") and has("schema_version") and has("project_id") and has("initiative_slug") and has("iteration_index") and has("role") and has("event_type") and has("ts_utc") and has("payload")' >/dev/null && echo true || echo false)"

# ---- Check 3: recovery iteration (no new failure this iter) → 0 emits ----
echo ""
echo "Check 3 — recovery iteration emits 0 (no double-fire)"
run_block "0007" "$FIX1"
check "0 iteration_failed at the recovery iteration" "0" "$(emit_count)"

# ---- Check 4: stale prior-iteration failure not re-emitted this iteration ----
echo ""
echo "Check 4 — stale prior-iteration item not re-emitted"
FIX4="$SCRATCH/fc4.json"
cat > "$FIX4" <<'JSON'
[{"item_id":"first-fail","count":1,"last_failure_iteration":"0006","last_reason":"this-iter"},
 {"item_id":"other","count":2,"last_failure_iteration":"0004","last_reason":"stale prior failure"}]
JSON
run_block "0006" "$FIX4"
check "exactly 1 emit (only the current-iteration item)" "1" "$(emit_count)"
check "the emitted item is first-fail (not the stale 'other')" "first-fail" \
      "$(tail -1 "$LOG" | jq -r '.payload.item_id')"

# ---- Check 5: >=3 item not emitted by this (recoverable) branch ----
echo ""
echo "Check 5 — >=3 item not emitted here (count<3 filter; >=3 HALT path owns it)"
FIX5="$SCRATCH/fc5.json"
cat > "$FIX5" <<'JSON'
[{"item_id":"hard-fail","count":3,"last_failure_iteration":"0006","last_reason":"threshold case"}]
JSON
run_block "0006" "$FIX5"
check "0 emits for a count>=3 item in the recoverable branch" "0" "$(emit_count)"

# ---- Check 6: zero-pad numeric compare tolerated (both directions) ----
echo ""
echo "Check 6 — 0006-vs-6 numeric iteration compare"
FIX6="$SCRATCH/fc6.json"
cat > "$FIX6" <<'JSON'
[{"item_id":"zp","count":1,"last_failure_iteration":"6","last_reason":"unpadded in file"}]
JSON
run_block "0006" "$FIX6"
check "padded ITER 0006 matches unpadded last_failure_iteration 6" "1" "$(emit_count)"
cat > "$FIX6" <<'JSON'
[{"item_id":"zp","count":1,"last_failure_iteration":"0006","last_reason":"padded in file"}]
JSON
run_block "6" "$FIX6"
check "unpadded ITER 6 matches padded last_failure_iteration 0006" "1" "$(emit_count)"

# ---- Summary ----
echo ""
echo "============================================"
echo "FUP-0844 test summary: $pass PASS, $fail FAIL"
echo "============================================"
if (( fail > 0 )); then
  echo "FAILED checks:" >&2
  for c in "${failed_checks[@]}"; do echo "  - $c" >&2; done
  exit 1
fi

exit 0
