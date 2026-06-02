#!/usr/bin/env bash
# tests/test_fup_0774_notification_reason.sh — FUP-0774 closure regression test.
#
# Validates that lib/notify.sh dispatch_notification persists the `reason` field from
# context_json to the notifications.log audit entry, so DW assertions like
# `.reason == "answerer_demote"` against notifications.log are verifiable as written.
#
# Before FUP-0774: notify.sh extracted iteration + gate_id from context_json but discarded
# the reason field; the demote signal only lived in execute_with_gates.sh stderr, making
# notifications.log-based DW assertions impossible. After: both surfaces carry the reason.
#
# Coverage:
#   Check 1  — answerer_demote reason persists verbatim to notifications.log
#   Check 2  — broker_classified_gate_human reason persists
#   Check 3  — multiple sequential notifications all preserve their distinct reasons
#   Check 4  — backward-compat: context_json without `reason` -> log entry has `reason: null`
#   Check 5  — log entry schema unchanged otherwise (event/iteration/gate_id/channel_*/ts)
#   Check 6  — gate_id null pattern preserved (the existing FUP-0750 null-marshalling)
#
# Run: bash tests/test_fup_0774_notification_reason.sh
# Exit 0 = all PASS; exit 1 = any FAIL (which checks failed printed to stderr).

set -uo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RALPH_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

# Source the modules under test
# shellcheck source=../lib/seed.sh
source "$RALPH_ROOT/lib/seed.sh"
# shellcheck source=../lib/notify.sh
source "$RALPH_ROOT/lib/notify.sh"

# Build a minimal valid seed with notification_channel resolved to gmail_smtp + skipped env
# (so dispatch_notification reaches the audit-append block but the channel resolves to
# `skipped:env_unset` — that's fine; the audit log still appends per the FR-009 unconditional
# rule, and the reason field test is independent of channel outcome).
TMPSEED="$(mktemp --suffix=.md)"
TMPSTATE="$(mktemp -d)"
trap 'rm -f "$TMPSEED"; rm -rf "$TMPSTATE"' EXIT

cat > "$TMPSEED" << 'EOF'
---
seed_schema_version: 1.4
initiative:
  slug: fup-0774-test
  title: FUP-0774 notify.sh reason persistence test
  owner: test
workspace_root: /tmp/fup-0774-test
read_only_paths: []
mcp_servers: []
state_dir_relative: state
work_registry: /tmp/fup-0774-test/registry.md
context_documents: []
session_shape_catalog: []
verification_bindings: {}
completion_predicate: []
gate_policy:
  pre_classification: []
  confidence_threshold: 0.7
budget:
  iterations_max: 1
  tokens_usd: 1.00
  hang_timeout_seconds: 60
notification_channel:
  primary: gmail_smtp
  primary_smtp_user: nobody@example.com
  primary_to_address: nobody@example.com
  primary_smtp_host: smtp.example.com
  primary_smtp_port: 587
  primary_env_vars:
    smtp_app_password: TEST_FUP_0774_NONEXISTENT_ENV_VAR
  fallback: ""
permission_posture: "--permission-mode auto"
---
EOF

# Ensure the password env var is unset so the channel resolves to skipped:env_unset
# (avoids actually sending email; the audit append still happens regardless).
unset TEST_FUP_0774_NONEXISTENT_ENV_VAR

pass=0
fail=0
failed_checks=()

check() {
  local desc="$1"; local expected="$2"; local actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    pass=$((pass+1))
    echo "  PASS — $desc"
  else
    fail=$((fail+1))
    failed_checks+=("$desc")
    echo "  FAIL — $desc" >&2
    echo "    expected: $expected" >&2
    echo "    actual:   $actual" >&2
  fi
}

LOG_FILE="$TMPSTATE/logs/notifications.log"

# ---- Check 1: answerer_demote reason persists ----
echo ""
echo "Check 1 — answerer_demote reason persists to notifications.log"
dispatch_notification "$TMPSEED" "$TMPSTATE" gate_human \
  "$(jq -nc '{iteration:"0042", gate_id:"G7-demote-test", reason:"answerer_demote"}')"
last_line="$(tail -1 "$LOG_FILE")"
check "log entry contains reason=\"answerer_demote\"" \
      "answerer_demote" \
      "$(printf '%s' "$last_line" | jq -r '.reason')"
check "gate_id preserved alongside reason" \
      "G7-demote-test" \
      "$(printf '%s' "$last_line" | jq -r '.gate_id')"
