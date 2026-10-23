-- Una decisione già presa non torna in coda. Non riscrive le righe già
-- presenti: nessun UPDATE di dati. Il blocco è un trigger sulle transizioni
-- nuove. Le righe vecchie restano com'erano.
--
-- L'annullamento è una decisione nuova di tipo revoked che cita quella
-- annullata, come la 0010 sulle decisioni di colonna: un solo annullamento
-- per decisione. Vale per il registro inbox (app.decisions_log) e per le
-- tre tabelle di stato (rilascio dalla quarantena, fusione clienti, cambio
-- di colonne visto). La riga originale non si riapre e non si sovrascrive.
--
-- Allowlist, riga esatta:
--   ALTER TABLE app.decisions_log DROP CONSTRAINT IF EXISTS decisions_log_decision_check

CREATE OR REPLACE FUNCTION app.prevent_return_to_queue()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = app, public
AS $$
BEGIN
  IF TG_TABLE_NAME = 'field_mappings' THEN
    IF OLD.status IN ('confirmed', 'rejected') AND NEW.status = 'proposed' THEN
      RAISE EXCEPTION 'decisione già presa: non torna in coda';
    END IF;
  ELSIF TG_TABLE_NAME = 'recommendations' THEN
    IF OLD.status NOT IN ('detected', 'proposed')
       AND NEW.status IN ('detected', 'proposed') THEN
      RAISE EXCEPTION 'decisione già presa: non torna in coda';
    END IF;
  ELSIF TG_TABLE_NAME = 'identity_reviews' THEN
    IF OLD.status IN ('confirmed', 'rejected') AND NEW.status = 'open' THEN
      RAISE EXCEPTION 'decisione già presa: non torna in coda';
    END IF;
  ELSIF TG_TABLE_NAME = 'quarantine_documents' THEN
    IF OLD.status IN ('released', 'rejected') AND NEW.status = 'open' THEN
      RAISE EXCEPTION 'decisione già presa: non torna in coda';
    END IF;
  ELSIF TG_TABLE_NAME = 'drift_reviews' THEN
    IF OLD.status = 'acknowledged' AND NEW.status = 'open' THEN
      RAISE EXCEPTION 'decisione già presa: non torna in coda';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'field_mappings_no_reopen'
      AND tgrelid = 'app.field_mappings'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER field_mappings_no_reopen
    BEFORE UPDATE ON app.field_mappings
    FOR EACH ROW EXECUTE FUNCTION app.prevent_return_to_queue();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'recommendations_no_reopen'
      AND tgrelid = 'app.recommendations'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER recommendations_no_reopen
    BEFORE UPDATE ON app.recommendations
    FOR EACH ROW EXECUTE FUNCTION app.prevent_return_to_queue();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'identity_reviews_no_reopen'
      AND tgrelid = 'app.identity_reviews'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER identity_reviews_no_reopen
    BEFORE UPDATE ON app.identity_reviews
    FOR EACH ROW EXECUTE FUNCTION app.prevent_return_to_queue();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'quarantine_documents_no_reopen'
      AND tgrelid = 'app.quarantine_documents'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER quarantine_documents_no_reopen
    BEFORE UPDATE ON app.quarantine_documents
    FOR EACH ROW EXECUTE FUNCTION app.prevent_return_to_queue();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'drift_reviews_no_reopen'
      AND tgrelid = 'app.drift_reviews'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER drift_reviews_no_reopen
    BEFORE UPDATE ON app.drift_reviews
    FOR EACH ROW EXECUTE FUNCTION app.prevent_return_to_queue();
  END IF;
END $$;

ALTER TABLE app.mapping_decisions
  ADD COLUMN IF NOT EXISTS reason text;

COMMENT ON COLUMN app.mapping_decisions.reason IS
  'Motivo dell''annullamento. Le righe revoked già scritte restano senza motivo: il controllo del testo è sulla decisione nuova.';

ALTER TABLE app.decisions_log DROP CONSTRAINT IF EXISTS decisions_log_decision_check;

ALTER TABLE app.decisions_log
  ADD CONSTRAINT decisions_log_decision_check
  CHECK (decision IN ('approve', 'reject', 'modify', 'revoked'));

ALTER TABLE app.decisions_log
  ADD COLUMN IF NOT EXISTS revoked_decision_id uuid;

