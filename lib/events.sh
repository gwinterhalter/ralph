#!/usr/bin/env bash
# lib/events.sh — Comprehensive_Event_Log_Spec v1.1 Phase 2 (FUP-0800).
#
# Sourced helper: a single CONSOLIDATED append-only event log at <state_dir>/logs/events.jsonl,
# written by the bash hooks exactly like lib/notify.sh appends to notifications.log (the FR-009
# unconditional-audit-append pattern). Zero DB, zero new secrets, no PostgREST/psql — co-locates
# with the runtime `logs/` tree the post-run audit already scans (operator directive 2026-06-02).
#
# Function:
#   emit_event <state_dir> <project_id> <initiative_slug> <iteration_index> <role> <event_type> \
#              [<duration_ms>] [<subject_id>] [<subject_kind>] [<payload_json>]
#
# Envelope (spec v1.1 §4.1 — 9 REQUIRED keys): event_uuid, schema_version, project_id,
#   initiative_slug, iteration_index, role, event_type, ts_utc, payload.
#   OPTIONAL (omitted when empty): duration_ms, subject_id, subject_kind.
#
# Four non-negotiable properties copied from the proven lib/notify.sh FR-009 append (operator
# directive 2026-06-02): (a) ISO-8601 UTC timestamp; (b) one compact JSON object per line (jq -nc)
# so it stays jq-queryable; (c) append (>>), never truncate; (d) non-fatal — guarded so a logging
# failure can NEVER HALT the orchestrator under `set -euo pipefail` (the FUP-0787 lesson; spec §6.3).
#
# Design notes:
#   - event_uuid via `openssl rand` (canonical v4 form; /proc/sys/kernel/random/uuid absent on the
#     Git-Bash runtime). Carried for spec-envelope completeness + future dedup if these are ever
#     mirrored into Postgres via the Supabase MCP at a claude -p point (the operator's approach (2);
#     NOT done here — no per-event DB writes from bash hooks).
#   - ts_utc stamped ONCE per event, millisecond precision, trailing Z (spec §5 single authority;
#     a superset of the second-precision ISO-8601 used by the sibling RL logs).
#   - Redaction (spec §10, secrets-only): values of keys matching
#     (token|key|password|secret|app_password) case-insensitively → "[REDACTED]" at every level.
#   - NO Supabase sync: code_factory exposes no Data API (PostgREST disabled) and RL holds no raw DB
#     connection — the local events.jsonl IS the event log; historical/cross-substrate consumption is
#     a separate MCP-based concern, out of scope for this writer.

: "${EVENTS_SCHEMA_VERSION:=1}"

# --- internal: canonical v4 UUID from openssl (proc-uuid absent on Git-Bash) ----------------
_events_uuid() {
  local h var
  h="$(openssl rand -hex 16 2>/dev/null || true)"
  if [[ ${#h} -ne 32 ]]; then
    h="$(printf '%04x%04x%04x%04x%04x%04x%04x%04x' \
         $((RANDOM)) $((RANDOM)) $((RANDOM)) $((RANDOM)) \
         $((RANDOM)) $((RANDOM)) $((RANDOM)) $((RANDOM)))"
    h="${h:0:32}"
  fi
  var=$(( (16#${h:16:1} & 0x3) | 0x8 ))
  printf '%s-%s-4%s-%x%s-%s' "${h:0:8}" "${h:8:4}" "${h:13:3}" "$var" "${h:17:3}" "${h:20:12}"
}

# --- internal: secrets-only redaction (spec §10) of a payload JSON object -------------------
_events_redact() {
  printf '%s' "$1" | jq -c '
    walk(
      if type == "object" then
        with_entries(
          if (.key | ascii_downcase | test("token|key|password|secret|app_password"))
          then .value = "[REDACTED]" else . end)
      else . end)' 2>/dev/null || printf '%s' "$1"
}

# emit_event <state_dir> <project_id> <initiative_slug> <iteration_index> <role> <event_type> \
#            [<duration_ms>] [<subject_id>] [<subject_kind>] [<payload_json>]
emit_event() {
  # Guarded subshell: any internal failure stays contained; the caller is never aborted (spec §6.3).
  (
    set +e
    ev_state_dir="${1:-}"
    ev_pid="${2:-}"
    ev_slug="${3:-}"
    ev_iter="${4:-}"
    ev_role="${5:-}"
    ev_type="${6:-}"
    ev_dur="${7:-}"
    ev_sid="${8:-}"
    ev_skind="${9:-}"
    ev_payload="${10:-}"
    [[ -z "$ev_state_dir" ]] && exit 0
    [[ -z "$ev_payload" ]] && ev_payload='{}'
    # ensure payload is a JSON value; wrap non-JSON as {raw:...}
    if ! printf '%s' "$ev_payload" | jq -e . >/dev/null 2>&1; then
      ev_payload="$(jq -nc --arg v "$ev_payload" '{raw:$v}')"
    fi
    ev_payload="$(_events_redact "$ev_payload")"
    ev_ts="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ 2>/dev/null)"
    ev_uuid="$(_events_uuid)"
    ev_line="$(jq -nc \
      --arg uuid "$ev_uuid" --argjson sv "${EVENTS_SCHEMA_VERSION:-1}" \
      --arg pid "$ev_pid" --arg slug "$ev_slug" --arg iter "$ev_iter" \
      --arg role "$ev_role" --arg et "$ev_type" --arg ts "$ev_ts" \
      --argjson payload "$ev_payload" \
      --arg dur "$ev_dur" --arg sid "$ev_sid" --arg skind "$ev_skind" \
      '{event_uuid:$uuid, schema_version:$sv, project_id:$pid, initiative_slug:$slug,
        iteration_index:($iter|tonumber? // 0), role:$role, event_type:$et,
        ts_utc:$ts, payload:$payload}
       + (if $dur   != "" then {duration_ms:($dur|tonumber? // null)} else {} end)
       + (if $sid   != "" then {subject_id:$sid} else {} end)
       + (if $skind != "" then {subject_kind:$skind} else {} end)' 2>/dev/null)"
    [[ -z "$ev_line" ]] && exit 0
    mkdir -p "$ev_state_dir/logs" 2>/dev/null
    printf '%s\n' "$ev_line" >> "$ev_state_dir/logs/events.jsonl" 2>/dev/null
    exit 0
  ) 2>/dev/null || true
  return 0
}
