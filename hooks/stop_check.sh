#!/usr/bin/env bash
# hooks/stop_check.sh <seed_path> <state_dir>   (Initiative_Orchestrator_Spec §13.2)
# Exit 0 — all seed.completion_predicate[] pass (orchestrator terminates clean)
# Exit 1 — at least one check failed (continue)
# Exit 2 — budget exhausted (delegated to budget_check.sh; this hook does not set it)
# Exit ≥3 — error / malformed predicate (HALT)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# FUP-0818: default CLAUDE_SKILLS_DIR for direct invocation (outside orchestrator parent).
# Matches orchestrator.sh line 18 export pattern. Used by skill_clean predicate evaluators
# at lines ~286/301/319/330 via --add-dir flag on `claude -p` invocations. Without this
# default, `set -u` aborts stop_check immediately on any skill_clean predicate when run
# standalone (testing, verification, manual diagnostics).
: "${CLAUDE_SKILLS_DIR:=K:/Claude Code Factory/V3/Project_Docs}"
# shellcheck source=../lib/seed.sh
source "$SCRIPT_DIR/../lib/seed.sh"

SEED="${1:?usage: stop_check.sh <seed_path> <state_dir>}"
# shellcheck disable=SC2034  # STATE_DIR required by §13.2 hook signature; predicates use seed-absolute paths
STATE_DIR="${2:?usage: stop_check.sh <seed_path> <state_dir>}"

