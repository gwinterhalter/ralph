#!/usr/bin/env bash
# lib/notify.sh — P4-05 notification dispatch (FR-009 / seed §12).
# Contract: dispatch_notification <seed_path> <state_dir> <event> <context_json>
#   <event>        one of: gate_human | iteration_failed | budget_exhausted | initiative_complete | fail_counts_threshold
#   <context_json> JSON object carrying {iteration, gate_id?, reason?, ...}
# Reads seed.notification_channel as a MAP (.primary / .primary_env_var / .fallback).
# Slack-webhook primary fires when env var named by .primary_env_var is set+non-empty; else
# .fallback path (win11toast when available; otherwise a no-op channel send).
# Audit append to "$state_dir/logs/notifications.log" is UNCONDITIONAL (FR-009 surface).
# Depends on read_seed_field() from lib/seed.sh + jq + curl (Slack path) + win11toast (fallback path).

dispatch_notification() {
  local seed_path="$1"
  local state_dir="$2"
  local event="$3"
  local context_json="$4"
  local primary primary_env fallback msg ts iteration gate_id channel_attempted channel_result
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  iteration="$(printf '%s' "$context_json" | jq -r '.iteration // ""' 2>/dev/null || echo "")"
  gate_id="$(printf '%s' "$context_json" | jq -r '.gate_id // "null"' 2>/dev/null || echo "null")"
  primary="$(read_seed_field "$seed_path" '.notification_channel.primary' 2>/dev/null || echo "")"
  primary_env="$(read_seed_field "$seed_path" '.notification_channel.primary_env_var' 2>/dev/null || echo "")"
  fallback="$(read_seed_field "$seed_path" '.notification_channel.fallback' 2>/dev/null || echo "")"
  msg="ralph-loop event=$event iteration=$iteration gate_id=$gate_id"
  channel_attempted="none"
  channel_result="no_channel"
  if [[ "$primary" == "slack_webhook" && -n "$primary_env" && "$primary_env" != "null" && -n "${!primary_env:-}" ]]; then
    channel_attempted="slack_webhook"
    if curl -fsS -X POST -H 'Content-Type: application/json' \
            --data "$(jq -nc --arg t "$msg" '{text:$t}')" \
            "${!primary_env}" >/dev/null 2>&1; then
      channel_result="ok"
    else
      channel_result="fail"
    fi
  elif [[ "$fallback" == "win11toast" ]]; then
    channel_attempted="win11toast"
    if command -v win11toast >/dev/null 2>&1; then
      if win11toast "$msg" >/dev/null 2>&1; then channel_result="ok"; else channel_result="fail"; fi
    else
      channel_result="unavailable"
    fi
  fi
  # UNCONDITIONAL audit append — runs regardless of channel attempt / success (FR-009).
  mkdir -p "$state_dir/logs"
  jq -nc --arg ev "$event" --arg it "$iteration" --arg gid "$gate_id" \
         --arg att "$channel_attempted" --arg res "$channel_result" --arg ts "$ts" \
         '{event:$ev, iteration:$it, gate_id:(if $gid=="null" then null else $gid end), channel_attempted:$att, channel_result:$res, ts:$ts}' \
         >> "$state_dir/logs/notifications.log"
}
