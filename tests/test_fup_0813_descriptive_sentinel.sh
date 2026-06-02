#!/usr/bin/env bash
# tests/test_fup_0813_descriptive_sentinel.sh — regression guard for the FUP-0813
# descriptive-sentinel sibling-fallback in stop_check.sh artefact_exists branch.
#
# Part 1 (static): greps stop_check.sh for the FUP-0813 patch anchors — no exec; zero-cost.
# Part 2 (functional): spins up a minimal tmp seed + register + state_dir under tests/.tmp/
# and invokes stop_check.sh end-to-end. Asserts that a descriptive-sentinel `targets_source`
# (no .md extension OR contains whitespace) triggers the sibling-fallback path + that
# stop_check resolves the register via the sibling's `params.path`. Zero LLM cost — the
# test seed only declares registry_zero_open + artefact_exists predicates; no skill_clean /
# doc_review_clean predicates so claude -p is never invoked.
#
# Regression context (commit 5d4a668, 2026-06-02): pre-fix, descriptive `targets_source`
# like "register closure entries" silently failed both exact + FUP-0806 scan-newest lookup
# and emitted `register 'X' not found` → all_pass=0 → stop_check rc=1 → FUP-0797 Consumer-
# confirm gate blocked Path-A INITIATIVE_COMPLETE signals. Observed at run-5 iter-13 of
# auto_build_spec_closure session 2026-05-31 (BLOCKED despite register showing 0 open).

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STOP_CHECK="$RALPH_DIR/hooks/stop_check.sh"
TMP_DIR="$SCRIPT_DIR/.tmp/fup_0813"

fail=0
check() {
  local label="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    echo "  PASS  $label"
  else
    echo "  FAIL  $label"
    eval "$cmd" 2>&1 | sed 's/^/    /' | head -10
    fail=1
  fi
}

echo "[1] Static patch-presence in $STOP_CHECK"
check "file exists"                                        "[[ -f '$STOP_CHECK' ]]"
check "FUP-0813 comment marker present"                    "grep -q 'FUP-0813' '$STOP_CHECK'"
check "descriptive-sentinel heuristic: '!= *.md OR contains space'" \
  "grep -E '\"\\\$targets_source\" != \\*\\.md.*\\|\\|.*\\*\" \"\\*' '$STOP_CHECK' >/dev/null || grep -F '\"\$targets_source\" != *.md' '$STOP_CHECK' >/dev/null"
check "fallback iterates completion_predicate looking for sibling registry_zero_open" \
  "grep -A3 'FUP-0813' '$STOP_CHECK' | grep -q 'registry_zero_open' && grep -A20 'FUP-0813' '$STOP_CHECK' | grep -q 'params.path'"
check "fallback log line cites sibling registry"           "grep -q 'looks descriptive — fell back to sibling registry' '$STOP_CHECK'"

echo ""
echo "[2] Functional black-box — minimal seed + register, no skill_clean predicates"

rm -rf "$TMP_DIR" && mkdir -p "$TMP_DIR/state" "$TMP_DIR/workspace"
REG="$TMP_DIR/workspace/Test_Register_v1.0.md"
SEED="$TMP_DIR/seed.md"

cat > "$REG" << 'REGISTER_EOF'
# Test Register v1.0

| ID | Name | Description | Priority | Prerequisites | Resolution path |
|---|---|---|---|---|---|
| G1 | Test gap | Smoke gap | **RESOLVED** (2026-06-02) | — | Closed inline at §10.4 (no .md token to existence-check; isolates FUP-0813 fallback path) |
REGISTER_EOF

cat > "$SEED" << SEED_EOF
---
seed_schema_version: 1.3
initiative:
  slug: misc
  title: FUP-0813 Smoke Test
  owner: test
workspace_root: "$TMP_DIR/workspace"
state_dir_relative: "../state"
work_registry: "Test_Register.md"
read_only_paths: []
context_documents: []
session_shape_catalog:
  - name: noop
    template_pointer: prompt_key:test.noop
verification_bindings: []
verification_policy: inline_per_session_plan
mcp_servers: []
completion_predicate:
  - name: zero_open_gaps
    check_kind: registry_zero_open
    params:
      path: Test_Register.md
      filter: "status != closed"
  - name: every_artefact_present_descriptive
    check_kind: artefact_exists
    params:
      root_field: workspace_root
      targets_source: "register closure entries"
gate_policy:
  pre_classification: []
  confidence_threshold: 0.7
budget:
  iterations_max: 1
  tokens_usd: 0.10
  hang_timeout_seconds: 60
notification_channel: "wintoast:default"
permission_posture: "--permission-mode auto"
---
# Test seed
SEED_EOF

# Invoke stop_check (will emit warnings + final rc). Capture stderr to inspect.
stderr_file="$TMP_DIR/stop_check.stderr"
set +e
bash "$STOP_CHECK" "$SEED" "$TMP_DIR/state" 2> "$stderr_file"
rc=$?
set -e
echo "  stop_check rc=$rc (expected: 0 — both predicates should pass; artefact_exists via sibling-fallback to Test_Register_v1.0.md which has 1 RESOLVED row + 0 open)"
echo "  --- stderr log lines (relevant) ---"
sed 's/^/    /' "$stderr_file" | head -20

check "stop_check exited 0 (all predicates pass)" "[[ '$rc' == '0' ]]"
check "registry_zero_open resolved Test_Register.md via scan-newest" \
  "grep -q 'zero_open_gaps.*Test_Register.md.*resolved via scan-newest' '$stderr_file'"
check "FUP-0813 descriptive-sentinel fallback fired" \
  "grep -q 'looks descriptive — fell back to sibling registry' '$stderr_file'"
check "fallback resolved to Test_Register_v1.0.md (sibling registry path)" \
  "grep -q 'Test_Register_v1.0.md' '$stderr_file'"
check "no 'register not found' final error emitted" \
  "! grep -q 'register .* not found\$' '$stderr_file'"

# Cleanup
rm -rf "$TMP_DIR"

echo ""
if (( fail == 0 )); then
  echo "ALL CHECKS PASS — FUP-0813 fix present + functional."
  exit 0
else
  echo "ONE OR MORE CHECKS FAILED — see PASS/FAIL lines above."
  exit 1
fi
