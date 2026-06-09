-- OL-5 — fleet event persistence helper (Fleet Analytics spec §1).
--
-- The `events` table is CANONICALLY owned by the Comprehensive_Event_Log_Spec / code_factory_db
-- migrations (it already exists on the branch + prod). This helper MIRRORS that canonical shape
-- EXACTLY (the same pattern ol2 uses for ralph_runs) so a fresh DB == canonical and an existing one
-- no-ops. The OUTER LOOP ships each project's local events.jsonl into this table (the deferred
-- "Phase 2 Supabase sync" — now done by the supervisor, which holds a psycopg connection the bash
-- hooks never had). ADDITIVE + IDEMPOTENT (§4.2): CREATE ... IF NOT EXISTS only. event_uuid is
-- UNIQUE => idempotent ingest (ON CONFLICT (event_uuid) DO NOTHING; re-reading a log never dups).
-- Live-only apply.

CREATE TABLE IF NOT EXISTS events (
    id               bigserial PRIMARY KEY,                 -- canonical: sequence-backed surrogate
    event_uuid       uuid NOT NULL UNIQUE,                  -- the §4.1 envelope id; ingest idempotency key
    schema_version   integer NOT NULL,
    project_id       text NOT NULL,
    initiative_slug  text NOT NULL,
    iteration_index  integer NOT NULL,
    role             text NOT NULL,
    event_type       text NOT NULL,
    ts_utc           timestamptz NOT NULL,                  -- the event instant
    duration_ms      bigint,
    subject_id       text,
    subject_kind     text,
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- Canonical analysis access paths (mirrored IF NOT EXISTS).
CREATE INDEX IF NOT EXISTS events_project_initiative_ts_idx ON events (project_id, initiative_slug, ts_utc DESC);
CREATE INDEX IF NOT EXISTS events_event_type_idx           ON events (event_type);
CREATE INDEX IF NOT EXISTS events_subject_kind_idx         ON events (subject_kind);
