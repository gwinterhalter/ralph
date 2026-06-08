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
-- Cross-initiative dependency gating (Item 1): the prerequisite project_ids a Candidate must wait
-- on. The admission gate HOLDS a Candidate (left `candidate`, retried each cycle — not rejected,
-- not spawned) while any listed prerequisite is not yet `complete`. Defaults to the empty array, so
-- every single-initiative project is unblocked unless it declares prerequisites (zero behaviour
-- change for existing rows). FR-022/FR-019 admission-precondition family.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS depends_on             text[] NOT NULL DEFAULT '{}';

-- ---------------------------------------------------------------------------
-- §5.4 Run Registry — `ralph_runs` (FR-009..014). seed_path is NOT NULL (the
-- iter-0017 production shape preflight asserts); status CHECK = registry.RUN_STATUSES.
-- The column shape MIRRORS the canonical code_factory_db ralph_runs table EXACTLY so a
-- fresh apply == canonical: `run_id` is `uuid` with a `gen_random_uuid()` server default
-- (the supervisor's record_run inserts WITHOUT a run_id and relies on this default — a
-- `text PK` with no default would reject those inserts); `metadata` is NOT NULL DEFAULT
-- '{}'; `spawned_at` is nullable (admission supplies it explicitly); `created_at` exists.
-- The FR-013 recorded orchestrator start-time is stored under `metadata.pid_start_time`
-- (the canonical convention shared with the Trigger Service FR-005) — NOT a dedicated
-- column — so the shared table keeps a single home for the fact.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ralph_runs (
    run_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),  -- canonical: uuid + server default
    project_slug      text NOT NULL,                       -- FR-010 soft reference
    seed_path         text NOT NULL,                       -- FR-021 spawn-row completeness (NOT NULL)
    orchestrator_pid  integer,                             -- FR-009 post-spawn pid
    status            text NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'complete', 'budget_exhausted', 'failed', 'halted')),
    idempotency_key   text,
    spawned_at        timestamptz,                         -- canonical: nullable (admission supplies it)
    terminated_at     timestamptz,                         -- FR-011 terminal reconcile
    terminal_cost_usd numeric(10, 4),                      -- FR-014 exact-decimal money (NFR-007)
    metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,   -- FR-013: metadata.pid_start_time
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- FR-007 active-run uniqueness: at most one running Run per Project (the partial
-- unique index the scheduler/admission ceiling + dispatch-idempotency gate on).
CREATE UNIQUE INDEX IF NOT EXISTS uq_ralph_runs_active_per_project
    ON ralph_runs (project_slug)
    WHERE status = 'running';
