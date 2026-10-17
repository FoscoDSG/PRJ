-- Checks for new mapping decisions. Does not edit 0007 or 0008.
-- NOT VALID: rows already stored are not scanned. Inserts and updates are.
-- target_property_corrected is allowed only on a reject.

ALTER TABLE app.mapping_decisions
  ADD CONSTRAINT mapping_decisions_snapshot_present_check
  CHECK (
    source_name IS NOT NULL
    AND source_field IS NOT NULL
    AND target_property IS NOT NULL
    AND score IS NOT NULL
    AND confidence_rule IS NOT NULL
  ) NOT VALID;

ALTER TABLE app.mapping_decisions
  ADD CONSTRAINT mapping_decisions_correction_on_reject_check
  CHECK (target_property_corrected IS NULL OR decision = 'reject') NOT VALID;

COMMENT ON TABLE app.mapping_decisions IS
  'Decisioni di mapping, append-only. Le righe nuove hanno fonte, campo, proprietà, punteggio e regola. target_property_corrected solo su reject. human è un''etichetta, anche un rifiuto. bulk_rule non entra nel test set.';
