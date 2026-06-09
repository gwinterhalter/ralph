-- OL-6 — per-item cost basis (Fleet Analytics D1 refinement).
--
-- Adds items_closed to the learning corpus so cost forecasting can use a true cost-PER-ITEM basis
-- (run cost / items closed in that run) instead of the v1 cost-per-Run mean. ADDITIVE + IDEMPOTENT
-- (§4.2): one nullable column. Live-only apply.

ALTER TABLE learning_records ADD COLUMN IF NOT EXISTS items_closed integer;
