-- Il rifiuto di un documento in quarantena richiede un motivo.
-- Nessun aggiornamento delle righe già presenti: un documento rifiutato
-- prima di questa colonna resta con il motivo vuoto.
--
-- decided_at è il momento in cui il documento viene rilasciato o rifiutato.
-- Le righe già presenti restano senza data: la colonna non ha un valore
-- predefinito e created_at non viene copiato.
--
-- Il CHECK è NOT VALID: non rilegge le righe vecchie. Un rifiuto nuovo
-- senza un motivo di almeno 5 caratteri, spazi esclusi, non entra.

ALTER TABLE app.quarantine_documents
  ADD COLUMN IF NOT EXISTS reason text;

ALTER TABLE app.quarantine_documents
  ADD COLUMN IF NOT EXISTS decided_at timestamptz;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'quarantine_documents_reject_reason_check'
      AND conrelid = 'app.quarantine_documents'::regclass
  ) THEN
    ALTER TABLE app.quarantine_documents
      ADD CONSTRAINT quarantine_documents_reject_reason_check
      CHECK (
        status <> 'rejected'
        OR char_length(btrim(coalesce(reason, ''))) >= 5
      ) NOT VALID;
  END IF;
END $$;

COMMENT ON COLUMN app.quarantine_documents.reason IS
  'Motivo del rifiuto. Vuoto sui documenti rifiutati prima di questa colonna.';

COMMENT ON COLUMN app.quarantine_documents.decided_at IS
  'Momento del rilascio o del rifiuto. Vuoto sui documenti decisi prima di questa colonna.';

COMMENT ON CONSTRAINT quarantine_documents_reject_reason_check ON app.quarantine_documents IS
  'Un rifiuto nuovo richiede un motivo di almeno 5 caratteri, spazi esclusi. NOT VALID: i rifiuti già scritti senza motivo restano.';
