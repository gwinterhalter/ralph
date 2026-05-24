#!/usr/bin/env bash
# hooks/budget_check.sh <seed_path> <state_dir>   (Initiative_Orchestrator_Spec §13.2)
# Exit 0 — within both budgets
# Exit 1 — budget exhausted (orchestrator terminates BUDGET_EXHAUSTED)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/seed.sh
source "$SCRIPT_DIR/../lib/seed.sh"

SEED="${1:?usage: budget_check.sh <seed_path> <state_dir>}"
STATE_DIR="${2:?usage: budget_check.sh <seed_path> <state_dir>}"

ITER_MAX="$(read_seed_field "$SEED" '.budget.iterations_max')"
TOKENS_USD_MAX="$(read_seed_field "$SEED" '.budget.tokens_usd')"

# iterations_max: count iterations/NNNN/ subdirs
iter_count=0
if [[ -d "$STATE_DIR/iterations" ]]; then
  iter_count=$(find "$STATE_DIR/iterations" -maxdepth 1 -type d -regex '.*/[0-9][0-9][0-9][0-9]$' | wc -l | tr -d ' ')
fi
if [[ "$iter_count" -ge "$ITER_MAX" ]]; then
  echo "budget_check: iterations_max reached ($iter_count >= $ITER_MAX)" >&2
  exit 1
fi

# tokens_usd: sum total_cost_usd across each iterations/NNNN/execution_result_NNNN.json
spent="0"
if [[ -d "$STATE_DIR/iterations" ]]; then
  while IFS= read -r f; do
    c="$(jq -r '.total_cost_usd // 0' "$f" 2>/dev/null || echo 0)"
    spent="$(awk -v a="$spent" -v b="$c" 'BEGIN{printf "%.6f", a+b}')"
  done < <(find "$STATE_DIR/iterations" -maxdepth 2 -name 'execution_result_*.json')
fi
over="$(awk -v s="$spent" -v m="$TOKENS_USD_MAX" 'BEGIN{print (s>m)?1:0}')"
if [[ "$over" -eq 1 ]]; then
  echo "budget_check: tokens_usd ceiling exceeded (spent=$spent > max=$TOKENS_USD_MAX)" >&2
  exit 1
fi
exit 0
