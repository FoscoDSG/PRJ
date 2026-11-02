-- Snapshot of a mapping decision, reject, and bulk confirmation.
-- 0007 is unchanged. Existing rows keep null snapshot columns.
-- New rows written by the application fill the snapshot.
--
-- Allowlist, exact lines:
--   ALTER TABLE app.mapping_decisions DROP CONSTRAINT IF EXISTS mapping_decisions_decision_check
--   ALTER TABLE app.mapping_decisions DROP CONSTRAINT IF EXISTS mapping_decisions_confidence_method_check

ALTER TABLE app.mapping_decisions
  ADD COLUMN IF NOT EXISTS source_name text;

ALTER TABLE app.mapping_decisions
  ADD COLUMN IF NOT EXISTS source_field text;

ALTER TABLE app.mapping_decisions
  ADD COLUMN IF NOT EXISTS target_property text;

ALTER TABLE app.mapping_decisions
  ADD COLUMN IF NOT EXISTS target_property_corrected text;

ALTER TABLE app.mapping_decisions
  ADD COLUMN IF NOT EXISTS score numeric;

ALTER TABLE app.mapping_decisions
  ADD COLUMN IF NOT EXISTS confidence_rule text;

ALTER TABLE app.mapping_decisions DROP CONSTRAINT IF EXISTS mapping_decisions_decision_check;

ALTER TABLE app.mapping_decisions ADD CONSTRAINT mapping_decisions_decision_check
  CHECK (decision IN ('confirm', 'reject'));

ALTER TABLE app.mapping_decisions DROP CONSTRAINT IF EXISTS mapping_decisions_confidence_method_check;

ALTER TABLE app.mapping_decisions ADD CONSTRAINT mapping_decisions_confidence_method_check
  CHECK (confidence_method IN ('human', 'bulk_rule'));

COMMENT ON TABLE app.mapping_decisions IS
  'Decisioni di mapping, append-only. human è un''etichetta. bulk_rule è la conferma sopra soglia e non entra nel test set. source_name, source_field, target_property, score e confidence_rule sono la fotografia del momento. target_property_corrected è facoltativa.';
