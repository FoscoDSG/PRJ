-- Modulo ordine-incasso. Non riscrive le righe già presenti: nessun UPDATE di dati.
-- Le proposte di abbinamento e le decisioni umane sono per azienda.
-- La decisione è append-only. revoked è una riga nuova.
-- IBAN e causale entrano nell'elenco dei dati personali.

ALTER TABLE app.recommendations
  ADD COLUMN IF NOT EXISTS process_id text,
  ADD COLUMN IF NOT EXISTS step_id text;

COMMENT ON COLUMN app.recommendations.process_id IS
  'Processo dell''eccezione, lo stesso id restituito da /api/ontology. NULL sulle righe scritte prima: in lettura è l''eccezione d''ordine.';

COMMENT ON COLUMN app.recommendations.step_id IS
  'Passo del processo. NULL sulle righe scritte prima: in lettura è il passo disponibilità.';

CREATE OR REPLACE FUNCTION app.key_is_pii(key text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT lower(coalesce(key, '')) IN (
    'company_name', 'full_name', 'customer_name', 'email', 'e_mail', 'mail', 'pec',
    'vat', 'piva', 'partita_iva', 'phone', 'telephone', 'telefono', 'cellulare',
    'tel', 'mobile', 'iban', 'address', 'indirizzo', 'codice_fiscale', 'tax_code',
    'cf', 'via', 'cap', 'citta', 'city', 'comune', 'provincia', 'ragione_sociale',
    'nome', 'recipient', 'referente', 'intestatario', 'cognome',
    'causale', 'customer_vat', 'counterparty_name', 'counterparty_vat'
  );
$$;

CREATE TABLE IF NOT EXISTS app.payment_proposals (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL REFERENCES app.companies (id),
  payment_entity_id uuid NOT NULL REFERENCES app.entities (id),
  invoice_entity_id uuid NOT NULL REFERENCES app.entities (id),
  allocation_entity_id uuid NOT NULL REFERENCES app.entities (id),
  match_group uuid NOT NULL,
  source_name text NOT NULL,
  label_origin text NOT NULL CHECK (label_origin IN ('rule', 'synthetic')),
  criteria jsonb NOT NULL,
  score numeric NOT NULL,
  confidence_method text NOT NULL CHECK (confidence_method = 'rule'),
  confidence_rule text NOT NULL,
  allocated_amount numeric NOT NULL,
  evidence jsonb NOT NULL,
  evidence_hash text NOT NULL,
  process_id text NOT NULL,
  step_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE app.payment_proposals IS
  'Proposte di abbinamento incasso-fattura. Il punteggio è un punteggio di regola, non una probabilità. La regola non chiude la fattura. label_origin synthetic non entra nella misura.';

COMMENT ON COLUMN app.payment_proposals.criteria IS
  'Criteri usati: amount, customer, reference. Una correzione umana mostra quale non regge.';

COMMENT ON COLUMN app.payment_proposals.company_id IS
  'Azienda della proposta. Un''altra azienda non la legge.';

CREATE INDEX IF NOT EXISTS payment_proposals_company_idx
  ON app.payment_proposals (company_id, payment_entity_id);

CREATE TABLE IF NOT EXISTS app.payment_decisions (
  id uuid PRIMARY KEY,
  proposal_id uuid REFERENCES app.payment_proposals (id),
  payment_entity_id uuid REFERENCES app.entities (id),
  company_id uuid NOT NULL REFERENCES app.companies (id),
  decision text NOT NULL CHECK (decision IN ('confirm', 'correct', 'reject', 'revoked')),
  actor text NOT NULL,
  actor_role text NOT NULL,
  decided_at timestamptz NOT NULL DEFAULT now(),
  invoice_entity_id uuid,
  allocated_amount numeric,
  failed_criteria jsonb,
  reason text,
  revokes_decision_id uuid,
  source_name text
);

COMMENT ON TABLE app.payment_decisions IS
  'Decisioni umane sull''abbinamento, append-only. Sono le sole etichette. revoked è una riga nuova e non cancella la precedente.';

CREATE OR REPLACE FUNCTION app.payment_decision_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  owner uuid;
BEGIN
  IF NEW.company_id IS NULL THEN
    RAISE EXCEPTION 'decisione senza azienda';
  END IF;
  IF NEW.proposal_id IS NOT NULL THEN
    SELECT company_id INTO owner FROM app.payment_proposals WHERE id = NEW.proposal_id;
    IF owner IS NULL OR owner IS DISTINCT FROM NEW.company_id THEN
      RAISE EXCEPTION 'proposta di un''altra azienda';
    END IF;
  ELSIF NEW.payment_entity_id IS NOT NULL THEN
    SELECT company_id INTO owner FROM app.entities WHERE id = NEW.payment_entity_id;
    IF owner IS NULL OR owner IS DISTINCT FROM NEW.company_id THEN
      RAISE EXCEPTION 'pagamento di un''altra azienda';
    END IF;
  ELSE
    RAISE EXCEPTION 'decisione senza oggetto';
  END IF;
  IF NEW.decision = 'revoked' THEN
    IF NEW.revokes_decision_id IS NULL THEN
      RAISE EXCEPTION 'revoca senza decisione';
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM app.payment_decisions
      WHERE id = NEW.revokes_decision_id AND company_id = NEW.company_id
    ) THEN
      RAISE EXCEPTION 'decisione da revocare assente';
    END IF;
    IF EXISTS (
      SELECT 1 FROM app.payment_decisions WHERE revokes_decision_id = NEW.revokes_decision_id
    ) THEN
      RAISE EXCEPTION 'decisione gia revocata';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'payment_decisions_company'
      AND tgrelid = 'app.payment_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER payment_decisions_company
      BEFORE INSERT ON app.payment_decisions
      FOR EACH ROW EXECUTE FUNCTION app.payment_decision_guard();
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'payment_decisions_no_update'
      AND tgrelid = 'app.payment_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER payment_decisions_no_update
    BEFORE UPDATE OR DELETE ON app.payment_decisions
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'payment_decisions_no_truncate'
      AND tgrelid = 'app.payment_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER payment_decisions_no_truncate
    BEFORE TRUNCATE ON app.payment_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS app.cash_documents (
  entity_id uuid PRIMARY KEY REFERENCES app.entities (id),
  company_id uuid NOT NULL REFERENCES app.companies (id),
  source_name text NOT NULL,
  label_origin text NOT NULL CHECK (label_origin IN ('rule', 'synthetic'))
);

COMMENT ON TABLE app.cash_documents IS
  'Origine del documento di cassa. synthetic è dichiarato sul file di prova e resta fuori dalla misura.';

CREATE INDEX IF NOT EXISTS cash_documents_company_idx
  ON app.cash_documents (company_id, source_name);

GRANT SELECT, INSERT ON app.cash_documents TO ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.cash_documents FROM ieol_app;

GRANT SELECT, INSERT ON app.payment_proposals TO ieol_app;
GRANT SELECT, INSERT ON app.payment_decisions TO ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.payment_decisions FROM ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.payment_proposals FROM ieol_app;
