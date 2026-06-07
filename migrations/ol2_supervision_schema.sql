-- OL-2 — additive Outer-Loop supervision schema migration (D7).
--
-- Brings the live substrate to the shape supervisor/preflight.py asserts and the
-- supervisor reads/writes. ADDITIVE + IDEMPOTENT by design (Spec v1.3 §4.2 / FR-006):
-- it only ADDs supervision columns to the existing `projects` table (no DROP, no
-- ALTER of the legacy columns, no data loss for the existing rows) and CREATEs
-- `ralph_runs` if absent. Safe to re-apply (every step is IF [NOT] EXISTS / guarded).
--
-- Authoring is code-completable; APPLYING this against the live branch / production is
-- an operator/cf-db-migrations action (LIVE-ONLY) — run it once at C2 preflight.

-- ---------------------------------------------------------------------------
-- §5.2 Project Registry — additive supervision columns (FR-001..008)
-- ---------------------------------------------------------------------------
-- FR-008 lifecycle-state legality (the transitions.py STATES set) is declared inline on
-- the column — matching the canonical code_factory_db migration
-- (20260604090303_extend_projects_supervision_columns). The CHECK is added only with the
-- column (ADD COLUMN IF NOT EXISTS), so replay over a pre-existing column no-ops cleanly.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS folder_path            text;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS lifecycle_state        text NOT NULL DEFAULT 'candidate'
    CHECK (lifecycle_state IN (
        'candidate', 'admitted', 'running',
        'paused_gate', 'paused_budget', 'paused_safety',
        'complete', 'failed'
    ));
ALTER TABLE projects ADD COLUMN IF NOT EXISTS priority               integer NOT NULL DEFAULT 100;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS blast_radius_scope     jsonb;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS attention_debt         integer NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS heartbeat_workstream_id text;

-- ---------------------------------------------------------------------------
-- §5.4 Run Registry — `ralph_runs` (FR-009..014). seed_path is NOT NULL (the
-- iter-0017 production shape preflight asserts); status CHECK = registry.RUN_STATUSES.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ralph_runs (
    run_id            text PRIMARY KEY,
    project_slug      text NOT NULL,                       -- FR-010 soft reference
    seed_path         text NOT NULL,                       -- FR-021 spawn-row completeness (NOT NULL)
    orchestrator_pid  integer,                             -- FR-009 post-spawn pid
    orchestrator_start_time text,                           -- FR-013 recorded half (pid-reuse disambiguation)
    status            text NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'complete', 'budget_exhausted', 'failed', 'halted')),
    idempotency_key   text,
    spawned_at        timestamptz NOT NULL DEFAULT now(),
    terminated_at     timestamptz,                         -- FR-011 terminal reconcile
    terminal_cost_usd numeric(10, 4),                      -- FR-014 exact-decimal money (NFR-007)
    metadata          jsonb,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- FR-013 recorded half: persist the orchestrator's OS start-time at spawn so a
-- Supervisor restart can disambiguate pid reuse on re-attach. Additive ALTER (not just
-- the CREATE body above) so an already-existing ralph_runs table gains the column on
-- replay. Idempotent (IF NOT EXISTS).
ALTER TABLE ralph_runs ADD COLUMN IF NOT EXISTS orchestrator_start_time text;

-- FR-007 active-run uniqueness: at most one running Run per Project (the partial
-- unique index the scheduler/admission ceiling + dispatch-idempotency gate on).
CREATE UNIQUE INDEX IF NOT EXISTS uq_ralph_runs_active_per_project
    ON ralph_runs (project_slug)
    WHERE status = 'running';
