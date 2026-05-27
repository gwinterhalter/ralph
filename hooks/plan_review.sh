#!/usr/bin/env bash
# hooks/plan_review.sh <plan_path>   (Initiative_Orchestrator_Spec §13.2)
# Orchestrates the cf-session-plan-reviewer + rl-initiative-planner --revise inner loop, <=5 rounds.
# Exit 0 — reviewer returned zero findings within 5 rounds
# Exit 1 — did not converge in 5 rounds; escalation file written; orchestrator should block on gate_human
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/seed.sh
source "$SCRIPT_DIR/../lib/seed.sh"

PLAN_PATH="${1:?usage: plan_review.sh <plan_path>}"
ITER_DIR="$(dirname "$PLAN_PATH")"
STATE_DIR="$(dirname "$(dirname "$ITER_DIR")")"   # iterations/NNNN -> state_dir

# FUP-0720 cost instrumentation: read budget cap from seed; default to 20 USD on failure.
SEED_PATH="$STATE_DIR/seed.md"
BUDGET_CAP=$(read_seed_field "$SEED_PATH" .budget.tokens_usd 2>/dev/null || echo 20)
[[ -z "$BUDGET_CAP" || "$BUDGET_CAP" == "null" ]] && BUDGET_CAP=20

for round in 1 2 3 4 5; do
  FINDINGS="$ITER_DIR/review_findings_${round}.json"
  RESULT_TEXT="$ITER_DIR/review_result_${round}.txt"
  # Invoke cf-session-plan-reviewer; capture JSON envelope via --output-format json. FUP-0720.
  # FUP-0743: --add-dir "$CLAUDE_SKILLS_DIR" -- required so the slash command resolves from
  # the ralph/ CWD (skills live in a SIBLING tree); env var exported by orchestrator.sh.
  claude -p --output-format json --max-budget-usd "$BUDGET_CAP" \
    --add-dir "$CLAUDE_SKILLS_DIR" -- "/cf-session-plan-reviewer $PLAN_PATH" > "$FINDINGS"
  # Extract .result field for convergence regex (newlines unescaped) — FUP-0720.
  jq -r '.result // empty' "$FINDINGS" > "$RESULT_TEXT"
  # Converged when the reviewer emits its completion block reporting zero BLOCKER
  # and zero DRIFT findings. cf-session-plan-reviewer Delivery Format contract:
  # a "## Session Plan Review Complete" header followed by a
  # "- Findings: N BLOCKER, N DRIFT, N COSMETIC" summary line. COSMETIC findings
  # do NOT block convergence (reviewer Step 6 severity model). FUP-0716 / FUP-0719.
  if grep -qF '## Session Plan Review Complete' "$RESULT_TEXT" \
     && grep -qE '^-[[:space:]]*Findings:[[:space:]]*0[[:space:]]+BLOCKER,[[:space:]]*0[[:space:]]+DRIFT' "$RESULT_TEXT"; then
    exit 0
  fi
  # Else invoke planner --revise to produce a revised plan (overwrites $PLAN_PATH).
  # FUP-0743: --add-dir + -- (same rationale as the reviewer call above).
  claude -p --output-format json --max-budget-usd "$BUDGET_CAP" \
    --add-dir "$CLAUDE_SKILLS_DIR" -- "/rl-initiative-planner --revise $FINDINGS" > "$ITER_DIR/revise_round_${round}.json"
done

# Did not converge — write escalation (§13.2 exit 1).
mkdir -p "$STATE_DIR/escalations"
ESCALATION="$STATE_DIR/escalations/plan_review_nonconvergence_$(basename "$ITER_DIR").md"
cat > "$ESCALATION" <<EOF
# Plan review non-convergence

Plan: $PLAN_PATH
Did not converge after 5 cf-session-plan-reviewer rounds.
Final findings: $ITER_DIR/review_findings_5.json
Action: operator gate_human decision required.
EOF
exit 1
