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
  local primary primary_env fallback msg ts iteration gate_id reason channel_attempted channel_result
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  iteration="$(printf '%s' "$context_json" | jq -r '.iteration // ""' 2>/dev/null || echo "")"
  gate_id="$(printf '%s' "$context_json" | jq -r '.gate_id // "null"' 2>/dev/null || echo "null")"
  # FUP-0774: persist `reason` field from context_json to the audit log entry. Without this,
  # DW assertions that target a specific dispatch path (e.g. `.reason == "answerer_demote"`
  # against notifications.log) are unverifiable — the demote signal previously only lived in
  # execute_with_gates.sh stderr ("Answerer demoted gate_dc <id> to gate_human"). With this
  # fix, both surfaces carry the reason: stderr for live-tail observability, notifications.log
  # for after-the-fact JSON-queryable audit. Backward-compat: context_json without a `reason`
  # field resolves to `"null"` (jq default) — the log entry emits `reason: null` in that case
  # rather than failing or omitting the field, preserving the schema for queries.
  reason="$(printf '%s' "$context_json" | jq -r '.reason // "null"' 2>/dev/null || echo "null")"
  primary="$(read_seed_field "$seed_path" '.notification_channel.primary' 2>/dev/null || echo "")"
  primary_env="$(read_seed_field "$seed_path" '.notification_channel.primary_env_var' 2>/dev/null || echo "")"
  fallback="$(read_seed_field "$seed_path" '.notification_channel.fallback' 2>/dev/null || echo "")"
  # FUP-0827: back-compat for the legacy SCALAR-STRING notification_channel form
  # (e.g. "gmail_smtp:default"). Older seeds (schema 1.4) declare notification_channel as a
  # "<type>:<config>" string rather than the map form (.primary / .primary_smtp_user / ...).
  # Without this, .notification_channel.primary resolves empty → channel_attempted stays
  # "none" and channel_result "no_channel" → notifications were silently never delivered.
  # When the map form is absent but a clean scalar token is present, derive <type> as the
  # primary channel and default the fallback to win11toast. The gmail_smtp branch below then
  # sources its SMTP routing from F_GMAIL_SMTP_* env vars (host/port defaulted).
  local channel_scalar=""
  if [[ -z "$primary" || "$primary" == "null" ]]; then
    channel_scalar="$(read_seed_field "$seed_path" '.notification_channel' 2>/dev/null || echo "")"
    if [[ "$channel_scalar" =~ ^[a-z_]+(:[a-z0-9_]+)?$ ]]; then
      primary="${channel_scalar%%:*}"
      [[ -z "$fallback" || "$fallback" == "null" ]] && fallback="win11toast"
    fi
  fi
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
    [[ -z "$smtp_user_val" || "$smtp_user_val" == "null" ]] && smtp_user_val="${F_GMAIL_SMTP_USER:-}"
    to_addr_val="$(read_seed_field "$seed_path" '.notification_channel.primary_to_address' 2>/dev/null || echo "")"
    [[ -z "$to_addr_val" || "$to_addr_val" == "null" ]] && to_addr_val="${F_GMAIL_SMTP_TO:-}"
    smtp_app_pw_env="$(read_seed_field "$seed_path" '.notification_channel.primary_env_vars.smtp_app_password' 2>/dev/null || echo "")"
    # FUP-0827: scalar-string form supplies no env-var name → default to the F_GMAIL_SMTP_APP_PASSWORD convention.
    [[ -z "$smtp_app_pw_env" || "$smtp_app_pw_env" == "null" ]] && smtp_app_pw_env="F_GMAIL_SMTP_APP_PASSWORD"
    smtp_host="$(read_seed_field "$seed_path" '.notification_channel.primary_smtp_host' 2>/dev/null || echo "")"
    [[ -z "$smtp_host" || "$smtp_host" == "null" ]] && smtp_host="${F_GMAIL_SMTP_HOST:-smtp.gmail.com}"
    smtp_port="$(read_seed_field "$seed_path" '.notification_channel.primary_smtp_port' 2>/dev/null || echo "")"
    [[ -z "$smtp_port" || "$smtp_port" == "null" ]] && smtp_port="${F_GMAIL_SMTP_PORT:-587}"
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
      # FUP-0787: guard curl under the inherited `set -e` (this file is sourced by
      # execute_with_gates.sh / orchestrator.sh). Unguarded, a transient non-zero curl
      # (e.g. 26 read error) aborts the function before `local rc=$?` captures it, propagating
      # the raw exit up and HALTing the orchestrator on EXECUTE_WITH_GATES_UNEXPECTED_EXIT.
      # Notification failures must stay non-fatal: capture rc, log it, never propagate.
      # FUP-0796: empirical evidence (2026-06-02 diagnostic — token VERIFIED valid via
      # FUP-0805 inbox-receipt confirmation; same dispatch_notification call rc=26 at
      # 04:47Z + rc=0 at 04:57Z) shows the rc=26 is a transient Gmail SMTP issue (network
      # blip, brief rate limit, mid-stream connection reset), not an auth or flag-handling
      # defect. Single retry with 2-second backoff resilient against the transient surface
      # without introducing a long blocking window. On both attempts failing, the existing
      # FUP-0787 guard + FUP-0809 win11toast fallback chain still apply.
      local rc=99
      local attempt
      for attempt in 1 2; do
        set +e
        curl --silent --show-error --max-time 30 --ssl-reqd \
             --url "smtp://${smtp_host}:${smtp_port}" \
             --mail-from "$smtp_user_val" \
             --mail-rcpt "$to_addr_val" \
             --user "${smtp_user_val}:${smtp_app_pw_val}" \
             --upload-file "$tmp_msg" >/dev/null 2>&1
        rc=$?
        set -e
        if [[ $rc -eq 0 ]]; then
          break
        fi
        if [[ $attempt -eq 1 ]]; then
          sleep 2
        fi
      done
      if [[ $rc -eq 0 ]]; then
        if [[ $attempt -eq 1 ]]; then
          channel_result="success"
        else
          channel_result="success_on_retry_${attempt}"
        fi
      else
        channel_result="failure:rc=$rc"
      fi
      rm -f "$tmp_msg"
    fi
  fi
  # FUP-0809: belt-and-braces fallback restructured from elif-on-primary-chain to sequential
  # post-primary check. The original elif structure prevented win11toast fallback from firing
  # whenever the primary chain attempted ANYTHING (whether the attempt succeeded or failed),
  # because the elif required no preceding primary clause to match. Now fires sequentially
  # when the primary did NOT succeed: channel_result not in {ok, success} → try fallback.
  # Captures all primary-failure cases (gmail_smtp rc=26, slack_webhook fail, env_unset skip,
  # unknown primary). Audit log entry records both the primary attempt and the fallback
  # outcome via the augmented channel_attempted + channel_result fields.
  if [[ "$fallback" == "win11toast" && "$channel_result" != "ok" && "$channel_result" != "success" && "$channel_result" != success_on_retry_* ]]; then
    local primary_attempt_summary="$channel_attempted=$channel_result"
    channel_attempted="${channel_attempted}+win11toast_fallback"
    # FUP-NEW (2026-06-02): win11toast is a Python module (pip install win11toast), not a
    # standalone CLI binary. Original `command -v win11toast` check failed even after
    # `pip install win11toast` because the package exposes no PATH entry point. Invoke via
    # `python -c` using argv-passing (avoids shell-injection on $msg with quotes/specials).
    # Backward-compatible: if a win11toast CLI shim exists in PATH (operator-created), prefer it.
    if command -v win11toast >/dev/null 2>&1; then
      if win11toast "$msg" >/dev/null 2>&1; then
        channel_result="primary_failed[$primary_attempt_summary]+fallback:ok"
      else
        channel_result="primary_failed[$primary_attempt_summary]+fallback:fail"
      fi
    elif python -c "import win11toast" >/dev/null 2>&1; then
      # app_id='CF Orchestrator' gives the toast a distinct AppUserModelID so Windows treats
      # CF notifications as a first-class app: shows under "CF Orchestrator" in Action Center
      # grouping AND lets the operator toggle banner-display ON/OFF per-CF independent of the
      # generic Python.win11toast identity. Without app_id, banner-vs-Action-Center is yoked to
      # whatever the default Python toast identity is set to — operator-confirmed 2026-06-02 the
      # toast lands silently in Action Center under that identity.
      # duration='long' = ~25 seconds on-screen (the longest standard Windows toast duration;
      # vs. default ~3-5 seconds operator-confirmed too short to catch). Orchestrator events are
      # infrequent (gate_human / iteration_failed / budget_exhausted / initiative_complete /
      # fail_counts_threshold) so a longer dwell is high-value, low-cost. For longer-than-25s
      # need, switch to scenario='reminder' (toast persists until manually dismissed; adds snooze /
      # dismiss buttons) — reserved as upgrade path if 25s still insufficient.
      if python -c "import sys; from win11toast import toast; toast('CF Orchestrator', sys.argv[1], app_id='CF Orchestrator', duration='long')" "$msg" >/dev/null 2>&1; then
        channel_result="primary_failed[$primary_attempt_summary]+fallback:ok_via_python_module"
      else
        channel_result="primary_failed[$primary_attempt_summary]+fallback:fail_via_python_module"
      fi
    else
      channel_result="primary_failed[$primary_attempt_summary]+fallback:unavailable"
    fi
  fi
  # UNCONDITIONAL audit append — runs regardless of channel attempt / success (FR-009).
  mkdir -p "$state_dir/logs"
  jq -nc --arg ev "$event" --arg it "$iteration" --arg gid "$gate_id" --arg rsn "$reason" \
         --arg att "$channel_attempted" --arg res "$channel_result" --arg ts "$ts" \
         '{event:$ev, iteration:$it, gate_id:(if $gid=="null" then null else $gid end), reason:(if $rsn=="null" then null else $rsn end), channel_attempted:$att, channel_result:$res, ts:$ts}' \
         >> "$state_dir/logs/notifications.log"
}
