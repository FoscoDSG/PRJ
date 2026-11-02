-- Admit hmac-sha256-v2 on decisions_log.hash_version.
-- Does not change the column default (hmac-sha256-v1) and does not
-- rewrite existing rows. The application still writes hmac-sha256-v1.
-- This file does not rotate the evidence HMAC key.

ALTER TABLE app.decisions_log DROP CONSTRAINT IF EXISTS decisions_log_hash_version_check;

ALTER TABLE app.decisions_log
  ADD CONSTRAINT decisions_log_hash_version_check
  CHECK (hash_version IN ('sha256', 'hmac-sha256-v1', 'hmac-sha256-v2'));

COMMENT ON COLUMN app.decisions_log.hash_version IS
  'sha256 sulle righe già presenti all''aggiunta della colonna. Le righe nuove usano hmac-sha256-v1. hmac-sha256-v2 è ammesso per una rotazione futura e non è il default. Nessun UPDATE.';