ALTER TABLE app.decisions_log
  ADD COLUMN IF NOT EXISTS reason text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'decisions_log_revoked_decision_fkey'
      AND conrelid = 'app.decisions_log'::regclass
  ) THEN
    ALTER TABLE app.decisions_log
      ADD CONSTRAINT decisions_log_revoked_decision_fkey
      FOREIGN KEY (revoked_decision_id) REFERENCES app.decisions_log (id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'decisions_log_revoked_link_check'
      AND conrelid = 'app.decisions_log'::regclass
  ) THEN
    ALTER TABLE app.decisions_log
      ADD CONSTRAINT decisions_log_revoked_link_check
      CHECK (
        (decision = 'revoked' AND revoked_decision_id IS NOT NULL)
        OR (decision <> 'revoked' AND revoked_decision_id IS NULL)
      ) NOT VALID;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'decisions_log_revoked_reason_check'
      AND conrelid = 'app.decisions_log'::regclass
  ) THEN
    ALTER TABLE app.decisions_log
      ADD CONSTRAINT decisions_log_revoked_reason_check
      CHECK (
        decision <> 'revoked'
        OR (reason IS NOT NULL AND length(btrim(reason)) >= 5)
      ) NOT VALID;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS decisions_log_one_revocation
  ON app.decisions_log (revoked_decision_id)
  WHERE revoked_decision_id IS NOT NULL;

COMMENT ON COLUMN app.decisions_log.revoked_decision_id IS
  'Decisione inbox annullata. Solo su una riga revoked, una sola volta. Le righe già scritte restano NULL.';

COMMENT ON COLUMN app.decisions_log.reason IS
  'Motivo obbligatorio dell''annullamento. NULL sulle decisioni che non sono un annullamento e sulle righe scritte prima della 0013.';

CREATE OR REPLACE FUNCTION app.enforce_decision_log_company()
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
  FROM app.decisions_log
  WHERE id = NEW.revoked_decision_id;
  IF revoked_kind IS NULL OR revoked_kind NOT IN ('approve', 'reject', 'modify') THEN
    RAISE EXCEPTION 'si revoca solo una decisione già presa';
  END IF;
  IF revoked_company IS DISTINCT FROM actor_company THEN
    RAISE EXCEPTION 'revoca di un''altra azienda';
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION app.enforce_state_revocation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = app, public
AS $$
DECLARE
  actor_company uuid;
  subject_status text;
  subject_company uuid;
BEGIN
  IF NEW.decision IS DISTINCT FROM 'revoked' OR NEW.revoked_decision_id IS NULL THEN
    RAISE EXCEPTION 'si revoca solo con una decisione revoked';
  END IF;
  IF NEW.reason IS NULL OR length(btrim(NEW.reason)) < 5 THEN
    RAISE EXCEPTION 'motivo dell''annullamento obbligatorio';
  END IF;
  IF NEW.company_id IS NULL THEN
    RAISE EXCEPTION 'decisione senza azienda';
  END IF;
  SELECT company_id INTO actor_company FROM app.users WHERE username = NEW.actor;
  IF actor_company IS NULL OR actor_company IS DISTINCT FROM NEW.company_id THEN
    RAISE EXCEPTION 'decisione di un''altra azienda';
  END IF;
  IF TG_TABLE_NAME = 'quarantine_decisions' THEN
    SELECT status, company_id INTO subject_status, subject_company
    FROM app.quarantine_documents
    WHERE id = NEW.revoked_decision_id;
    IF subject_status IS NULL OR subject_status NOT IN ('released', 'rejected') THEN
      RAISE EXCEPTION 'si revoca solo una decisione già presa';
    END IF;
  ELSIF TG_TABLE_NAME = 'identity_review_decisions' THEN
    SELECT status, company_id INTO subject_status, subject_company
    FROM app.identity_reviews
    WHERE id = NEW.revoked_decision_id;
    IF subject_status IS NULL OR subject_status NOT IN ('confirmed', 'rejected') THEN
      RAISE EXCEPTION 'si revoca solo una decisione già presa';
    END IF;
  ELSIF TG_TABLE_NAME = 'drift_decisions' THEN
    SELECT status, company_id INTO subject_status, subject_company
    FROM app.drift_reviews
    WHERE id = NEW.revoked_decision_id;
    IF subject_status IS NULL OR subject_status <> 'acknowledged' THEN
      RAISE EXCEPTION 'si revoca solo una decisione già presa';
    END IF;
  ELSE
    RAISE EXCEPTION 'tabella di revoca non prevista';
  END IF;
  IF subject_company IS DISTINCT FROM actor_company THEN
    RAISE EXCEPTION 'revoca di un''altra azienda';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TABLE IF NOT EXISTS app.quarantine_decisions (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL REFERENCES app.companies (id),
  decision text NOT NULL CHECK (decision = 'revoked'),
  revoked_decision_id uuid NOT NULL REFERENCES app.quarantine_documents (id),
  reason text NOT NULL,
  actor text NOT NULL,
  actor_role text NOT NULL,
  decided_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT quarantine_decisions_link_check CHECK (
    decision = 'revoked' AND revoked_decision_id IS NOT NULL
  ),
  CONSTRAINT quarantine_decisions_reason_check CHECK (length(btrim(reason)) >= 5)
);

