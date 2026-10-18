-- Assegnazione azienda. Non riscrive le righe già presenti: nessuna
-- istruzione UPDATE. I company_id NULL di utenti, fonti e batch restano
-- NULL. is_demo nasce false: un'azienda già creata non è demo.
--
-- L'azienda di un utente si scrive una sola volta, da NULL a un valore.
-- Un batch e una fonte non ricevono e non cambiano azienda dopo la
-- creazione. Le righe nuove prendono l'azienda in INSERT.
--
-- Allowlist, riga esatta:
--   ALTER TABLE app.audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check

ALTER TABLE app.companies
  ADD COLUMN IF NOT EXISTS is_demo boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN app.companies.is_demo IS
  'Azienda demo. Le misure di riconoscimento colonne e di tipo file escludono queste righe sempre, non solo la prima volta. Si usa il flag, mai il nome.';

COMMENT ON TABLE app.companies IS
  'Azienda del tenant. Si crea solo dal comando amministrativo sul server. is_demo marca la demo: il nome non conta. Un''azienda pilota si crea solo dopo la firma dell''accordo sui dati: è una regola operativa, non un controllo di questo schema.';

CREATE OR REPLACE FUNCTION app.enforce_user_company_once()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = app, public
AS $$
BEGIN
  IF OLD.company_id IS NOT NULL AND NEW.company_id IS DISTINCT FROM OLD.company_id THEN
    RAISE EXCEPTION 'l''azienda di un utente si assegna una sola volta: per cambiarla serve un utente nuovo';
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION app.enforce_source_company_frozen()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = app, public
AS $$
BEGIN
  IF NEW.company_id IS DISTINCT FROM OLD.company_id THEN
    RAISE EXCEPTION 'l''azienda di una fonte non si aggiunge e non si cambia';
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION app.enforce_batch_company_frozen()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = app, public
AS $$
BEGIN
  IF NEW.company_id IS DISTINCT FROM OLD.company_id THEN
    RAISE EXCEPTION 'l''azienda di un batch non si aggiunge e non si cambia';
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'users_company_once'
      AND tgrelid = 'app.users'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER users_company_once
    BEFORE UPDATE ON app.users
    FOR EACH ROW EXECUTE FUNCTION app.enforce_user_company_once();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'sources_company_frozen'
      AND tgrelid = 'app.sources'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER sources_company_frozen
    BEFORE UPDATE ON app.sources
    FOR EACH ROW EXECUTE FUNCTION app.enforce_source_company_frozen();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'landing_batches_company_frozen'
      AND tgrelid = 'app.landing_batches'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER landing_batches_company_frozen
    BEFORE UPDATE ON app.landing_batches
    FOR EACH ROW EXECUTE FUNCTION app.enforce_batch_company_frozen();
  END IF;
END $$;

ALTER TABLE app.audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check;

ALTER TABLE app.audit_log ADD CONSTRAINT audit_log_action_check CHECK (action IN (
  'proposal', 'human_decision', 'dry_run', 'external_call', 'outcome',
  'pii_unmask', 'pii_read', 'pii_mask', 'evidence_key_rotate',
  'company_create', 'user_company_assign'
));

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON app.companies FROM ieol_app;
GRANT SELECT ON app.companies TO ieol_app;

REVOKE INSERT, UPDATE ON app.users FROM ieol_app;
GRANT INSERT (username, password_hash, role) ON app.users TO ieol_app;
GRANT UPDATE (password_hash) ON app.users TO ieol_app;
