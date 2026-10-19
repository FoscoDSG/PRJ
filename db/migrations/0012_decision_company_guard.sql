-- Guardia sull'azienda delle decisioni nuove. Non riscrive le righe già
-- presenti: nessuna istruzione UPDATE. I company_id NULL già scritti
-- restano NULL e la migrazione non li controlla.
--
-- Vale per le decisioni di colonna/mapping, le fusioni di clienti, i
-- rilasci dalla quarantena, i cambi di colonna segnati come visti e il
-- registro delle decisioni in inbox (app.decisions_log).
-- Una riga nuova che registra la decisione ha company_id non nullo e
-- uguale all'azienda di chi decide. Una riga ancora aperta (in coda, non
-- ancora decisa) può nascere senza azienda. Il controllo è un trigger
-- BEFORE INSERT e, per le tre tabelle di stato, anche BEFORE UPDATE.
-- Su mapping_decisions il vincolo NOT VALID non rilegge le righe vecchie:
-- blocca solo gli inserimenti successivi.

CREATE OR REPLACE FUNCTION app.enforce_mapping_revoke_company()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = app, public
AS $$
DECLARE
  actor_company uuid;
  revoked_company uuid;
  revoked_kind text;
BEGIN
  IF NEW.company_id IS NULL THEN
    RAISE EXCEPTION 'decisione senza azienda';
  END IF;
  SELECT company_id INTO actor_company FROM app.users WHERE username = NEW.actor;
  IF actor_company IS NULL OR actor_company IS DISTINCT FROM NEW.company_id THEN
    RAISE EXCEPTION 'decisione di un''altra azienda';
  END IF;
  IF NEW.decision IS DISTINCT FROM 'revoked' THEN
    RETURN NEW;
  END IF;
  SELECT company_id, decision INTO revoked_company, revoked_kind
  FROM app.mapping_decisions
  WHERE id = NEW.revoked_decision_id;
  IF revoked_kind IS NULL OR revoked_kind NOT IN ('confirm', 'reject') THEN
    RAISE EXCEPTION 'si revoca solo una conferma o un rifiuto';
  END IF;
  IF revoked_company IS DISTINCT FROM actor_company
     OR NEW.company_id IS DISTINCT FROM actor_company THEN
    RAISE EXCEPTION 'revoca di un''altra azienda';
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'mapping_decisions_company_present_check'
      AND conrelid = 'app.mapping_decisions'::regclass
  ) THEN
    ALTER TABLE app.mapping_decisions
      ADD CONSTRAINT mapping_decisions_company_present_check
      CHECK (company_id IS NOT NULL) NOT VALID;
  END IF;
END $$;

COMMENT ON TABLE app.mapping_decisions IS
  'Decisioni di mapping, append-only. La 0007 diceva che non è un dataset che l''agente legge per proporre. La 0010 cambia quella regola e non la aggira: la review del mapping legge queste righe solo per un suggerimento appreso, con lo stesso company_id, la stessa fonte e la stessa intestazione normalizzata. Un company_id NULL non si usa mai. L''agente degli ordini non legge questa tabella. Dalla 0012 una riga nuova ha company_id non nullo e uguale all''azienda di chi decide: le righe già scritte senza azienda restano. confirm, reject e revoked esigono la stessa azienda dell''attore. revoked cita una conferma o un rifiuto, una sola volta, e indica il batch del file.';

ALTER TABLE app.identity_reviews
  ADD COLUMN IF NOT EXISTS company_id uuid;

ALTER TABLE app.identity_reviews
  ADD COLUMN IF NOT EXISTS actor text;

ALTER TABLE app.quarantine_documents
  ADD COLUMN IF NOT EXISTS company_id uuid;

ALTER TABLE app.quarantine_documents
  ADD COLUMN IF NOT EXISTS actor text;

ALTER TABLE app.drift_reviews
  ADD COLUMN IF NOT EXISTS company_id uuid;

ALTER TABLE app.drift_reviews
  ADD COLUMN IF NOT EXISTS actor text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'identity_reviews_company_fkey'
      AND conrelid = 'app.identity_reviews'::regclass
  ) THEN
    ALTER TABLE app.identity_reviews
      ADD CONSTRAINT identity_reviews_company_fkey
      FOREIGN KEY (company_id) REFERENCES app.companies (id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'quarantine_documents_company_fkey'
      AND conrelid = 'app.quarantine_documents'::regclass
  ) THEN
    ALTER TABLE app.quarantine_documents
      ADD CONSTRAINT quarantine_documents_company_fkey
      FOREIGN KEY (company_id) REFERENCES app.companies (id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'drift_reviews_company_fkey'
      AND conrelid = 'app.drift_reviews'::regclass
  ) THEN
    ALTER TABLE app.drift_reviews
      ADD CONSTRAINT drift_reviews_company_fkey
      FOREIGN KEY (company_id) REFERENCES app.companies (id);
  END IF;
END $$;

