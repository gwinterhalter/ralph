#!/usr/bin/env bash
# status.sh — FUP-0835: local "live" status reader (Path 1 of docs/status_query_contract.md).
# Dependency-free: reads the tail of the local append-only event log and prints the current
# iteration, last event, last phase_complete, last run boundary, and liveness for a run. No
# Supabase dependency (never blocks). The Path-2 (historical Supabase `events` query) and the
# HTTP `GET /ralph/status` endpoint (iPhone Trigger Service) remain future work — see
# docs/status_query_contract.md. This script IS the Path-1 implementation that doc specifies.
#
# Usage: status.sh <state_dir> [tail_lines]
#   <state_dir>  — the orchestrator state_dir (the dir holding logs/events.jsonl)
#   [tail_lines] — trailing events to scan (default 200)
#
# Output: a single JSON object on stdout. Exit 0 always (a missing log returns a degraded
# answer rather than failing — the never-block contract, spec §16).
set -euo pipefail
STATE_DIR="${1:?usage: status.sh <state_dir> [tail_lines]}"
TAIL_N="${2:-200}"
# Local-append design (FUP-0800) writes logs/events.jsonl — NOT the spec's original
# <state_dir>/events.ndjson path (that was the pre-redesign name; docs/status_query_contract.md
# v1.1 reconciles it).
EVENTS="$STATE_DIR/logs/events.jsonl"

if [[ ! -f "$EVENTS" ]]; then
  jq -nc --arg p "$EVENTS" '{error:"no event log at path", events_path:$p, degraded:true, historical:null}'
  exit 0
fi

now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
tail -n "$TAIL_N" "$EVENTS" | jq -s --arg now "$now" '
  ( map(.iteration_index) | map(select(. != null)) ) as $iters
  | { current_iteration_index: ( $iters | max ),
      last_event: ( if length == 0 then null else (last | {event_type, ts_utc, role, iteration_index}) end ),
      last_phase_complete: ( map(select(.event_type == "phase_complete")) | last
                             | if . == null then null
                               else {iteration_index, ts_utc, cumulative_spend: (.payload.cumulative_spend // null)} end ),
      last_run: ( map(select(.event_type == "run_start" or .event_type == "run_end")) | last
                  | if . == null then null
                    else {event_type, ts_utc, terminal_reason: (.payload.terminal_reason // null), resumed: (.payload.resumed // null)} end ),
      now: $now,
      degraded: false,
      historical: null }'
