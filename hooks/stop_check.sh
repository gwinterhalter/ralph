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
      # Phase 4b P-#3: recursive-find-by-filename under the seed field named in params.root_field
      # (e.g. workspace_root). Closure-entry Resolution-path cells of the register may name many
      # token types (spec §-numbers, IMP IDs, etc.); we only existence-check tokens matching
      # \S+\.md (closure cells often name resolvable doc paths but also non-file tokens). The
      # register itself is identified via params.targets_source; if absent, fall back to the first
      # registry_zero_open predicate's params.path.
      root_field="$(read_seed_field "$SEED" ".completion_predicate[$i].params.root_field")"
      if [[ -z "$root_field" || "$root_field" == "null" ]]; then
        echo "stop_check: artefact_exists missing params.root_field" >&2; exit 3
      fi
      workspace_root="$(read_seed_field "$SEED" ".$root_field")"
      if [[ -z "$workspace_root" || "$workspace_root" == "null" || ! -d "$workspace_root" ]]; then
        echo "stop_check: artefact_exists root_field='$root_field' resolves to '$workspace_root' (not a directory)" >&2; exit 3
      fi
      targets_source="$(read_seed_field "$SEED" ".completion_predicate[$i].params.targets_source" 2>/dev/null || echo "")"
      if [[ -z "$targets_source" || "$targets_source" == "null" ]]; then
        for ((k=0; k<count; k++)); do
          if [[ "$(read_seed_field "$SEED" ".completion_predicate[$k].check_kind")" == "registry_zero_open" ]]; then
            targets_source="$(read_seed_field "$SEED" ".completion_predicate[$k].params.path")"
            break
          fi
        done
      fi
      if [[ -z "$targets_source" || "$targets_source" == "null" ]]; then
        echo "stop_check: artefact_exists has no params.targets_source and no sibling registry path to derive from" >&2; exit 3
      fi
      register_path=""
      if [[ -f "$targets_source" ]]; then
        register_path="$targets_source"
      else
        register_path="$(find "$workspace_root" "$SCRIPT_DIR/.." -type f -name "$targets_source" 2>/dev/null | head -1)"
      fi
      if [[ -z "$register_path" || ! -f "$register_path" ]]; then
        echo "stop_check: artefact_exists targets_source register '$targets_source' not found" >&2
        all_pass=0
      else
        missing_count=0
        while IFS= read -r line; do
          line="${line%$'\r'}"
          [[ "$line" == \|* ]] || continue
          IFS='|' read -ra cells <<< "$line"
          [[ ${#cells[@]} -ge 7 ]] || continue
          priority="${cells[4]}"
          priority="${priority#"${priority%%[![:space:]]*}"}"
          priority="${priority%"${priority##*[![:space:]]}"}"
          [[ "$priority" == "**RESOLVED**"* ]] || continue
          resolution="${cells[6]}"
          while read -r token; do
            [[ -z "$token" ]] && continue
            found="$(find "$workspace_root" -type f -name "$(basename "$token")" 2>/dev/null | head -1)"
            if [[ -z "$found" ]]; then
              missing_count=$((missing_count+1))
            fi
          done < <(echo "$resolution" | grep -oE '[A-Za-z0-9_./-]+\.md' || true)
        done < "$register_path"
        [[ "$missing_count" -eq 0 ]] || all_pass=0
      fi
      ;;
    registry_zero_open)
      # Phase 4b P-#1/#2: read params.path (NOT params.registry — was a key drift bug); branch on
      # predicate name to differentiate #1 zero_open_gaps (open Priority count) vs
      # #2 every_closure_cites_iteration (RESOLVED rows missing iteration token in Resolution-path).
      # Parses Auto_Build_Gap_Register-style tables with columns:
      #   | ID | Name | Gap description | Priority | Prerequisites | Resolution path |
      # CRLF-tolerant; trims leading/trailing whitespace on Priority cell.
      register_rel="$(read_seed_field "$SEED" ".completion_predicate[$i].params.path")"
      pred_name="$(read_seed_field "$SEED" ".completion_predicate[$i].name")"
      if [[ -z "$register_rel" || "$register_rel" == "null" ]]; then
        echo "stop_check: registry_zero_open predicate '$pred_name' missing params.path" >&2; exit 3
      fi
      if [[ -f "$register_rel" ]]; then
        register_path="$register_rel"
      else
        workspace_root="$(read_seed_field "$SEED" '.workspace_root')"
        register_path="$(find "$workspace_root" "$SCRIPT_DIR/.." -type f -name "$register_rel" 2>/dev/null | head -1)"
      fi
      if [[ -z "$register_path" || ! -f "$register_path" ]]; then
        echo "stop_check: registry_zero_open predicate '$pred_name' register '$register_rel' not found" >&2
        all_pass=0
      else
        case "$pred_name" in
          zero_open_gaps)
            open_count=0
            while IFS= read -r line; do
              line="${line%$'\r'}"
              [[ "$line" == \|* ]] || continue
              IFS='|' read -ra cells <<< "$line"
              [[ ${#cells[@]} -ge 5 ]] || continue
              priority="${cells[4]}"
              priority="${priority#"${priority%%[![:space:]]*}"}"
              priority="${priority%"${priority##*[![:space:]]}"}"
              if [[ "$priority" == "**P1**" || "$priority" == "**P2**" || "$priority" == "**P3**" ]]; then
                open_count=$((open_count+1))
              fi
            done < "$register_path"
            [[ "$open_count" -eq 0 ]] || all_pass=0
            ;;
          every_closure_cites_iteration)
            missing_count=0
            while IFS= read -r line; do
              line="${line%$'\r'}"
              [[ "$line" == \|* ]] || continue
              IFS='|' read -ra cells <<< "$line"
              [[ ${#cells[@]} -ge 7 ]] || continue
              priority="${cells[4]}"
              priority="${priority#"${priority%%[![:space:]]*}"}"
              priority="${priority%"${priority##*[![:space:]]}"}"
              [[ "$priority" == "**RESOLVED**"* ]] || continue
              resolution="${cells[6]}"
              if ! echo "$resolution" | grep -qE 'iteration [0-9]{4}|iter:[0-9]{4}'; then
                missing_count=$((missing_count+1))
              fi
            done < "$register_path"
            [[ "$missing_count" -eq 0 ]] || all_pass=0
            ;;
          *)
            echo "stop_check: registry_zero_open predicate '$pred_name' has no known sub-evaluator (expected: zero_open_gaps, every_closure_cites_iteration)" >&2
            exit 3
            ;;
        esac
      fi
      ;;
    skill_clean|doc_review_clean)
      # Phase 4b P4-02 / FUP-0641: per-skill clean-marker map. Markers DERIVED from each
      # skill's SKILL.md output spec at implement time (operator-confirmed 2026-05-24 via
      # plan §8.3 operator-choice; "output-presence detection" path). Map:
      #   cf-doc-reviewer (\fix2):     stdout grep "All findings resolved: YES"
      #   cf-corpus-auditor:           REPORT file has no `^|` table rows (append-only output spec)
      #   cf-cross-reference-audit:    Issues file matches "Coverage summary: 100%" OR has no `^|` rows
      #   cf-skill-reviewer (mode A):  stdout grep "Recommended Action:.*KEEP"
      # Each branch keys on predicate name (independent evaluation per #4-#7). Missing/failed
      # marker → all_pass=0 (continue; never silent pass). Unknown predicate-name → exit 3 (HALT).
      pred_name="$(read_seed_field "$SEED" ".completion_predicate[$i].name")"
      skill_name="$(read_seed_field "$SEED" ".completion_predicate[$i].params.skill" 2>/dev/null || echo "")"
      target="$(read_seed_field "$SEED" ".completion_predicate[$i].params.target" 2>/dev/null || echo "")"
      tmp_out="$(mktemp)"
      case "$pred_name" in
        corpus_auditor_clean)
          claude -p "/cf-corpus-auditor target=$target" > "$tmp_out" 2>&1 || true
          report_file="$(tail -1 "$tmp_out")"
          if [[ -z "$report_file" || ! -f "$report_file" ]]; then
            echo "stop_check: cf-corpus-auditor did not name a readable REPORT file (last stdout line empty or not a file)" >&2
            all_pass=0
          elif grep -qE '^\|' "$report_file"; then
            all_pass=0
          fi
          ;;
        cross_reference_audit_clean)
          claude -p "/cf-cross-reference-audit target=$target mode=A severity_floor=severe" > "$tmp_out" 2>&1 || true
          issues_file="$(tail -1 "$tmp_out")"
          if [[ -z "$issues_file" || ! -f "$issues_file" ]]; then
            echo "stop_check: cf-cross-reference-audit did not name a readable Issues file" >&2
            all_pass=0
          elif ! grep -qE 'Coverage summary:\s*100%' "$issues_file" && grep -qE '^\|' "$issues_file"; then
            all_pass=0
          fi
          ;;
        new_skills_clean)
          claude -p "/cf-skill-reviewer mode=A target=$target" > "$tmp_out" 2>&1 || true
          if ! grep -qE 'Recommended Action:.*KEEP' "$tmp_out"; then
            all_pass=0
          fi
          ;;
        auto_build_spec_clean)
          claude -p "/cf-doc-reviewer \\fix2 target=$target" > "$tmp_out" 2>&1 || true
          if ! grep -qE 'All findings resolved:\s*YES' "$tmp_out"; then
            all_pass=0
          fi
          ;;
        *)
          echo "stop_check: $kind predicate '$pred_name' (skill='$skill_name') has no marker-map entry (expected: corpus_auditor_clean, cross_reference_audit_clean, new_skills_clean, auto_build_spec_clean)" >&2
          rm -f "$tmp_out"
          exit 3
          ;;
      esac
      rm -f "$tmp_out"
      ;;
    *)
      echo "stop_check: unknown check_kind '$kind' (not in §8.4 enum)" >&2; exit 3
      ;;
  esac
done

[[ "$all_pass" -eq 1 ]] && exit 0 || exit 1
