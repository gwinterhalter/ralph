-- OL-3 — Learning-capture schema (Item 2 DB capture + auto-feedback).
--
-- Persists the §4.4(6) Learn step's outputs to two queryable tables so learnings survive across
-- runs/projects (today they were local files only): the per-completed-Run cost/duration corpus and
-- the Run-Auditor findings. ADDITIVE + IDEMPOTENT (Spec v1.3 §4.2): CREATE ... IF NOT EXISTS only,
-- no DROP/ALTER of existing tables. Safe to re-apply. Applying is live-only (operator/cf-db-migrations).
--
-- The Run-Auditor itself stays read-only (FR-053): it never writes here. These writes are issued by
-- the sole-writer Registry (supervisor/registry.py) from the production Learn wiring — persisting the
-- auditor's OWN findings (surfacing), never mutating an audited artifact.

-- ---------------------------------------------------------------------------
-- Per-completed-Run cost/duration/status learning corpus (one row per Run; UPSERT by run_id so
-- re-capturing the same completed Run is idempotent — no per-cycle duplication).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS learning_records (
    run_id            text PRIMARY KEY,             -- the ralph_runs.run_id (as text)
    project_slug      text NOT NULL,
    status            text NOT NULL,                -- complete | failed
    cost_usd          numeric(10, 4),               -- exact-decimal terminal spend (NFR-007); NULL if unknown
    duration_seconds  double precision,             -- terminated_at - spawned_at; NULL if unknown
    captured_at       timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Run-Auditor findings (FR-050/051/052), keyed by a stable finding_key so a recurring finding is a
-- single row UPSERTed each pass (last_seen_at + evidence/runs_audited refreshed); first_seen_at is
-- preserved. The auto-feedback bridge treats a freshly-INSERTed key as a NEW learning to surface once.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_audit_findings (
    finding_key       text PRIMARY KEY,             -- "<kind>:<subject>[:<binding_class>]"
    kind              text NOT NULL,                -- answerer_dsl_candidate | verification_binding | session_shape
    subject           text NOT NULL,                -- the gate / binding / shape the finding is about
    binding_class     text,                         -- over_verification | binding_defect (FR-051 only)
    evidence          text NOT NULL,
    recommendation    text NOT NULL,
    routes_to         text NOT NULL,                -- operator + the authoring skill (FR-053 adoption hook)
    runs_audited      integer NOT NULL DEFAULT 0,
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now()
);