# FUP-0806: scan-newest resolution for bare-name register references (mirrors Planner-side per
# seed §4.1 / FUP-0788). Args: $1 = workspace_root, $2 = bare register name (e.g.
# "Auto_Build_Gap_Register.md"). Echoes the resolved absolute path of the highest-versioned
# match, or empty if no candidate. Excludes Project_Docs_Current subtree (CLAUDE.md
# read-only historical snapshot) so sibling-project legacy copies don't shadow the live
# Sub_Projects/<project>/{New,design}/ versioned files.
resolve_register_scan_newest() {
  local root="$1" name="$2"
  local base="${name%.md}"
  local best="" best_v="" f bn v
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    [[ "$f" == *Project_Docs_Current* ]] && continue
    bn="${f##*/}"
    v="${bn#${base}_v}"
    v="${v%.md}"
    [[ "$v" =~ ^[0-9]+(\.[0-9]+)*$ ]] || continue
    if [[ -z "$best_v" ]]; then
      best_v="$v"; best="$f"
    elif [[ "$(printf '%s\n%s\n' "$best_v" "$v" | sort -V | tail -1)" == "$v" ]]; then
      best_v="$v"; best="$f"
    fi
  done < <(find "$root" "$SCRIPT_DIR/.." -type f -name "${base}_v*.md" 2>/dev/null)
  echo "$best"
}

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
        # FUP-0806: scan-newest fallback (parallel to registry_zero_open branch above).
        if [[ -z "$register_path" || ! -f "$register_path" ]]; then
          register_path="$(resolve_register_scan_newest "$workspace_root" "$targets_source")"
          if [[ -n "$register_path" && -f "$register_path" ]]; then
            echo "stop_check: artefact_exists targets_source register '$targets_source' resolved via scan-newest to '$register_path'" >&2
          fi
        fi
      fi
      # FUP-0813: descriptive-sentinel fallback — if targets_source looks like a description
      # rather than a filename (no .md extension OR contains whitespace), treat as a sentinel
      # for "use the sibling registry_zero_open's params.path" (reuses the empty/null fallback
      # path at lines 67-73 above). Mirrors that fallback but triggered by heuristic shape
      # rather than missing value. Closes the auto_build_spec_closure seed v1.5.x case where
      # params.targets_source: "register closure entries" (descriptive string) failed both the
      # exact and scan-newest lookup, silently blocking INITIATIVE_COMPLETE detection at iter-13.
      if [[ -z "$register_path" || ! -f "$register_path" ]]; then
        if [[ "$targets_source" != *.md ]] || [[ "$targets_source" == *" "* ]]; then
          sibling_path=""
          for ((k=0; k<count; k++)); do
            if [[ "$(read_seed_field "$SEED" ".completion_predicate[$k].check_kind")" == "registry_zero_open" ]]; then
              sibling_path="$(read_seed_field "$SEED" ".completion_predicate[$k].params.path")"
              break
            fi
          done
          if [[ -n "$sibling_path" && "$sibling_path" != "null" ]]; then
            if [[ -f "$sibling_path" ]]; then
              register_path="$sibling_path"
            else
              register_path="$(resolve_register_scan_newest "$workspace_root" "$sibling_path")"
            fi
            if [[ -n "$register_path" && -f "$register_path" ]]; then
              echo "stop_check: artefact_exists targets_source '$targets_source' looks descriptive — fell back to sibling registry '$sibling_path' -> '$register_path'" >&2
            fi
          fi
        fi
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
        # FUP-0806: scan-newest fallback — bare-name register references (per FUP-0788 / seed §4.1)
        # like "Auto_Build_Gap_Register.md" don't match versioned files by exact find -name. Try
        # `<base>_v*.md` pattern, extract version from basename, take highest. Mirrors the Planner-
        # side resolution rule. (Plain `sort -V` on full paths is unsafe — archive subtrees and
        # sibling project copies sort by directory before version.)
        if [[ -z "$register_path" || ! -f "$register_path" ]]; then
          register_path="$(resolve_register_scan_newest "$workspace_root" "$register_rel")"
          if [[ -n "$register_path" && -f "$register_path" ]]; then
            echo "stop_check: registry_zero_open predicate '$pred_name' register '$register_rel' resolved via scan-newest to '$register_path'" >&2
          fi
        fi
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
            # FUP-0767: predicates whose name is not one of the hardcoded sub-evaluators above
            # (e.g. a V7-shaped seed's descriptively-named `all_v7_items_closed`) carry their
            # semantics in params.filter. Generic evaluator for the "<column> != <value>" form
            # (e.g. "status != closed"): locate <column> by its markdown-table header name, count
            # data rows whose cell != <value>, pass (clean) when that count is 0. The name-based
            # zero_open_gaps / every_closure_cites_iteration arms above remain unchanged.
            pred_filter="$(read_seed_field "$SEED" ".completion_predicate[$i].params.filter" 2>/dev/null || echo "")"
            if [[ -z "$pred_filter" || "$pred_filter" == "null" ]]; then
              echo "stop_check: registry_zero_open predicate '$pred_name' has no known sub-evaluator (expected: zero_open_gaps, every_closure_cites_iteration) and no params.filter" >&2
              exit 3
            fi
            if [[ "$pred_filter" != *"!="* ]]; then
              echo "stop_check: registry_zero_open params.filter '$pred_filter' unsupported for predicate '$pred_name' (expected '<column> != <value>')" >&2
              exit 3
            fi
            filter_col="$(printf '%s' "$pred_filter" | sed -E 's/[[:space:]]*!=.*$//; s/^[[:space:]]*//; s/[[:space:]]*$//')"
            filter_val="$(printf '%s' "$pred_filter" | sed -E 's/^.*!=[[:space:]]*//; s/^"//; s/"$//; s/^[[:space:]]*//; s/[[:space:]]*$//')"
            if [[ -z "$filter_col" ]]; then
              echo "stop_check: registry_zero_open params.filter '$pred_filter' has no column name (expected '<column> != <value>')" >&2
              exit 3
            fi
            col_idx=-1
            unmet_count=0
            while IFS= read -r line; do
              line="${line%$'\r'}"
              [[ "$line" == \|* ]] || continue
              [[ "$line" =~ ^\|[[:space:]]*-+ ]] && continue
              IFS='|' read -ra cells <<< "$line"
              if [[ "$col_idx" -lt 0 ]]; then
                for idx in "${!cells[@]}"; do
                  hdr="${cells[$idx]}"
                  hdr="${hdr#"${hdr%%[![:space:]]*}"}"; hdr="${hdr%"${hdr##*[![:space:]]}"}"
                  if [[ "$hdr" == "$filter_col" ]]; then col_idx="$idx"; break; fi
                done
                if [[ "$col_idx" -lt 0 ]]; then
                  echo "stop_check: registry_zero_open params.filter column '$filter_col' not found in register header ($register_path)" >&2
                  exit 3
                fi
                continue
              fi
              [[ ${#cells[@]} -gt "$col_idx" ]] || continue
              cell="${cells[$col_idx]}"
              cell="${cell#"${cell%%[![:space:]]*}"}"; cell="${cell%"${cell##*[![:space:]]}"}"
              [[ "$cell" != "$filter_val" ]] && unmet_count=$((unmet_count+1))
            done < "$register_path"
            [[ "$unmet_count" -eq 0 ]] || all_pass=0
            ;;
        esac
      fi
      ;;
    skill_clean|doc_review_clean)
      # Phase 4b P4-02 / FUP-0641: per-skill clean-marker map. Markers DERIVED from each
      # skill's SKILL.md output spec at implement time. FUP-0696 alignment 2026-05-24:
      # 3 of 4 predicates re-aligned to match actual SKILL.md output formats. Map:
      #   cf-doc-reviewer (\fix2):     stdout grep "All findings resolved: YES" (unchanged)
      #   cf-corpus-auditor:           parse "- Report location:" line from stdout (SKILL.md L210);
      #                                then REPORT file has no `^|` table rows
      #   cf-cross-reference-audit:    require audit_type=db_columns argv; "Issues path:" line OR
      #                                workspace-root-anchored audit/ glob fallback;
      #                                "Coverage summary:.*100%.*references resolved" (SKILL.md L144)
      #   cf-skill-reviewer (mode A):  awk parses "## Recommended Action" heading + next non-blank
      #                                line; grep validates ^KEEP\b (SKILL.md L195-200)
      # Each branch keys on predicate name (independent evaluation per #4-#7). Missing/failed
      # marker → all_pass=0 (continue; never silent pass). Unknown predicate-name → exit 3 (HALT).
      pred_name="$(read_seed_field "$SEED" ".completion_predicate[$i].name")"
      skill_name="$(read_seed_field "$SEED" ".completion_predicate[$i].params.skill" 2>/dev/null || echo "")"
      target="$(read_seed_field "$SEED" ".completion_predicate[$i].params.target" 2>/dev/null || echo "")"
      tmp_out="$(mktemp)"
      workspace_root="$(read_seed_field "$SEED" '.workspace_root')"
      case "$pred_name" in
        corpus_auditor_clean)
          # FUP-0819: cache-first — look for existing Corpus_Audit*.md report under workspace
          # audit/ within 7-day TTL before invoking claude -p (each invocation costs $1-3 LLM
          # + is stochastic). Falls through to the original claude -p path if no recent
          # audit is found OR if cached audit lacks the clean signal.
          cached_audit="$(find "$workspace_root" -path '*/audit/Corpus_Audit*.md' -type f -mtime -7 2>/dev/null | sort -r | head -1)"
          if [[ -n "$cached_audit" && -f "$cached_audit" ]]; then
            # Clean signal per cf-corpus-auditor v1.7 output format: zero attributable
            # Layer-1 / Layer-2 findings ("Layer 1 findings attributable...: 0 🔴 / 0 🟡 / 0 🟢").
            if grep -qE 'Layer 1 findings attributable[^0-9]+0[^0-9]+0[^0-9]+0|Layer-1[[:space:]]+0[^0-9]+0[^0-9]+0' "$cached_audit"; then
              echo "stop_check: corpus_auditor_clean CACHED-CLEAN via $cached_audit (FUP-0819 cache-first path)" >&2
            else
              echo "stop_check: corpus_auditor_clean CACHED-NOT-CLEAN via $cached_audit (FUP-0819; lacks Layer-1=0/0/0 attributable markers)" >&2
              all_pass=0
            fi
          else
            # No recent audit — fall through to original claude -p invocation.
            # FUP-0745: --add-dir + -- required for slash-command resolution from ralph/ CWD.
            claude -p --add-dir "$CLAUDE_SKILLS_DIR" -- "/cf-corpus-auditor target=$target" > "$tmp_out" 2>&1 || true
            # cf-corpus-auditor v1.7 emits `- Report location: {path}` into stdout (status echo);
            # skill's own SKILL.md L210 confirms this is the declared output convention.
            report_file="$(grep -oE '^- Report location: .+$' "$tmp_out" | sed 's/^- Report location: //' | tail -1)"
            if [[ -z "$report_file" || ! -f "$report_file" ]]; then
              echo "stop_check: cf-corpus-auditor did not emit '- Report location: <path>' line OR report file unreadable" >&2
              all_pass=0
            elif grep -qE '^\|' "$report_file"; then
              all_pass=0
            fi
          fi
          ;;
        cross_reference_audit_clean)
          # FUP-0819: cache-first — look for existing Cross_Reference_Audit*.md report
          # under workspace audit/ within 7-day TTL before invoking claude -p.
          cached_audit="$(find "$workspace_root" -path '*/audit/Cross_Reference_Audit*.md' -type f -mtime -7 2>/dev/null | sort -r | head -1)"
          if [[ -n "$cached_audit" && -f "$cached_audit" ]]; then
            # Clean signal per cf-cross-reference-audit v1.7 output format: presence of
            # "🟢 clean" markers (per-row severity) or "0 severe attributable" summary.
            if grep -qE '🟢 clean|0 severe attributable' "$cached_audit"; then
              echo "stop_check: cross_reference_audit_clean CACHED-CLEAN via $cached_audit (FUP-0819 cache-first path)" >&2
            else
              echo "stop_check: cross_reference_audit_clean CACHED-NOT-CLEAN via $cached_audit (FUP-0819; lacks 🟢-clean / 0-severe markers)" >&2
              all_pass=0
            fi
          else
            # No recent audit — fall through to original claude -p invocation.
            # cf-cross-reference-audit v1.7 SKILL.md L122-129 Inputs require audit_type=;
            # default to db_columns (most-commonly-referenced Tier-1 audit per skill's audit-type table).
            # FUP-0745: --add-dir + -- required for slash-command resolution from ralph/ CWD.
            claude -p --add-dir "$CLAUDE_SKILLS_DIR" -- "/cf-cross-reference-audit target=$target audit_type=db_columns mode=A severity_floor=severe" > "$tmp_out" 2>&1 || true
            # Issues-file resolution: prefer explicit "Issues path:" line; else workspace-root-anchored glob
            # under audit/. SKILL.md L139-146 documents output at <workspace>/audit/<descriptor>_Issues_<date>_v1.0.md.
            issues_file="$(grep -oE '^Issues path: .+$' "$tmp_out" | sed 's/^Issues path: //' | tail -1)"
            if [[ -z "$issues_file" || ! -f "$issues_file" ]]; then
              if [[ -n "$workspace_root" && -d "$workspace_root" ]]; then
                issues_file="$(find "$workspace_root" -path '*/audit/*_Issues_*.md' -type f -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)"
              fi
            fi
            if [[ -z "$issues_file" || ! -f "$issues_file" ]]; then
              echo "stop_check: cf-cross-reference-audit did not emit 'Issues path:' line and no recent audit/ Issues file found under workspace_root" >&2
              all_pass=0
            elif ! grep -qE 'Coverage summary:.*100%.*references resolved' "$issues_file" && grep -qE '^\|' "$issues_file"; then
              all_pass=0
            fi
          fi
          ;;
        new_skills_clean)
          # FUP-0819: vacuously-clean shortcut — if seed.session_shape_catalog has no skill-
          # authoring shape (skill_build, skill_create, etc.), the predicate is vacuously
          # zero per the describe-not-prescribe convention; skip claude -p invocation.
          has_skill_shape="$(read_seed_field "$SEED" '[.session_shape_catalog[] | select(.name == "skill_build" or .name == "skill_create") | .name] | length')"
          if [[ "$has_skill_shape" == "0" ]]; then
            echo "stop_check: new_skills_clean VACUOUSLY-CLEAN — session_shape_catalog has no skill-authoring shape (FUP-0819 shortcut)" >&2
          else
            # FUP-0745: --add-dir + -- required for slash-command resolution from ralph/ CWD.
            claude -p --add-dir "$CLAUDE_SKILLS_DIR" -- "/cf-skill-reviewer mode=A target=$target" > "$tmp_out" 2>&1 || true
            # cf-skill-reviewer v1.9 SKILL.md L195-200 emits `## Recommended Action` header then
            # the value (KEEP / REPAIR / REBUILD) on a separate non-blank line. Awk picks the next
            # non-blank line after the header; grep validates KEEP.
            recommended="$(awk '/^## Recommended Action/{getline; while(/^[[:space:]]*$/) getline; print; exit}' "$tmp_out")"
            if ! echo "$recommended" | grep -qE '^KEEP\b'; then
              all_pass=0
            fi
          fi
          ;;
        auto_build_spec_clean)
          # FUP-0819: cache-first — look for existing cf-doc-reviewer fix2 report under
          # workspace audit/ matching the target doc + within 7-day TTL. Common naming
          # convention: <target_stem>_v*_fix2_<date>.md (e.g. Auto_Build_Spec_v1.39_fix2_2026-05-31.md).
          target_stem="$(basename "$target" .md)"
          cached_audit="$(find "$workspace_root" -path "*/audit/*${target_stem}*fix2*.md" -type f -mtime -7 2>/dev/null | sort -r | head -1)"
          if [[ -n "$cached_audit" && -f "$cached_audit" ]]; then
            # Clean signal per cf-doc-reviewer fix2 output convention: "All findings resolved: YES"
            # (possibly with markdown bold wrapping, e.g. "All findings resolved: **YES**").
            if grep -qE 'All findings resolved:[[:space:]]*\*?\*?YES\*?\*?' "$cached_audit"; then
              echo "stop_check: auto_build_spec_clean CACHED-CLEAN via $cached_audit (FUP-0819 cache-first path)" >&2
            else
              echo "stop_check: auto_build_spec_clean CACHED-NOT-CLEAN via $cached_audit (FUP-0819; lacks 'All findings resolved: YES' marker)" >&2
              all_pass=0
            fi
          else
            # No recent fix2 audit — fall through to original claude -p invocation.
            # FUP-0745: --add-dir + -- required for slash-command resolution from ralph/ CWD.
            claude -p --add-dir "$CLAUDE_SKILLS_DIR" -- "/cf-doc-reviewer \\fix2 target=$target" > "$tmp_out" 2>&1 || true
            if ! grep -qE 'All findings resolved:\s*YES' "$tmp_out"; then
              all_pass=0
            fi
          fi
          ;;
        *)
          # FUP-0722: generic catch-all evaluator for predicate names not in the hardcoded
          # marker map (corpus_auditor_clean, cross_reference_audit_clean, new_skills_clean,
          # auto_build_spec_clean). Operator supplies the success-marker pattern via
          # params.success_marker_pattern (grep -E regex); skill invocation uses params.skill
          # + params.target. If either params.skill or params.success_marker_pattern is
          # missing, preserves the prior unknown-predicate-name HALT semantics. Lets seed
          # authors add new skill_clean / doc_review_clean predicates without requiring a
          # stop_check.sh edit per new predicate.
          generic_marker="$(read_seed_field "$SEED" ".completion_predicate[$i].params.success_marker_pattern" 2>/dev/null || echo "")"
          if [[ -z "$skill_name" || "$skill_name" == "null" ]]; then
            echo "stop_check: $kind predicate '$pred_name' has no marker-map entry AND missing params.skill (catch-all evaluator needs it)" >&2
            rm -f "$tmp_out"
            exit 3
          fi
          if [[ -z "$generic_marker" || "$generic_marker" == "null" ]]; then
            echo "stop_check: $kind predicate '$pred_name' (skill='$skill_name') has no marker-map entry (expected: corpus_auditor_clean, cross_reference_audit_clean, new_skills_clean, auto_build_spec_clean) AND missing params.success_marker_pattern for generic catch-all evaluator" >&2
            rm -f "$tmp_out"
            exit 3
          fi
          # FUP-0745: --add-dir + -- required for slash-command resolution from ralph/ CWD.
          claude -p --add-dir "$CLAUDE_SKILLS_DIR" -- "/$skill_name target=$target" > "$tmp_out" 2>&1 || true
          if ! grep -qE "$generic_marker" "$tmp_out"; then
            echo "stop_check: $kind predicate '$pred_name' (skill='$skill_name') generic-evaluator output does not match params.success_marker_pattern '$generic_marker'" >&2
            all_pass=0
          fi
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
