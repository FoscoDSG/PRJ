-- Apprendimento dalle correzioni. Non riscrive le righe già presenti:
-- nessuna istruzione che assegni valori alle righe vecchie. I company_id
-- già assenti restano NULL e non entrano nei suggerimenti.
--
-- La 0007 commentava app.mapping_decisions così: non è un dataset che
-- l'agente legge per proporre. Quella regola si cambia qui, non si aggira.
-- La review del mapping legge la tabella solo per un suggerimento appreso,
-- con lo stesso company_id, la stessa fonte e la stessa intestazione
-- normalizzata. L'agente degli ordini non la legge.
--
-- Allowlist, riga esatta:
--   ALTER TABLE app.mapping_decisions DROP CONSTRAINT IF EXISTS mapping_decisions_decision_check

CREATE TABLE IF NOT EXISTS app.companies (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE app.companies IS
  'Azienda del tenant. Il company_id delle decisioni nuove si legge dall''utente autenticato, non da un parametro del client.';

ALTER TABLE app.users
  ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES app.companies (id);

ALTER TABLE app.sources
  ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES app.companies (id);

ALTER TABLE app.field_mappings
  ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES app.companies (id);

ALTER TABLE app.field_mappings
  ADD COLUMN IF NOT EXISTS origin text;

ALTER TABLE app.field_mappings
  ADD COLUMN IF NOT EXISTS source_decision_id uuid;

ALTER TABLE app.mapping_decisions
  ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES app.companies (id);

ALTER TABLE app.mapping_decisions
  ADD COLUMN IF NOT EXISTS origin text;

ALTER TABLE app.mapping_decisions
  ADD COLUMN IF NOT EXISTS source_decision_id uuid;

ALTER TABLE app.mapping_decisions
  ADD COLUMN IF NOT EXISTS revoked_decision_id uuid;

ALTER TABLE app.landing_batches
  ADD COLUMN IF NOT EXISTS proposed_type text;

ALTER TABLE app.landing_batches
  ADD COLUMN IF NOT EXISTS proposed_score numeric;

ALTER TABLE app.mapping_decisions DROP CONSTRAINT IF EXISTS mapping_decisions_decision_check;

ALTER TABLE app.mapping_decisions ADD CONSTRAINT mapping_decisions_decision_check
  CHECK (decision IN ('confirm', 'reject', 'annullata'));

ALTER TABLE app.mapping_decisions
  ADD CONSTRAINT mapping_decisions_revoked_link_check
  CHECK (
    (decision = 'annullata' AND revoked_decision_id IS NOT NULL)
    OR (decision <> 'annullata' AND revoked_decision_id IS NULL)
  ) NOT VALID;

ALTER TABLE app.mapping_decisions
  ADD CONSTRAINT mapping_decisions_origin_check
  CHECK (
    origin IS NULL
    OR (origin = 'rule' AND source_decision_id IS NULL)
    OR (origin = 'learned' AND source_decision_id IS NOT NULL)
  ) NOT VALID;

ALTER TABLE app.field_mappings
  ADD CONSTRAINT field_mappings_origin_check
  CHECK (
    origin IS NULL
    OR (origin = 'rule' AND source_decision_id IS NULL)
    OR (origin = 'learned' AND source_decision_id IS NOT NULL)
  ) NOT VALID;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'field_mappings_source_decision_fkey'
      AND conrelid = 'app.field_mappings'::regclass
  ) THEN
    ALTER TABLE app.field_mappings
      ADD CONSTRAINT field_mappings_source_decision_fkey
      FOREIGN KEY (source_decision_id) REFERENCES app.mapping_decisions (id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'mapping_decisions_source_decision_fkey'
      AND conrelid = 'app.mapping_decisions'::regclass
  ) THEN
    ALTER TABLE app.mapping_decisions
      ADD CONSTRAINT mapping_decisions_source_decision_fkey
      FOREIGN KEY (source_decision_id) REFERENCES app.mapping_decisions (id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'mapping_decisions_revoked_decision_fkey'
      AND conrelid = 'app.mapping_decisions'::regclass
  ) THEN
    ALTER TABLE app.mapping_decisions
      ADD CONSTRAINT mapping_decisions_revoked_decision_fkey
      FOREIGN KEY (revoked_decision_id) REFERENCES app.mapping_decisions (id);
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS app.table_type_decisions (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL REFERENCES app.companies (id),
  file_id uuid NOT NULL REFERENCES app.landing_batches (id),
  proposed_type text NOT NULL CHECK (proposed_type IN (
    'customer', 'order', 'order_line', 'product', 'stock', 'supplier', 'shipment', 'unknown'
  )),
  score numeric NOT NULL,
  decision text NOT NULL CHECK (decision IN ('corretto', 'sbagliato', 'non_so')),
  correct_type text,
  actor text NOT NULL,
  actor_role text NOT NULL,
  decided_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT table_type_decisions_shape_check CHECK (
    (decision = 'corretto' AND correct_type IS NULL)
    OR (
      decision = 'sbagliato'
      AND correct_type IN (
        'customer', 'order', 'order_line', 'product', 'stock', 'supplier', 'shipment'
      )
    )
    OR (decision = 'non_so' AND correct_type IS NULL)
  )
);

