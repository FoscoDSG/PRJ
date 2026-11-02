-- Schema iniziale, equivalente a db/schema.sql.
-- Rieseguire questo file non cancella righe: gli oggetti si creano solo se mancano.
-- Un database già creato con db/schema.sql non passa da qui: il runner lo marca come baseline.

CREATE SCHEMA IF NOT EXISTS app;
CREATE SCHEMA IF NOT EXISTS src;

CREATE TABLE IF NOT EXISTS app.users (
  username text PRIMARY KEY,
  password_hash text NOT NULL,
  role text NOT NULL CHECK (role IN ('admin', 'mapper', 'operator', 'approver'))
);

CREATE TABLE IF NOT EXISTS app.audit_log (
  id uuid PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  actor text,
  role text,
  action text NOT NULL CHECK (action IN (
    'proposal', 'human_decision', 'dry_run', 'external_call', 'outcome'
  )),
  entity_ref text,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE OR REPLACE FUNCTION app.prevent_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'audit log is append-only';
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'audit_no_update'
      AND tgrelid = 'app.audit_log'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER audit_no_update
    BEFORE UPDATE OR DELETE ON app.audit_log
    FOR EACH ROW EXECUTE FUNCTION app.prevent_audit_mutation();
  END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS app.ontology_versions (
  id uuid PRIMARY KEY,
  package text NOT NULL,
  version text NOT NULL,
  changelog text NOT NULL DEFAULT '',
  loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.schema_snapshots (
  id uuid PRIMARY KEY,
  source_name text NOT NULL,
  column_hash text NOT NULL,
  columns jsonb NOT NULL,
  captured_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.field_mappings (
  id uuid PRIMARY KEY,
  source_name text NOT NULL,
  source_field text NOT NULL,
  target_type text NOT NULL,
  target_property text NOT NULL,
  confidence numeric NOT NULL,
  status text NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected')),
  samples jsonb NOT NULL DEFAULT '[]'::jsonb,
  profile text NOT NULL,
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz
);

CREATE TABLE IF NOT EXISTS app.drift_reviews (
  id uuid PRIMARY KEY,
  source_name text NOT NULL,
  previous_hash text,
  new_hash text NOT NULL,
  diff jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('open', 'acknowledged')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.landing_batches (
  id uuid PRIMARY KEY,
  source_name text NOT NULL,
  watermark text NOT NULL,
  row_count integer NOT NULL,
  loaded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_name, watermark)
);

CREATE TABLE IF NOT EXISTS app.landing_rows (
  id bigserial PRIMARY KEY,
  batch_id uuid NOT NULL REFERENCES app.landing_batches (id) ON DELETE CASCADE,
  row_index integer NOT NULL,
  data jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS app.ingest_watermarks (
  source_name text PRIMARY KEY,
  watermark text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.entities (
  id uuid PRIMARY KEY,
  type text NOT NULL,
  business_key text NOT NULL,
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz,
  UNIQUE (type, business_key)
);

CREATE TABLE IF NOT EXISTS app.identity_aliases (
  entity_type text NOT NULL,
  alias text NOT NULL,
  entity_id uuid NOT NULL REFERENCES app.entities (id) ON DELETE CASCADE,
  PRIMARY KEY (entity_type, alias)
);

CREATE TABLE IF NOT EXISTS app.facts (
  id uuid PRIMARY KEY,
  entity_id uuid NOT NULL REFERENCES app.entities (id) ON DELETE CASCADE,
  property text NOT NULL,
  value jsonb NOT NULL,
  classification text NOT NULL,
  origin text NOT NULL CHECK (origin IN (
    'original', 'transformed', 'inferred', 'approved', 'calculated'
  )),
  source text,
  confidence numeric,
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz
);

CREATE INDEX IF NOT EXISTS facts_open_idx ON app.facts (entity_id, property) WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS app.graph_edges (
  id uuid PRIMARY KEY,
  from_id uuid NOT NULL REFERENCES app.entities (id) ON DELETE CASCADE,
  rel text NOT NULL,
  to_id uuid NOT NULL REFERENCES app.entities (id) ON DELETE CASCADE,
  origin text NOT NULL DEFAULT 'transformed',
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz
);

CREATE TABLE IF NOT EXISTS app.recommendations (
  id uuid PRIMARY KEY,
  order_entity_id uuid NOT NULL REFERENCES app.entities (id),
  problem text NOT NULL,
  evidence jsonb NOT NULL,
  evidence_hash text NOT NULL,
  actions jsonb NOT NULL,
  expected_impact text NOT NULL,
  risk text NOT NULL,
  confidence numeric NOT NULL,
  status text NOT NULL CHECK (status IN (
    'detected', 'proposed', 'approved', 'modified', 'rejected',
    'dry_run_ok', 'executed', 'failed', 'expired'
  )),
  requires_cfo boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.workflow_events (
  id uuid PRIMARY KEY,
  recommendation_id uuid NOT NULL REFERENCES app.recommendations (id) ON DELETE CASCADE,
  from_status text,
  to_status text NOT NULL,
  actor text,
  at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.action_batches (
  id uuid PRIMARY KEY,
  recommendation_id uuid NOT NULL REFERENCES app.recommendations (id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('dry_run', 'execute')),
  status text NOT NULL,
  payloads jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.sandbox_documents (
  id uuid PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('sales_order', 'purchase_order')),
  idempotency_key text NOT NULL UNIQUE,
  payload jsonb NOT NULL,
  status text NOT NULL,
  compensation jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.email_outbox (
  id uuid PRIMARY KEY,
  idempotency_key text NOT NULL UNIQUE,
  recipient text,
  subject text NOT NULL,
  body text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS src.clienti (
  anag_cli text,
  ragione_sociale text,
  piva text,
  email text
);

CREATE TABLE IF NOT EXISTS src.ordini (
  ord_head text,
  anag_cli text,
  importo numeric,
  stato text,
  data text
);

CREATE TABLE IF NOT EXISTS src.righe (
  ord_head text,
  cod_art text,
  qty numeric,
  prezzo numeric
);

CREATE TABLE IF NOT EXISTS src.articoli (
  cod_art text,
  descrizione text,
  costo numeric
);

CREATE TABLE IF NOT EXISTS src.giacenze (
  cod_art text,
  magazzino text,
  giacenza numeric,
  impegnato numeric
);

CREATE TABLE IF NOT EXISTS src.fornitori (
  forn text,
  nome text,
  cod_art text,
  ritardo_giorni numeric
);