CREATE UNIQUE INDEX IF NOT EXISTS quarantine_decisions_one_revocation
  ON app.quarantine_decisions (revoked_decision_id);

COMMENT ON TABLE app.quarantine_decisions IS
  'Annullamento di un rilascio o di un rifiuto in quarantena. La riga revoked cita il documento, una sola volta. Non riapre la quarantena e non compensa.';

CREATE TABLE IF NOT EXISTS app.identity_review_decisions (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL REFERENCES app.companies (id),
  decision text NOT NULL CHECK (decision = 'revoked'),
  revoked_decision_id uuid NOT NULL REFERENCES app.identity_reviews (id),
  reason text NOT NULL,
  actor text NOT NULL,
  actor_role text NOT NULL,
  decided_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT identity_review_decisions_link_check CHECK (
    decision = 'revoked' AND revoked_decision_id IS NOT NULL
  ),
  CONSTRAINT identity_review_decisions_reason_check CHECK (length(btrim(reason)) >= 5)
);

CREATE UNIQUE INDEX IF NOT EXISTS identity_review_decisions_one_revocation
  ON app.identity_review_decisions (revoked_decision_id);

COMMENT ON TABLE app.identity_review_decisions IS
  'Annullamento di una fusione clienti confermata o rifiutata. La riga revoked cita la revisione, una sola volta. Non rimette la coppia in coda e non scioglie la fusione.';

CREATE TABLE IF NOT EXISTS app.drift_decisions (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL REFERENCES app.companies (id),
  decision text NOT NULL CHECK (decision = 'revoked'),
  revoked_decision_id uuid NOT NULL REFERENCES app.drift_reviews (id),
  reason text NOT NULL,
  actor text NOT NULL,
  actor_role text NOT NULL,
  decided_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT drift_decisions_link_check CHECK (
    decision = 'revoked' AND revoked_decision_id IS NOT NULL
  ),
  CONSTRAINT drift_decisions_reason_check CHECK (length(btrim(reason)) >= 5)
);

CREATE UNIQUE INDEX IF NOT EXISTS drift_decisions_one_revocation
  ON app.drift_decisions (revoked_decision_id);

COMMENT ON TABLE app.drift_decisions IS
  'Annullamento di un cambio di colonne segnato come visto. La riga revoked cita la revisione, una sola volta. Non rimette il cambio in coda.';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'quarantine_decisions_company'
      AND tgrelid = 'app.quarantine_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER quarantine_decisions_company
    BEFORE INSERT ON app.quarantine_decisions
    FOR EACH ROW EXECUTE FUNCTION app.enforce_state_revocation();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'identity_review_decisions_company'
      AND tgrelid = 'app.identity_review_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER identity_review_decisions_company
    BEFORE INSERT ON app.identity_review_decisions
    FOR EACH ROW EXECUTE FUNCTION app.enforce_state_revocation();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'drift_decisions_company'
      AND tgrelid = 'app.drift_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER drift_decisions_company
    BEFORE INSERT ON app.drift_decisions
    FOR EACH ROW EXECUTE FUNCTION app.enforce_state_revocation();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'quarantine_decisions_no_update'
      AND tgrelid = 'app.quarantine_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER quarantine_decisions_no_update
    BEFORE UPDATE OR DELETE ON app.quarantine_decisions
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'quarantine_decisions_no_truncate'
      AND tgrelid = 'app.quarantine_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER quarantine_decisions_no_truncate
    BEFORE TRUNCATE ON app.quarantine_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'identity_review_decisions_no_update'
      AND tgrelid = 'app.identity_review_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER identity_review_decisions_no_update
    BEFORE UPDATE OR DELETE ON app.identity_review_decisions
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'identity_review_decisions_no_truncate'
      AND tgrelid = 'app.identity_review_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER identity_review_decisions_no_truncate
    BEFORE TRUNCATE ON app.identity_review_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'drift_decisions_no_update'
      AND tgrelid = 'app.drift_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER drift_decisions_no_update
    BEFORE UPDATE OR DELETE ON app.drift_decisions
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'drift_decisions_no_truncate'
      AND tgrelid = 'app.drift_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER drift_decisions_no_truncate
    BEFORE TRUNCATE ON app.drift_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
END $$;

REVOKE UPDATE, DELETE, TRUNCATE ON app.quarantine_decisions FROM ieol_app;
GRANT INSERT, SELECT ON app.quarantine_decisions TO ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.identity_review_decisions FROM ieol_app;
GRANT INSERT, SELECT ON app.identity_review_decisions TO ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.drift_decisions FROM ieol_app;
GRANT INSERT, SELECT ON app.drift_decisions TO ieol_app;
