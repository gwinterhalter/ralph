-- OL-7 — learning effect-measurement (Effect-Measurement Loop spec).
--
-- Records the measured before/after effect of each ADOPTED (applied) Run-Auditor finding, so
-- learnings prove they helped. ADDITIVE + IDEMPOTENT (§4.2): one new table, CREATE IF NOT EXISTS.
-- Read-only w.r.t. audited artifacts (FR-053) — populated by the §4.4(6) measure pass from the
-- events table; one recomputed row per finding (keyed by finding_key, soft-ref run_audit_findings).
-- Live-only apply.

CREATE TABLE IF NOT EXISTS run_audit_effects (
    finding_key        text PRIMARY KEY,          -- soft-references run_audit_findings.finding_key
    kind               text NOT NULL,
    subject            text NOT NULL,
    applied_at         timestamptz,               -- the adoption instant measured against
    before_metric      double precision,          -- the effect signal across pre-adoption runs
    after_metric       double precision,          -- the effect signal across post-adoption runs
    post_adoption_runs integer NOT NULL DEFAULT 0,
    outcome            text NOT NULL DEFAULT 'pending'
                          CHECK (outcome IN ('pending', 'confirmed', 'no_effect', 'regressed')),
    detail             text,
    measured_at        timestamptz NOT NULL DEFAULT now()
);
