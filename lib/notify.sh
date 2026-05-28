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
  # Slack interface disabled by default per operator directive 2026-05-28 — the slack_webhook
  # branch fires ONLY when the operator explicitly exports CF_ORCHESTRATOR_ENABLE_SLACK=1
  # (fully reversible; code retained). Email (gmail_smtp) is primary per seed v1.3.
  if [[ "$primary" == "slack_webhook" && "${CF_ORCHESTRATOR_ENABLE_SLACK:-0}" == "1" && -n "$primary_env" && "$primary_env" != "null" && -n "${!primary_env:-}" ]]; then
    channel_attempted="slack_webhook"
    if curl -fsS -X POST -H 'Content-Type: application/json' \
            --data "$(jq -nc --arg t "$msg" '{text:$t}')" \
            "${!primary_env}" >/dev/null 2>&1; then
      channel_result="ok"
    else
      channel_result="fail"
    fi
  elif [[ "$primary" == "gmail_smtp" ]]; then
    # gmail_smtp branch (seed v1.3): sender + destination addresses are read LITERALLY from the
    # seed (.primary_smtp_user / .primary_to_address) — not secret, baked in for zero-setup
    # operability. Only the app password is env-var-indirect: .primary_env_vars.smtp_app_password
    # holds the env-var NAME, indirect-looked-up to the 16-char Google app password operator-set
    # at Machine-scope. Password value is NEVER echoed/logged/persisted by this function (curl
    # --user passes it in the child process env only). On any unset value, channel_result
    # records the failure mode and the unconditional audit-log append below still fires (FR-009).
    channel_attempted="gmail_smtp"
    local smtp_user_val to_addr_val smtp_app_pw_env smtp_host smtp_port
    smtp_user_val="$(read_seed_field "$seed_path" '.notification_channel.primary_smtp_user' 2>/dev/null || echo "")"
    to_addr_val="$(read_seed_field "$seed_path" '.notification_channel.primary_to_address' 2>/dev/null || echo "")"
    smtp_app_pw_env="$(read_seed_field "$seed_path" '.notification_channel.primary_env_vars.smtp_app_password' 2>/dev/null || echo "")"
    smtp_host="$(read_seed_field "$seed_path" '.notification_channel.primary_smtp_host' 2>/dev/null || echo "")"
    smtp_port="$(read_seed_field "$seed_path" '.notification_channel.primary_smtp_port' 2>/dev/null || echo "")"
    local smtp_app_pw_val="${!smtp_app_pw_env:-}"
    if [[ -z "$smtp_user_val" || -z "$smtp_app_pw_val" || -z "$to_addr_val" || -z "$smtp_host" || -z "$smtp_port" ]]; then
      channel_result="skipped:env_unset"
    else
      local tmp_msg
      tmp_msg="$(mktemp 2>/dev/null || printf '/tmp/notify_msg.%s' "$$")"
      {
        printf 'From: %s\r\n' "$smtp_user_val"
        printf 'To: %s\r\n' "$to_addr_val"
        printf 'Subject: [CF Orchestrator] %s — iteration %s\r\n' "$event" "${iteration:-unknown}"
        printf 'Date: %s\r\n' "$(date -R)"
        printf '\r\n'
        printf '%s\r\n' "$msg"
      } > "$tmp_msg"
      curl --silent --show-error --max-time 30 --ssl-reqd \
           --url "smtp://${smtp_host}:${smtp_port}" \
           --mail-from "$smtp_user_val" \
           --mail-rcpt "$to_addr_val" \
           --user "${smtp_user_val}:${smtp_app_pw_val}" \
           --upload-file "$tmp_msg" >/dev/null 2>&1
      local rc=$?
      if [[ $rc -eq 0 ]]; then
        channel_result="success"
      else
        channel_result="failure:rc=$rc"
      fi
      rm -f "$tmp_msg"
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