COMMENT ON COLUMN app.identity_reviews.company_id IS
  'Azienda di chi conferma o rifiuta la fusione. NULL sulle righe già in coda e su quelle decise prima della 0012.';

COMMENT ON COLUMN app.identity_reviews.actor IS
  'Utente che conferma o rifiuta la fusione. NULL finché la riga resta aperta.';

COMMENT ON COLUMN app.quarantine_documents.company_id IS
  'Azienda di chi rilascia il documento. NULL finché il documento resta in quarantena e sulle righe già rilasciate prima della 0012.';

COMMENT ON COLUMN app.quarantine_documents.actor IS
  'Utente che rilascia il documento. NULL finché la riga resta aperta.';

COMMENT ON COLUMN app.drift_reviews.company_id IS
  'Azienda di chi segna il cambio di colonne come visto. NULL finché la riga resta aperta e sulle righe già viste prima della 0012.';

COMMENT ON COLUMN app.drift_reviews.actor IS
  'Utente che segna il cambio come visto. NULL finché la riga resta aperta.';

CREATE OR REPLACE FUNCTION app.enforce_recorded_decision_company()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = app, public
AS $$
DECLARE
  actor_company uuid;
  deciding boolean;
BEGIN
  deciding := CASE TG_TABLE_NAME
    WHEN 'identity_reviews' THEN NEW.status IN ('confirmed', 'rejected')
    WHEN 'quarantine_documents' THEN NEW.status IN ('released', 'rejected')
    WHEN 'drift_reviews' THEN NEW.status = 'acknowledged'
    ELSE false
  END;
  IF NOT deciding THEN
    RETURN NEW;
  END IF;
  IF TG_OP = 'UPDATE'
     AND OLD.status IS NOT DISTINCT FROM NEW.status
     AND OLD.company_id IS NOT DISTINCT FROM NEW.company_id
     AND OLD.actor IS NOT DISTINCT FROM NEW.actor THEN
    RETURN NEW;
  END IF;
  IF NEW.company_id IS NULL THEN
    RAISE EXCEPTION 'decisione senza azienda';
  END IF;
  SELECT company_id INTO actor_company FROM app.users WHERE username = NEW.actor;
  IF actor_company IS NULL OR actor_company IS DISTINCT FROM NEW.company_id THEN
    RAISE EXCEPTION 'decisione di un''altra azienda';
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'identity_reviews_decision_company'
      AND tgrelid = 'app.identity_reviews'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER identity_reviews_decision_company
    BEFORE INSERT OR UPDATE ON app.identity_reviews
    FOR EACH ROW EXECUTE FUNCTION app.enforce_recorded_decision_company();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'quarantine_documents_decision_company'
      AND tgrelid = 'app.quarantine_documents'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER quarantine_documents_decision_company
    BEFORE INSERT OR UPDATE ON app.quarantine_documents
    FOR EACH ROW EXECUTE FUNCTION app.enforce_recorded_decision_company();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'drift_reviews_decision_company'
      AND tgrelid = 'app.drift_reviews'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER drift_reviews_decision_company
    BEFORE INSERT OR UPDATE ON app.drift_reviews
    FOR EACH ROW EXECUTE FUNCTION app.enforce_recorded_decision_company();
  END IF;
END $$;

-- Il registro inbox è append-only: la decisione è l'INSERT. Le righe già
-- scritte restano con company_id NULL. Il CHECK NOT VALID non le rilegge.

ALTER TABLE app.decisions_log
  ADD COLUMN IF NOT EXISTS company_id uuid;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'decisions_log_company_fkey'
      AND conrelid = 'app.decisions_log'::regclass
  ) THEN
    ALTER TABLE app.decisions_log
      ADD CONSTRAINT decisions_log_company_fkey
      FOREIGN KEY (company_id) REFERENCES app.companies (id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'decisions_log_company_present_check'
      AND conrelid = 'app.decisions_log'::regclass
  ) THEN
    ALTER TABLE app.decisions_log
      ADD CONSTRAINT decisions_log_company_present_check
      CHECK (company_id IS NOT NULL) NOT VALID;
  END IF;
END $$;

COMMENT ON COLUMN app.decisions_log.company_id IS
  'Azienda di chi approva, modifica o rifiuta. NULL sulle righe scritte prima della 0012.';

CREATE OR REPLACE FUNCTION app.enforce_decision_log_company()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = app, public
AS $$
DECLARE
  actor_company uuid;
BEGIN
  IF NEW.company_id IS NULL THEN
    RAISE EXCEPTION 'decisione senza azienda';
  END IF;
  SELECT company_id INTO actor_company FROM app.users WHERE username = NEW.actor;
  IF actor_company IS NULL OR actor_company IS DISTINCT FROM NEW.company_id THEN
    RAISE EXCEPTION 'decisione di un''altra azienda';
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'decisions_log_decision_company'
      AND tgrelid = 'app.decisions_log'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER decisions_log_decision_company
    BEFORE INSERT ON app.decisions_log
    FOR EACH ROW EXECUTE FUNCTION app.enforce_decision_log_company();
  END IF;
END $$;
