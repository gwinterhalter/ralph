#!/usr/bin/env bash
# hooks/plan_review.sh <plan_path>   (Initiative_Orchestrator_Spec §13.2)
# Orchestrates the cf-session-plan-reviewer + rl-initiative-planner --revise inner loop, <=5 rounds.
# Exit 0 — reviewer returned zero findings within 5 rounds
# Exit 1 — did not converge in 5 rounds; escalation file written; orchestrator should block on gate_human
set -euo pipefail

PLAN_PATH="${1:?usage: plan_review.sh <plan_path>}"
ITER_DIR="$(dirname "$PLAN_PATH")"
STATE_DIR="$(dirname "$(dirname "$ITER_DIR")")"   # iterations/NNNN -> state_dir

for round in 1 2 3 4 5; do
  FINDINGS="$ITER_DIR/review_findings_${round}.json"
  # Invoke cf-session-plan-reviewer; capture findings JSON.
  claude -p "/cf-session-plan-reviewer $PLAN_PATH" > "$FINDINGS"
  # Zero findings -> converged.
  if jq -e '.findings | length == 0' "$FINDINGS" >/dev/null 2>&1; then
    exit 0
  fi
  # Else invoke planner --revise to produce a revised plan (overwrites $PLAN_PATH).
  claude -p "/rl-initiative-planner --revise $FINDINGS" > /dev/null
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
