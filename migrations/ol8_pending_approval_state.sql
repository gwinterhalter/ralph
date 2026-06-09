-- OL-8 — RL Project Intake: add the `pending_approval` projects lifecycle state.
-- A candidate RL project scaffolded by the rl-project-intake skill is inserted at
-- lifecycle_state='pending_approval'; the supervisor admit pass ignores it (admit reads 'candidate'),
-- so nothing dispatches until the operator Approves in the control-panel GUI (→ 'candidate').
-- Additive + idempotent: drops + re-adds the lifecycle CHECK with the extra value; existing rows
-- (all in the prior 8 states) remain valid. (Spec: RL_Project_Intake_and_Evaluation_Spec_v1.0 §4.)
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_lifecycle_state_check;
ALTER TABLE projects ADD CONSTRAINT projects_lifecycle_state_check
  CHECK (lifecycle_state IN (
    'pending_approval',
    'candidate', 'admitted', 'running',
    'paused_gate', 'paused_budget', 'paused_safety',
    'complete', 'failed'
  ));
