#!/usr/bin/env bash
# hooks/stop_check.sh <seed_path> <state_dir>   (Initiative_Orchestrator_Spec §13.2)
# Exit 0 — all seed.completion_predicate[] pass (orchestrator terminates clean)
# Exit 1 — at least one check failed (continue)
# Exit 2 — budget exhausted (delegated to budget_check.sh; this hook does not set it)
# Exit ≥3 — error / malformed predicate (HALT)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/seed.sh
source "$SCRIPT_DIR/../lib/seed.sh"

SEED="${1:?usage: stop_check.sh <seed_path> <state_dir>}"
# shellcheck disable=SC2034  # STATE_DIR required by §13.2 hook signature; predicates use seed-absolute paths
STATE_DIR="${2:?usage: stop_check.sh <seed_path> <state_dir>}"

count="$(read_seed_field "$SEED" '.completion_predicate | length')"
if ! [[ "$count" =~ ^[0-9]+$ ]]; then
  echo "stop_check: completion_predicate not an array" >&2; exit 3
fi

all_pass=1
for ((i=0; i<count; i++)); do
  kind="$(read_seed_field "$SEED" ".completion_predicate[$i].check_kind")"
  case "$kind" in
    artefact_exists)
      path="$(read_seed_field "$SEED" ".completion_predicate[$i].params.path")"
      [[ -f "$path" ]] || all_pass=0
      ;;
    registry_zero_open)
      reg="$(read_seed_field "$SEED" ".completion_predicate[$i].params.registry")"
      # convention: an open item is an unchecked GFM task box "- [ ]"
      if [[ ! -f "$reg" ]] || grep -qE '^\s*-\s\[ \]' "$reg"; then all_pass=0; fi
      ;;
    skill_clean|doc_review_clean)
      # Requires a real skill/reviewer invocation (claude -p). Phase 3 MVP authors the
      # dispatch but defers live evaluation to Phase 4; treat as not-yet-passed so the
      # orchestrator never falsely signals INITIATIVE_COMPLETE.
      echo "stop_check: check_kind '$kind' not yet evaluated live (Phase 4) — treating as not-passed" >&2
      all_pass=0
      ;;
    *)
      echo "stop_check: unknown check_kind '$kind' (not in §8.4 enum)" >&2; exit 3
      ;;
  esac
done

[[ "$all_pass" -eq 1 ]] && exit 0 || exit 1
