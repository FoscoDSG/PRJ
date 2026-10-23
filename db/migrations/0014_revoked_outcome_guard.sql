-- Non riscrive le righe già presenti: nessun UPDATE di dati.
--
-- Una raccomandazione la cui ultima decisione è revoked non può passare a
-- executed. Lo stato revoked entra fra quelli ammessi: le righe vecchie
-- restano valide perché il nuovo controllo è più largo.
--
-- Su quarantena, fusioni e cambi di colonne, una riga già decisa non cambia
-- esito e non cambia chi l'ha decisa. Il ritorno in coda resta quello della
-- 0013.
--
-- Il motivo di un annullamento di colonna ha lo stesso controllo del
-- registro inbox, NOT VALID: le righe revoked già scritte senza motivo
-- restano, le righe nuove no.
--
-- Allowlist, riga esatta:
--   ALTER TABLE app.recommendations DROP CONSTRAINT IF EXISTS recommendations_status_check

ALTER TABLE app.recommendations DROP CONSTRAINT IF EXISTS recommendations_status_check;

ALTER TABLE app.recommendations
  ADD CONSTRAINT recommendations_status_check
  CHECK (status IN (
    'detected', 'proposed', 'approved', 'modified', 'rejected',
    'dry_run_ok', 'executed', 'failed', 'expired', 'revoked'
  ));

CREATE OR REPLACE FUNCTION app.prevent_execute_after_revocation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = app, public
AS $$
DECLARE
  latest text;
BEGIN
  IF NEW.status = 'executed' AND OLD.status IS DISTINCT FROM 'executed' THEN
    SELECT decision INTO latest
    FROM app.decisions_log
    WHERE recommendation_id = NEW.id
    ORDER BY decided_at DESC, id DESC
    LIMIT 1;
    IF latest = 'revoked' THEN
      RAISE EXCEPTION 'decisione annullata: non si esegue';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'recommendations_no_execute_after_revocation'
      AND tgrelid = 'app.recommendations'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER recommendations_no_execute_after_revocation
    BEFORE UPDATE ON app.recommendations
    FOR EACH ROW EXECUTE FUNCTION app.prevent_execute_after_revocation();
  END IF;
END $$;

CREATE OR REPLACE FUNCTION app.prevent_decided_outcome_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = app, public
AS $$
DECLARE
  decided boolean;
BEGIN
  IF TG_TABLE_NAME = 'identity_reviews' THEN
    decided := OLD.status IN ('confirmed', 'rejected');
  ELSIF TG_TABLE_NAME = 'quarantine_documents' THEN
    decided := OLD.status IN ('released', 'rejected');
  ELSIF TG_TABLE_NAME = 'drift_reviews' THEN
    decided := OLD.status = 'acknowledged';
  ELSE
    decided := false;
  END IF;
  IF decided AND (
    NEW.status IS DISTINCT FROM OLD.status
    OR NEW.actor IS DISTINCT FROM OLD.actor
  ) THEN
    RAISE EXCEPTION 'decisione già presa: esito e autore restano';
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'identity_reviews_outcome_locked'
      AND tgrelid = 'app.identity_reviews'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER identity_reviews_outcome_locked
    BEFORE UPDATE ON app.identity_reviews
    FOR EACH ROW EXECUTE FUNCTION app.prevent_decided_outcome_change();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'quarantine_documents_outcome_locked'
      AND tgrelid = 'app.quarantine_documents'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER quarantine_documents_outcome_locked
    BEFORE UPDATE ON app.quarantine_documents
    FOR EACH ROW EXECUTE FUNCTION app.prevent_decided_outcome_change();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'drift_reviews_outcome_locked'
      AND tgrelid = 'app.drift_reviews'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER drift_reviews_outcome_locked
    BEFORE UPDATE ON app.drift_reviews
    FOR EACH ROW EXECUTE FUNCTION app.prevent_decided_outcome_change();
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'mapping_decisions_revoked_reason_check'
      AND conrelid = 'app.mapping_decisions'::regclass
  ) THEN
    ALTER TABLE app.mapping_decisions
      ADD CONSTRAINT mapping_decisions_revoked_reason_check
      CHECK (
        decision <> 'revoked'
        OR (reason IS NOT NULL AND length(btrim(reason)) >= 5)
      ) NOT VALID;
  END IF;
END $$;

COMMENT ON CONSTRAINT mapping_decisions_revoked_reason_check ON app.mapping_decisions IS
  'Annullamento di colonna: motivo di almeno 5 caratteri. NOT VALID, le righe revoked già scritte senza motivo restano.';
