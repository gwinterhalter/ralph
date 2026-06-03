# Ralph Status Query Contract

**Version:** 1.1
**Date:** 2026-06-03
**Authority:** Comprehensive_Event_Log_Spec v1.1 §16 (status endpoint) + §13 Q1/Q4 (query patterns / index set).
**Status:** Reference contract. **Path 1 (live) IS NOW IMPLEMENTED as `status.sh`** (FUP-0835, 2026-06-03) — a
dependency-free local reader over `logs/events.jsonl`. The HTTP `GET /ralph/status` endpoint (iPhone Trigger
Service) and Path 2 (historical Supabase query) remain future work.

---

## Purpose

`GET /ralph/status` answers "what is the Ralph loop doing right now, and what did it last complete?" for a
given `(project_id, initiative_slug)`. It has **two independent read paths** with a strict precedence rule:
the **live** path is local-first and MUST resolve even when Supabase is unreachable; the **historical** path
enriches the answer from the synced `events` table when available.

## Path 1 — Live (local NDJSON tail; never blocks on Supabase)

Source: `<state_dir>/logs/events.jsonl` (the local-first append-only event log written by `lib/events.sh`
`emit_event`; FUP-0800 local-append design — NOT the pre-redesign `events.ndjson` name). Implemented by
`status.sh <state_dir>`. Read the **tail** (last N lines, e.g. `tail -n 200`) and derive:

| Field | Derivation |
|---|---|
| `current_iteration_index` | max `iteration_index` across tail lines (or the last `iteration_start`'s `iteration_index`) |
| `last_event` | the last line's `event_type` + `ts_utc` |
| `last_phase_complete` | the last line with `event_type == "phase_complete"` (its `iteration_index`, `ts_utc`, `payload.cumulative_spend`) — the §15 per-iteration heartbeat-equivalent |
| `liveness` | `now - last_event.ts_utc`; stale if beyond an iteration's expected wall-clock |

This path is **dependency-free** (reads a local file) and is the authoritative answer for "is it alive / what
iteration". It MUST be served even if Supabase is down (spec §16 never-block rule).

Example (jq over the tail):

```bash
tail -n 200 "$STATE_DIR/logs/events.jsonl" | jq -s '
  { current_iteration_index: (map(.iteration_index) | max),
    last_event: (last | {event_type, ts_utc}),
    last_phase_complete: (map(select(.event_type=="phase_complete")) | last
                          | {iteration_index, ts_utc, cumulative_spend: .payload.cumulative_spend}) }'
```

## Path 2 — Historical (Supabase `events`; enrichment only)

> **Status (FUP-0800 local-append design):** the bash→Supabase sync (`events_sync` / PostgREST) was **removed**
> — the runtime no longer writes the `events` table, so Path 2 is currently **N/A** (and `code_factory` exposes
> no Data API anyway → PGRST002). If historical enrichment is wanted later, mirror the local log to Postgres via
> the Supabase MCP at a `claude -p` point ("approach 2"; see the events-table disposition, FUP-0836) and serve
> the queries below from there. The query shape + index contract is retained as the build spec.

Source: the synced `public.events` table. Two queries, both served by the v1.1 index set (§13/§14.2). The
endpoint reaches Supabase via PostgREST (same surface as `lib/heartbeat.sh` / `lib/events.sh events_sync`) or
any SQL client; the **query shape + index usage** below is the binding contract, surface-agnostic.

**Q1 — latest event for an initiative** (index-served by `events_project_initiative_ts_idx`
`(project_id, initiative_slug, ts_utc DESC)` — the v1.1 reorder leads with `project_id` for multi-project
scoping, then `initiative_slug`):

```sql
SELECT event_type, role, iteration_index, ts_utc, payload
FROM   public.events
WHERE  project_id = $1 AND initiative_slug = $2
ORDER  BY ts_utc DESC
LIMIT  1;
```

PostgREST form:
`GET /rest/v1/events?project_id=eq.<pid>&initiative_slug=eq.<slug>&order=ts_utc.desc&limit=1`

**Q4 — last phase_complete (cross-run staleness signal; §15)**:

```sql
SELECT iteration_index, ts_utc, payload
FROM   public.events
WHERE  project_id = $1 AND initiative_slug = $2 AND event_type = 'phase_complete'
ORDER  BY ts_utc DESC
LIMIT  1;
```

PostgREST form:
`GET /rest/v1/events?project_id=eq.<pid>&initiative_slug=eq.<slug>&event_type=eq.phase_complete&order=ts_utc.desc&limit=1`

## Precedence + failure semantics

1. Resolve Path 1 (live) first; it is the answer for liveness + current iteration. **Never block on Supabase.**
2. If Supabase is reachable, enrich with Path 2 (historical) — useful when the local `state_dir` was cleared
   or the query host is remote from the run host.
3. If Supabase is unreachable, return the live answer with `historical: null` and a `degraded: true` flag —
   the endpoint still answers.

## Notes

- `project_id` on `events` is a **logical join key** (NOT NULL `text`, no FK; matches `projects.project_id`).
  Q1/Q4 scope by `project_id` then `initiative_slug` so the index is fully used.
- The HTTP endpoint itself is out of scope for the event-log Phase 2 implementation (no service exists yet);
  this contract is the build spec for the future `GET /ralph/status` implementer. Path 1 is now served locally
  by `status.sh`.

## Change History

| Version | Date | Summary |
|---|---|---|
| 1.1 | 2026-06-03 | FUP-0835: Path 1 (live) implemented as `status.sh` (reads `logs/events.jsonl`). Corrected the source path `events.ndjson` → `logs/events.jsonl` (FUP-0800 local-append). Added Path 2 N/A note (bash→Supabase sync removed; deferred to approach-2 per FUP-0836). HTTP endpoint + Path 2 remain future work. |
| 1.0 | 2026-06-02 | Initial contract (two read paths; endpoint unbuilt). |
