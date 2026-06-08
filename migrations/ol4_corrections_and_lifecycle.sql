-- OL-4 — correction capture + finding lifecycle (Item 2 promoter enhancement).
--
-- ADDITIVE + IDEMPOTENT (Spec v1.3 §4.2): a new correction-history table + additive lifecycle
-- columns on run_audit_findings (ol3). No DROP/destructive ALTER. Safe to re-apply. Live-only apply.

-- ---------------------------------------------------------------------------
-- Correction history: one row per cf-correction-agent `correction_attempt` event (the L1-L4
-- correction-loop retries). Keyed by the event_uuid so re-reading the append-only event log is
-- idempotent (no duplication). Feeds the correction_pattern learning finding + the corrections view.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS correction_attempts (
    event_uuid       text PRIMARY KEY,
    project_slug     text NOT NULL,
    iteration_index  integer,
    attempt          integer,
    level            text,                          -- L1 | L2 | L3 | L4 (the patch level)
    item_id          text,                          -- the work-item under correction
    ts_utc           text,                          -- the event instant (ISO-8601)
    captured_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Finding lifecycle on run_audit_findings (ol3): the operator decision state the control-panel
-- promoter drives. A NEW finding is `proposed`; promote -> `accepted` (+ an adopt_learning dispatch),
-- the authoring skill applies it -> `applied`; reject -> `rejected` (suppressed, never re-surfaced).
-- authoring_skill is the skill named in routes_to (the dispatch target). All additive + defaulted.
-- ---------------------------------------------------------------------------
ALTER TABLE run_audit_findings ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed', 'accepted', 'applied', 'rejected'));
ALTER TABLE run_audit_findings ADD COLUMN IF NOT EXISTS authoring_skill text;
ALTER TABLE run_audit_findings ADD COLUMN IF NOT EXISTS decided_by      text;
ALTER TABLE run_audit_findings ADD COLUMN IF NOT EXISTS decided_at      timestamptz;
