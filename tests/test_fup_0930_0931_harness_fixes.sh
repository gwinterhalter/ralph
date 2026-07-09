#!/usr/bin/env bash
# FUP-0930 + FUP-0931 harness fixes.
#   0930 — execute_with_gates report-recovery net fires for EVERY non-noop shape (was a hardcoded
#          allowlist). Verified behaviourally (case-selection) + statically (real file structure).
#   0931 — orchestrator.sh bootstraps initiative_narrative.md (required Planner+Consumer input) when
#          absent, and does NOT clobber an existing one. Verified by running the REAL orchestrator
#          with a mock `claude` (zero real spend).
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
pass=0; fail=0; failed=()
check() { # desc expected actual
  if [[ "$2" == "$3" ]]; then pass=$((pass+1)); echo "  PASS — $1";
  else fail=$((fail+1)); failed+=("$1"); echo "  FAIL — $1" >&2; echo "    expected: $2" >&2; echo "    actual:   $3" >&2; fi
}

echo "===== FUP-0930 — report-recovery fires for every non-noop shape ====="

# (a) Behavioural: the EXACT case logic now in execute_with_gates.sh (""|noop -> skip; * -> recover).
branch_for() { case "$1" in ""|noop) echo skip ;; *) echo recover ;; esac; }
for s in spec_bump skill_build skill_audit migration_author decision_q_block spec_review_loop \
         component_build integration_checkpoint doc_stub a_brand_new_custom_shape; do
  check "shape '$s' enters the recovery branch" "recover" "$(branch_for "$s")"
done
check "shape 'noop' is exempt (no report expected)" "skip" "$(branch_for noop)"
check "empty shape is exempt" "skip" "$(branch_for "")"

# (b) Static: the real file no longer hardcodes the allowlist and now has the noop-exclusion + catch-all.
EWG="$RALPH_ROOT/hooks/execute_with_gates.sh"
check "execute_with_gates.sh has the '\"\"|noop)' skip branch" "true" \
  "$(grep -qE '^\s*""\|noop\)' "$EWG" && echo true || echo false)"
check "execute_with_gates.sh recovery case has a '*)' catch-all" "true" \
  "$(awk '/^case "\$_plan_shape" in/{f=1} f&&/^\s*\*\)/{print "y"; exit}' "$EWG" | grep -q y && echo true || echo false)"
check "execute_with_gates.sh no longer gates recovery on the old hardcoded allowlist line" "true" \
  "$(grep -qE '^\s*component_build\|integration_checkpoint\|skill_build' "$EWG" && echo false || echo true)"

echo ""
echo "===== FUP-0931 — orchestrator bootstraps initiative_narrative.md ====="

WORK="$(mktemp -d 2>/dev/null || echo "/tmp/ff0931_$$")"; mkdir -p "$WORK"
TMPBIN="$WORK/bin"; mkdir -p "$TMPBIN"
# Mock claude: any invocation returns a minimal valid JSON envelope and exits 0 (no real API call).
cat > "$TMPBIN/claude" <<'STUB'
#!/usr/bin/env bash
echo '{"result":"INITIATIVE_COMPLETE","session_id":"stub-session","total_cost_usd":0,"permission_denials":[],"is_error":false}'
exit 0
STUB
chmod +x "$TMPBIN/claude"

WS="$WORK/ws"; mkdir -p "$WS"
cat > "$WS/registry.md" <<'REG'
# Registry
| ID | Name | Gap description | Priority | Prerequisites | Resolution path |
|---|---|---|---|---|---|
| T-01 | item | stub | **P1** | (none) | artifacts/x.md |
REG
SEED="$WS/seed.md"
cat > "$SEED" <<SEEDDOC
---
seed_schema_version: 1.4
initiative:
  slug: fup0931_test
  title: FUP-0931 bootstrap test
  owner: test
workspace_root: "$WS"
read_only_paths: []
writable_paths: ["$WS"]
mcp_servers: []
state_dir_relative: "state"
work_registry: "registry.md"
context_documents: []
session_shape_catalog:
  - name: doc_stub
    template_pointer: "x"
verification_bindings:
  doc_stub: []
completion_predicate:
  - name: zero_open
    check_kind: registry_zero_open
    params:
      path: "$WS/registry.md"
gate_policy:
  pre_classification: []
  confidence_threshold: 0.7
budget:
  iterations_max: 1
  tokens_usd: 1.00
  hang_timeout_seconds: 60
notification_channel: "wintoast:default"
permission_posture: "--permission-mode auto"
---
body
SEEDDOC

run_orch() { # runs the REAL orchestrator with the mock claude; rc ignored (we assert on state)
  timeout 45 env PATH="$TMPBIN:$PATH" CLAUDE_SKILLS_DIR="/tmp/none" RALPH_DISABLE_DESKTOP_TOAST=1 \
    bash "$RALPH_ROOT/orchestrator.sh" "$SEED" >/dev/null 2>&1 || true
}

# --- Fresh launch: narrative must be seeded at bootstrap ---
run_orch
NARR="$WS/state/initiative_narrative.md"
check "initiative_narrative.md was seeded on a fresh launch" "true" "$([[ -f "$NARR" ]] && echo true || echo false)"
check "seeded narrative has the '## Iteration summaries' header the Planner reads" "true" \
  "$(grep -q '## Iteration summaries' "$NARR" 2>/dev/null && echo true || echo false)"
check "seeded narrative has the '## fail_counts' tail block" "true" \
  "$(grep -q '## fail_counts' "$NARR" 2>/dev/null && echo true || echo false)"
check "bootstrap logged the FUP-0931 seed line" "true" \
  "$(grep -q 'seeded initiative_narrative.md skeleton (FUP-0931)' "$WS/state/logs/orchestrator.log" 2>/dev/null && echo true || echo false)"

# --- Idempotency: an existing narrative must NOT be clobbered ---
rm -rf "$WS/state"; mkdir -p "$WS/state"
printf 'MARKER_KEEP_ME\n' > "$WS/state/initiative_narrative.md"
run_orch
check "existing narrative preserved (not overwritten by bootstrap)" "true" \
  "$(grep -q 'MARKER_KEEP_ME' "$WS/state/initiative_narrative.md" 2>/dev/null && echo true || echo false)"

rm -rf "$WORK" 2>/dev/null || true

echo ""
echo "=== test_fup_0930_0931_harness_fixes.sh: $pass PASS, $fail FAIL ==="
[[ $fail -eq 0 ]] || { printf 'FAILED: %s\n' "${failed[@]}" >&2; exit 1; }
exit 0
