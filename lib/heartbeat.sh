#!/usr/bin/env bash
# lib/heartbeat.sh — FUP-0798 closure: per-iteration heartbeat UPSERT to the workstreams
# registry row so long-running headless RL orchestrator runs leave a DB-queryable signal of
# progress (rather than leaving last_session_label / next_session_blocker / metadata stale).
#
# Contract: heartbeat_workstream <seed_path> <state_dir> <iter_index> <phase_label>
#   <iter_index>   — e.g. "0006"
#   <phase_label>  — short tag (e.g. "iter 0006 begin", "iter 0006 consumer", "iter 0006 close")
#
# Reads seed.heartbeat.workstream_id + seed.heartbeat.env_vars.project_url + seed.heartbeat.env_vars.service_role_key.
# Each env_vars.<name> holds the NAME of an env var the operator exports at machine scope; the
# function indirect-looks-up the value (same pattern as lib/notify.sh §gmail_smtp branch).
#
# Backward-compat:
#   - seed lacks .heartbeat.workstream_id     -> silent skip (logged "heartbeat skipped: no workstream_id")
#   - env vars unset / empty                  -> silent skip (logged "heartbeat skipped: env_unset")
#   - PostgREST PATCH returns non-2xx         -> non-fatal warn (logged + curl_rc + http_code; never propagated)
#
# Heartbeat append to "$state_dir/logs/heartbeat.log" is UNCONDITIONAL — even on no-op
# skip-paths the log records the attempt + reason so the operator can verify the hook ran.

heartbeat_workstream() {
  local seed_path="$1"
  local state_dir="$2"
  local iter_index="$3"
  local phase_label="$4"
  local ws_id project_url_env service_role_env project_url_val service_role_val ts log_dir
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  log_dir="$state_dir/logs"
  mkdir -p "$log_dir"

  ws_id="$(read_seed_field "$seed_path" '.heartbeat.workstream_id' 2>/dev/null || echo "")"
  [[ "$ws_id" == "null" ]] && ws_id=""
  if [[ -z "$ws_id" ]]; then
    jq -nc --arg ts "$ts" --arg it "$iter_index" --arg ph "$phase_label" \
       '{ts:$ts, iter:$it, phase:$ph, attempted:false, reason:"no_workstream_id_in_seed"}' \
       >> "$log_dir/heartbeat.log"
    return 0
  fi

  project_url_env="$(read_seed_field "$seed_path" '.heartbeat.env_vars.project_url' 2>/dev/null || echo "")"
  service_role_env="$(read_seed_field "$seed_path" '.heartbeat.env_vars.service_role_key' 2>/dev/null || echo "")"
  [[ "$project_url_env" == "null" ]] && project_url_env=""
  [[ "$service_role_env" == "null" ]] && service_role_env=""
  project_url_val="${!project_url_env:-}"
  service_role_val="${!service_role_env:-}"

  if [[ -z "$project_url_val" || -z "$service_role_val" ]]; then
    jq -nc --arg ts "$ts" --arg it "$iter_index" --arg ph "$phase_label" --arg ws "$ws_id" \
       '{ts:$ts, iter:$it, phase:$ph, attempted:false, reason:"env_unset", workstream_id:$ws}' \
       >> "$log_dir/heartbeat.log"
    return 0
  fi

  # FUP-0834 (decision 2026-06-03): this PostgREST DB-PATCH path is SUPERSEDED by the event
  # log's `phase_complete` event (lib/events.sh; spec §15 heartbeat-equivalent), which is the
  # canonical cross-run staleness signal. It is also NON-FUNCTIONAL on `code_factory`
  # (eybdbshxswutgaaylpol) — that project exposes no Supabase Data API (PostgREST returns
  # PGRST002 `pg_pgrst_no_exposed_schemas`), consistent with the RL design contract "no raw DB
  # connection; all RL DB access via the Supabase MCP only". The path is RETAINED (gated on a
  # seed `.heartbeat` block, which the rl_test harness omits → skips; non-fatal on failure) for
  # any host that DOES expose a Data API, but is not the source of truth. The local
  # heartbeat.log append below is the functional part and the F.4 coexist invariant. If DB
  # heartbeat is ever needed on a no-Data-API project, do it via Supabase MCP at a claude -p
  # point ("approach 2"), never a bash PostgREST write. Full heartbeat_workstream retirement
  # (deleting the orchestrator.sh L215/L426 calls) is deferred until events Q4 is the consumed
  # staleness signal.
  # Build PATCH body — update last_session_label + next_session_blocker + metadata heartbeat keys.
  # metadata is jsonb; PostgREST PATCH on jsonb columns replaces the whole column, so we read +
  # merge: GET current metadata, jq-merge with new heartbeat keys, PATCH the merged value.
  local current_meta merged_meta http_code curl_rc
  set +e
  current_meta="$(curl --silent --max-time 10 \
    -H "apikey: $service_role_val" -H "Authorization: Bearer $service_role_val" \
    "$project_url_val/rest/v1/workstreams?workstream_id=eq.$ws_id&select=metadata" 2>/dev/null \
    | jq -r '.[0].metadata // {}')"
  curl_rc=$?
  set -e
  if [[ $curl_rc -ne 0 ]]; then
    jq -nc --arg ts "$ts" --arg it "$iter_index" --arg ph "$phase_label" --arg ws "$ws_id" --arg rc "$curl_rc" \
       '{ts:$ts, iter:$it, phase:$ph, attempted:true, result:"failure:get_metadata", curl_rc:$rc, workstream_id:$ws}' \
       >> "$log_dir/heartbeat.log"
    return 0
  fi

  merged_meta="$(printf '%s' "$current_meta" | jq -c \
    --arg ts "$ts" --arg it "$iter_index" --arg ph "$phase_label" \
    '. + {heartbeat_at:$ts, heartbeat_iter:$it, heartbeat_phase:$ph}')"

  local patch_body
  patch_body="$(jq -nc --arg lsl "iter $iter_index ($phase_label) @ $ts" \
                       --arg nsb "orchestrator running — iter $iter_index $phase_label" \
                       --argjson meta "$merged_meta" \
                       '{last_session_label:$lsl, next_session_blocker:$nsb, metadata:$meta}')"

  set +e
  http_code="$(curl --silent --show-error --max-time 10 -o /dev/null -w '%{http_code}' \
    -X PATCH \
    -H "apikey: $service_role_val" -H "Authorization: Bearer $service_role_val" \
    -H 'Content-Type: application/json' -H 'Prefer: return=minimal' \
    "$project_url_val/rest/v1/workstreams?workstream_id=eq.$ws_id" \
    -d "$patch_body" 2>/dev/null)"
  curl_rc=$?
  set -e

  if [[ $curl_rc -eq 0 && "$http_code" =~ ^2 ]]; then
    jq -nc --arg ts "$ts" --arg it "$iter_index" --arg ph "$phase_label" --arg ws "$ws_id" --arg hc "$http_code" \
       '{ts:$ts, iter:$it, phase:$ph, attempted:true, result:"success", http_code:$hc, workstream_id:$ws}' \
       >> "$log_dir/heartbeat.log"
  else
    jq -nc --arg ts "$ts" --arg it "$iter_index" --arg ph "$phase_label" --arg ws "$ws_id" --arg hc "$http_code" --arg rc "$curl_rc" \
       '{ts:$ts, iter:$it, phase:$ph, attempted:true, result:"failure:patch", http_code:$hc, curl_rc:$rc, workstream_id:$ws}' \
       >> "$log_dir/heartbeat.log"
  fi
  # Never propagate failure — heartbeat is observability-only, must not block orchestrator.
  return 0
}