COMMENT ON TABLE app.table_type_decisions IS
  'Decisioni sul tipo di file, append-only, fuori da mapping_decisions. corretto, sbagliato e non_so sono decisioni distinte. non_so è un errore del suggerimento e non è un esempio di apprendimento.';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'table_type_decisions_no_update'
      AND tgrelid = 'app.table_type_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER table_type_decisions_no_update
    BEFORE UPDATE OR DELETE ON app.table_type_decisions
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'table_type_decisions_no_truncate'
      AND tgrelid = 'app.table_type_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER table_type_decisions_no_truncate
    BEFORE TRUNCATE ON app.table_type_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
END $$;

REVOKE UPDATE, DELETE, TRUNCATE ON app.table_type_decisions FROM ieol_app;
GRANT INSERT, SELECT ON app.table_type_decisions TO ieol_app;

CREATE OR REPLACE FUNCTION app.enforce_mapping_revoke_company()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = app, public
AS $$
DECLARE
  actor_company uuid;
  revoked_company uuid;
BEGIN
  IF NEW.decision IS DISTINCT FROM 'annullata' THEN
    RETURN NEW;
  END IF;
  SELECT company_id INTO actor_company FROM app.users WHERE username = NEW.actor;
  SELECT company_id INTO revoked_company
  FROM app.mapping_decisions
  WHERE id = NEW.revoked_decision_id;
  IF actor_company IS NULL
     OR revoked_company IS NULL
     OR actor_company IS DISTINCT FROM revoked_company
     OR NEW.company_id IS DISTINCT FROM actor_company THEN
    RAISE EXCEPTION 'revoca di un''altra azienda';
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'mapping_decisions_same_company'
      AND tgrelid = 'app.mapping_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER mapping_decisions_same_company
    BEFORE INSERT ON app.mapping_decisions
    FOR EACH ROW EXECUTE FUNCTION app.enforce_mapping_revoke_company();
  END IF;
END $$;

CREATE OR REPLACE FUNCTION app.enforce_table_type_company()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = app, public
AS $$
DECLARE
  actor_company uuid;
BEGIN
  SELECT company_id INTO actor_company FROM app.users WHERE username = NEW.actor;
  IF actor_company IS NULL OR NEW.company_id IS DISTINCT FROM actor_company THEN
    RAISE EXCEPTION 'decisione di un''altra azienda';
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'table_type_decisions_same_company'
      AND tgrelid = 'app.table_type_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER table_type_decisions_same_company
    BEFORE INSERT ON app.table_type_decisions
    FOR EACH ROW EXECUTE FUNCTION app.enforce_table_type_company();
  END IF;
END $$;

GRANT SELECT (company_id, origin, source_decision_id) ON app.field_mappings TO ieol_app;

COMMENT ON TABLE app.mapping_decisions IS
  'Decisioni di mapping, append-only. La 0007 diceva che non è un dataset che l''agente legge per proporre. La 0010 cambia quella regola e non la aggira: la review del mapping legge queste righe solo per un suggerimento appreso, con lo stesso company_id, la stessa fonte e la stessa intestazione normalizzata. Un company_id NULL non si usa mai. L''agente degli ordini non legge questa tabella. annullata cita la decisione revocata: l''attore deve appartenere alla stessa azienda.';