check "iteration preserved alongside reason" \
      "0042" \
      "$(printf '%s' "$last_line" | jq -r '.iteration')"
check "event field unchanged" \
      "gate_human" \
      "$(printf '%s' "$last_line" | jq -r '.event')"

# ---- Check 2: broker_classified_gate_human reason persists ----
echo ""
echo "Check 2 — broker_classified_gate_human reason persists"
dispatch_notification "$TMPSEED" "$TMPSTATE" gate_human \
  "$(jq -nc '{iteration:"0043", gate_id:"G8-broker-test", reason:"broker_classified_gate_human"}')"
last_line="$(tail -1 "$LOG_FILE")"
check "log entry contains reason=\"broker_classified_gate_human\"" \
      "broker_classified_gate_human" \
      "$(printf '%s' "$last_line" | jq -r '.reason')"

# ---- Check 3: multiple sequential notifications preserve distinct reasons ----
echo ""
echo "Check 3 — sequential notifications preserve distinct reasons"
# Two more events with different reasons, then verify the full log has 4 rows with distinct reasons
dispatch_notification "$TMPSEED" "$TMPSTATE" iteration_failed \
  "$(jq -nc '{iteration:"0044", reason:"max_turns_exceeded"}')"
dispatch_notification "$TMPSEED" "$TMPSTATE" initiative_complete \
  "$(jq -nc '{iteration:"0045", reason:"all_predicates_pass"}')"
# Robust comparison: read reasons as a jq array (handles any whitespace / line-ending variants)
# and compare to expected array semantically rather than via a stringified concat.
all_reasons_json="$(jq -sc '[.[].reason]' "$LOG_FILE" 2>/dev/null)"
expected_json='["answerer_demote","broker_classified_gate_human","max_turns_exceeded","all_predicates_pass"]'
check "all four reasons preserved in order (JSON array compare)" \
      "$expected_json" \
      "$all_reasons_json"

# ---- Check 4: backward-compat — no reason field in context_json ----
echo ""
echo "Check 4 — backward-compat: context_json without reason field"
dispatch_notification "$TMPSEED" "$TMPSTATE" gate_human \
  "$(jq -nc '{iteration:"0046", gate_id:"G9-no-reason"}')"
last_line="$(tail -1 "$LOG_FILE")"
# jq's `// "DEFAULT"` returns the default for null (alternative-operator behavior). Distinguish
# "key present, value null" from "key absent" via has() + null-check.
check "log entry has the reason key present (not absent)" \
      "true" \
      "$(printf '%s' "$last_line" | jq -r 'has("reason")')"
check "log entry reason is JSON null when context_json omits reason" \
      "true" \
      "$(printf '%s' "$last_line" | jq -r '.reason == null')"

# ---- Check 5: log entry schema unchanged otherwise ----
echo ""
echo "Check 5 — log entry schema unchanged"
expected_keys='channel_attempted,channel_result,event,gate_id,iteration,reason,ts'
actual_keys="$(printf '%s' "$last_line" | jq -r 'keys | join(",")')"
check "log entry has 7 expected keys (alphabetised)" \
      "$expected_keys" \
      "$actual_keys"

# ---- Check 6: gate_id null pattern preserved when omitted ----
echo ""
echo "Check 6 — gate_id null marshalling preserved (FUP-0750 compatibility)"
dispatch_notification "$TMPSEED" "$TMPSTATE" budget_exhausted \
  "$(jq -nc '{iteration:"0047", reason:"hard_cap_hit"}')"
last_line="$(tail -1 "$LOG_FILE")"
check "log entry gate_id is JSON null when omitted from context_json" \
      "true" \
      "$(printf '%s' "$last_line" | jq -r '.gate_id == null')"
check "reason still persists when only iteration+reason in context_json" \
      "hard_cap_hit" \
      "$(printf '%s' "$last_line" | jq -r '.reason')"

# ---- Summary ----
echo ""
echo "============================================"
echo "FUP-0774 test summary: $pass PASS, $fail FAIL"
echo "============================================"
if (( fail > 0 )); then
  echo "FAILED checks:" >&2
  for c in "${failed_checks[@]}"; do echo "  - $c" >&2; done
  echo ""
  echo "Last 5 log entries for diagnosis:" >&2
  tail -5 "$LOG_FILE" | jq . >&2 2>/dev/null || tail -5 "$LOG_FILE" >&2
  exit 1
fi

exit 0
