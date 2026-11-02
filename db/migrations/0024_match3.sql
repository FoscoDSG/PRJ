-- Tre vie: proposte e decisioni umane, per azienda.
-- Stesso modello della 0017. Nessun aggiornamento di dati già presenti e nessun backfill.
-- La decisione è append-only. Una revoca è una riga nuova.
-- Il motivo di correzione, rifiuto e revoca ha almeno 5 caratteri.
-- Il CHECK è NOT VALID: non rilegge righe già scritte.
-- ieol_app può solo leggere e inserire.

CREATE TABLE IF NOT EXISTS app.match3_proposals (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL REFERENCES app.companies (id),
  supplier_invoice_entity_id uuid NOT NULL REFERENCES app.entities (id),
  purchase_order_entity_id uuid REFERENCES app.entities (id),
  outcome text NOT NULL CHECK (outcome IN (
    'MATCH_OK',
    'QTY_NOT_RECEIVED',
    'QTY_OVER',
    'PRICE_OUT_OF_TOL',
    'VAT_MISMATCH',
    'TOTAL_MISMATCH',
    'NO_PO',
    'DUPLICATE_INVOICE'
  )),
  source_name text NOT NULL,
  label_origin text NOT NULL CHECK (label_origin IN ('rule', 'synthetic')),
  criteria jsonb NOT NULL,
  score numeric NOT NULL,
  confidence_method text NOT NULL CHECK (confidence_method = 'rule'),
  confidence_rule text NOT NULL,
  evidence jsonb NOT NULL,
  evidence_hash text NOT NULL,
  process_id text NOT NULL,
  step_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE app.match3_proposals IS
  'Proposte delle tre vie su una fattura fornitore. Il punteggio è un punteggio di regola, non una probabilità. La regola non chiude la fattura. label_origin synthetic non entra in una misura.';

COMMENT ON COLUMN app.match3_proposals.company_id IS
  'Azienda della proposta. Un''altra azienda non la legge.';

COMMENT ON COLUMN app.match3_proposals.outcome IS
  'Esito della regola: MATCH_OK oppure un''eccezione. Non è una chiusura.';

CREATE INDEX IF NOT EXISTS match3_proposals_company_idx
  ON app.match3_proposals (company_id, supplier_invoice_entity_id);

CREATE TABLE IF NOT EXISTS app.match3_decisions (
  id uuid PRIMARY KEY,
  proposal_id uuid REFERENCES app.match3_proposals (id),
  supplier_invoice_entity_id uuid REFERENCES app.entities (id),
  company_id uuid NOT NULL REFERENCES app.companies (id),
  decision text NOT NULL CHECK (decision IN ('confirm', 'correct', 'reject', 'revoked')),
  actor text NOT NULL,
  actor_role text NOT NULL,
  decided_at timestamptz NOT NULL DEFAULT now(),
  failed_criteria jsonb,
  reason text,
  revokes_decision_id uuid,
  source_name text
);

COMMENT ON TABLE app.match3_decisions IS
  'Decisioni umane sulle tre vie, append-only. Sono le sole etichette. revoked è una riga nuova e non cancella la precedente.';

COMMENT ON COLUMN app.match3_decisions.decided_at IS
  'Istante della decisione. Una revoca ha il proprio istante.';

CREATE OR REPLACE FUNCTION app.match3_decision_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  owner uuid;
  kind text;
BEGIN
  IF NEW.company_id IS NULL THEN
    RAISE EXCEPTION 'decisione senza azienda';
  END IF;
  IF NEW.proposal_id IS NOT NULL THEN
    SELECT company_id INTO owner FROM app.match3_proposals WHERE id = NEW.proposal_id;
    IF owner IS NULL OR owner IS DISTINCT FROM NEW.company_id THEN
      RAISE EXCEPTION 'proposta di un''altra azienda';
    END IF;
  ELSIF NEW.supplier_invoice_entity_id IS NOT NULL THEN
    SELECT company_id, type INTO owner, kind FROM app.entities WHERE id = NEW.supplier_invoice_entity_id;
    IF owner IS NULL OR owner IS DISTINCT FROM NEW.company_id THEN
      RAISE EXCEPTION 'fattura di un''altra azienda';
    END IF;
    IF kind IS DISTINCT FROM 'SupplierInvoice' THEN
      RAISE EXCEPTION 'oggetto diverso dalla fattura fornitore';
    END IF;
  ELSE
    RAISE EXCEPTION 'decisione senza oggetto';
  END IF;
  IF NEW.supplier_invoice_entity_id IS NOT NULL AND NEW.proposal_id IS NOT NULL THEN
    SELECT company_id, type INTO owner, kind
    FROM app.entities WHERE id = NEW.supplier_invoice_entity_id;
    IF owner IS NULL OR owner IS DISTINCT FROM NEW.company_id OR kind IS DISTINCT FROM 'SupplierInvoice' THEN
      RAISE EXCEPTION 'fattura di un''altra azienda';
    END IF;
  END IF;
  IF NEW.decision = 'revoked' THEN
    IF NEW.revokes_decision_id IS NULL THEN
      RAISE EXCEPTION 'revoca senza decisione';
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM app.match3_decisions
      WHERE id = NEW.revokes_decision_id AND company_id = NEW.company_id
    ) THEN
      RAISE EXCEPTION 'decisione da revocare assente';
    END IF;
    IF EXISTS (
      SELECT 1 FROM app.match3_decisions WHERE revokes_decision_id = NEW.revokes_decision_id
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
    WHERE tgname = 'match3_decisions_company'
      AND tgrelid = 'app.match3_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER match3_decisions_company
      BEFORE INSERT ON app.match3_decisions
      FOR EACH ROW EXECUTE FUNCTION app.match3_decision_guard();
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'match3_proposals_no_update'
      AND tgrelid = 'app.match3_proposals'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER match3_proposals_no_update
    BEFORE UPDATE OR DELETE ON app.match3_proposals
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'match3_proposals_no_truncate'
      AND tgrelid = 'app.match3_proposals'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER match3_proposals_no_truncate
    BEFORE TRUNCATE ON app.match3_proposals
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'match3_decisions_no_update'
      AND tgrelid = 'app.match3_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER match3_decisions_no_update
    BEFORE UPDATE OR DELETE ON app.match3_decisions
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'match3_decisions_no_truncate'
      AND tgrelid = 'app.match3_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER match3_decisions_no_truncate
    BEFORE TRUNCATE ON app.match3_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
END $$;

ALTER TABLE app.match3_decisions
  ADD CONSTRAINT match3_decisions_reason_check
  CHECK (
    decision = 'confirm'
    OR char_length(btrim(coalesce(reason, ''))) >= 5
  ) NOT VALID;

COMMENT ON CONSTRAINT match3_decisions_reason_check ON app.match3_decisions IS
  'Correggi, Rifiuta e Annulla esigono un motivo di almeno 5 caratteri. La conferma può restare senza motivo. Il controllo non rilegge le righe già presenti.';

GRANT SELECT, INSERT ON app.match3_proposals TO ieol_app;
GRANT SELECT, INSERT ON app.match3_decisions TO ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.match3_proposals FROM ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.match3_decisions FROM ieol_app;
