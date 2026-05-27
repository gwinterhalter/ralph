#!/usr/bin/env bash
# test_slash_command_discovery.sh — regression test for FUP-0739 + FUP-0740 + FUP-0743/0744/0745 patches
# (claude -p invocation flags for slash-command discovery + MSYS env guards — covers orchestrator.sh
# AND all 3 hook files: plan_review.sh, execute_with_gates.sh, stop_check.sh).
#
# Two parts:
#   1) Static guard (always runs; zero spend) — asserts orchestrator.sh + 3 hook files retain the patch
#      (all 8 claude -p invocation sites carry --add-dir "$CLAUDE_SKILLS_DIR" --).
#   2) Behavioral check (--live; ~$0.05–0.30 spend; requires network + auth) — reproduces
#      Diagnostic 3 from Ralph_Loop_FUP-0717_0718_0737_R4_Gating_Fix_Execution_Report_2026-05-26_v1.0.md
#      §3, asserting `claude -p` resolves /rl-initiative-planner from the ralph/ CWD.
#
# Defect class regression: without --add-dir on the claude -p invocation, the CLI's
# slash-command resolver cannot find the rl-* skills (they live in a sibling tree, not
# an ancestor of ralph/), and `claude -p "/rl-initiative-planner …"` short-circuits with
# result="Unknown command: /rl-initiative-planner" in ~11 ms / 0 turns. Additionally,
# without MSYS_NO_PATHCONV=1 on Git Bash for Windows, the /rl-* prompt argument is
# rewritten to C:/Program Files/Git/rl-initiative-planner and K:-drive paths get
# colon-split into MSYS PATH-list form, mangling the args before claude receives them.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ORCH="$RALPH_ROOT/orchestrator.sh"
HOOK_PLAN_REVIEW="$RALPH_ROOT/hooks/plan_review.sh"
HOOK_EXEC_GATES="$RALPH_ROOT/hooks/execute_with_gates.sh"
HOOK_STOP_CHECK="$RALPH_ROOT/hooks/stop_check.sh"

LIVE=0
for arg in "$@"; do
  case "$arg" in
    --live) LIVE=1 ;;
    *) echo "WARN: unknown arg '$arg' (recognised: --live)" >&2 ;;
  esac
done

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Part 1 — static guard (zero spend)
# ---------------------------------------------------------------------------
echo "[static] checking orchestrator.sh retains FUP-0739 + FUP-0740 patch..."

[[ -f "$ORCH" ]] || fail "orchestrator.sh not found at $ORCH"

grep -qF 'export MSYS_NO_PATHCONV=1' "$ORCH" \
  || fail "FUP-0740 guard 'export MSYS_NO_PATHCONV=1' missing from orchestrator.sh"

grep -qF "export MSYS2_ARG_CONV_EXCL='*'" "$ORCH" \
  || fail "FUP-0740 guard \"export MSYS2_ARG_CONV_EXCL='*'\" missing from orchestrator.sh"

grep -qE '^export[[:space:]]+CLAUDE_SKILLS_DIR=' "$ORCH" \
  || fail "FUP-0739 'export CLAUDE_SKILLS_DIR=...' missing from orchestrator.sh (must be exported so child hooks inherit it under set -u)"

grep -qF -- '--add-dir "$CLAUDE_SKILLS_DIR" --' "$ORCH" \
  || fail "FUP-0739 '--add-dir \"\$CLAUDE_SKILLS_DIR\" --' missing from orchestrator.sh claude -p invocation"

# Negative: the pre-patch invocation form must NOT remain.
if grep -qF -- '--max-budget-usd "$remaining_budget" "$@"' "$ORCH"; then
  fail "pre-patch claude -p form '--max-budget-usd \"\$remaining_budget\" \"\$@\"' (no --add-dir) still present in orchestrator.sh"
fi

# FUP-0743 + FUP-0744 + FUP-0745: hook files must also carry --add-dir on every claude -p site.
# Total claude -p sites by file: orchestrator.sh 1 (patched above), plan_review.sh 2, execute_with_gates.sh 2
# (the gate_dc rl-operator-answerer call PLUS the canonical Executor `claude --print` call which uses
# its own flag set — counted separately), stop_check.sh 4. Static guard: every `claude -p` line in the
# 3 hook files must carry `--add-dir "$CLAUDE_SKILLS_DIR" --`. (The Executor invocation uses
# `claude --print` not `claude -p`; we grep for `^[[:space:]]*claude -p` to scope correctly.)
echo "[static] checking hook files retain FUP-0743/0744/0745 patch..."

check_hook_claude_p_sites() {
  local hook_file="$1"
  local hook_name="$2"
  [[ -f "$hook_file" ]] || fail "$hook_name not found at $hook_file"
  # Each `claude -p` invocation may span multiple lines via backslash continuation; join
  # continued lines first, then verify --add-dir is present in every joined invocation.
  # awk: at each line starting (after optional whitespace) with `claude -p `, accumulate
  # the line; if it ends with `\`, append the next line; emit the joined invocation; check.
  local violations
  violations=$(awk '
    /^[[:space:]]*claude -p / {
      joined = $0
      while (joined ~ /\\$/) {
        sub(/\\$/, "", joined)
        if ((getline next_line) <= 0) break
        joined = joined " " next_line
      }
      if (joined !~ /--add-dir/) {
        printf("  line %d: %s\n", NR, $0)
      }
    }
  ' "$hook_file")
  if [[ -n "$violations" ]]; then
    echo "$violations" >&2
    fail "$hook_name has claude -p invocations without --add-dir (FUP-0743/0744/0745 patch missing)"
  fi
}

check_hook_claude_p_sites "$HOOK_PLAN_REVIEW" "plan_review.sh"
check_hook_claude_p_sites "$HOOK_EXEC_GATES"  "execute_with_gates.sh"
check_hook_claude_p_sites "$HOOK_STOP_CHECK"  "stop_check.sh"

echo "[static] PASS — all patch markers present + pre-patch form absent (orchestrator.sh + 3 hook files)"

# ---------------------------------------------------------------------------
# Part 2 — behavioral check (--live only)
# ---------------------------------------------------------------------------
if [[ "$LIVE" -ne 1 ]]; then
  echo "[behavioral] skipped (pass --live to run; ~\$0.05-0.30 spend, requires auth)"
  exit 0
fi

command -v claude >/dev/null 2>&1 || fail "claude CLI not on PATH"
command -v jq     >/dev/null 2>&1 || fail "jq not on PATH"

CLAUDE_SKILLS_DIR="${CLAUDE_SKILLS_DIR:-K:/Claude Code Factory/V3/Project_Docs}"

echo "[behavioral] invoking claude -p from $RALPH_ROOT with --add-dir $CLAUDE_SKILLS_DIR ..."

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

cd "$RALPH_ROOT"

MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
  claude -p --output-format json --max-budget-usd 0.05 \
    --add-dir "$CLAUDE_SKILLS_DIR" -- "/rl-initiative-planner --version-info" \
  > "$TMP" || true   # the --max-budget-usd cap may trip is_error=true; that's fine

RESULT="$(jq -r '.result // ""' "$TMP")"

if [[ "$RESULT" == *"Unknown command"* ]]; then
  echo "[behavioral] FAIL — claude -p returned 'Unknown command' (slash-command discovery still broken):"
  echo "  $RESULT" | head -c 400
  echo ""
  exit 1
fi

echo "[behavioral] PASS — /rl-initiative-planner resolved (no 'Unknown command' in result)"
exit 0
