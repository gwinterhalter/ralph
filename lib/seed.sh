#!/usr/bin/env bash
# lib/seed.sh — single canonical seed-field accessor (Initiative_Orchestrator_Spec §8.5 step 5).
# Contract: read_seed_field <seed_path> <yq_expr>
#   Returns the value of the named YAML frontmatter field, stripped.
#   Frontmatter is delimited by the first two '---' lines; the markdown body is ignored.
#   Frontmatter is canonical on every conflict (§8.5 parse contract step 2).
# Uses yq (>=4.0). If yq is unavailable, substitute a python-yaml shim (plan §9 row 5)
#   and keep this function's contract identical.

set -euo pipefail

read_seed_field() {
  local seed_path="$1"
  local yq_expr="$2"
  if [[ ! -f "$seed_path" ]]; then
    echo "read_seed_field: seed file not found: $seed_path" >&2
    return 2
  fi
  # Extract frontmatter (lines between the first and second '---')
  local fm
  fm=$(awk '/^---[[:space:]]*$/{c++; next} c==1{print} c==2{exit}' "$seed_path")
  if [[ -z "$fm" ]]; then
    echo "read_seed_field: no frontmatter found in $seed_path" >&2
    return 3
  fi
  printf '%s\n' "$fm" | yq -r "$yq_expr"
}
