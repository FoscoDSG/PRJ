-- Provenance, append-only audit against TRUNCATE, and the V1 quality tables.
-- Safe to re-run: columns and tables are created only when missing, and
-- existing fact and audit rows are kept. A database that already has these
-- objects is baselined and this file is not executed.

ALTER TABLE app.facts ADD COLUMN IF NOT EXISTS source_class text NOT NULL DEFAULT 'manual';
UPDATE app.facts SET confidence = 1 WHERE confidence IS NULL;
ALTER TABLE app.facts ALTER COLUMN confidence SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'audit_no_truncate'
      AND tgrelid = 'app.audit_log'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER audit_no_truncate
    BEFORE TRUNCATE ON app.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_audit_mutation();
  END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS app.identity_reviews (
  id uuid PRIMARY KEY,
  entity_type text NOT NULL,
  left_key text NOT NULL,
  right_key text NOT NULL,
  score numeric NOT NULL,
  reason text NOT NULL,
  status text NOT NULL CHECK (status IN ('open', 'confirmed', 'rejected')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entity_type, left_key, right_key)
);

CREATE TABLE IF NOT EXISTS app.agent_overrides (
  id uuid PRIMARY KEY,
  recommendation_id uuid NOT NULL REFERENCES app.recommendations (id) ON DELETE CASCADE,
  field_name text NOT NULL,
  previous_value jsonb NOT NULL,
  new_value jsonb NOT NULL,
  actor text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.quality_issues (
  id uuid PRIMARY KEY,
  entity_type text NOT NULL,
  business_key text,
  check_name text NOT NULL,
  detail text NOT NULL,
  status text NOT NULL DEFAULT 'open',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.price_list (
  product_source_key text PRIMARY KEY,
  price numeric NOT NULL
);

INSERT INTO app.price_list (product_source_key, price) VALUES
  ('ART-X', 120),
  ('ART-Y', 200)
ON CONFLICT (product_source_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS app.quarantine_documents (
  id uuid PRIMARY KEY,
  source_name text NOT NULL,
  order_key text,
  reasons jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('open', 'released', 'rejected')),
  created_at timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ieol_owner') THEN
    CREATE ROLE ieol_owner NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ieol_app') THEN
    CREATE ROLE ieol_app NOLOGIN;
  END IF;
END $$;

GRANT ieol_app TO CURRENT_USER;
GRANT USAGE ON SCHEMA app, src TO ieol_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO ieol_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.audit_log FROM ieol_app;
GRANT INSERT, SELECT ON app.audit_log TO ieol_app;
REVOKE TRUNCATE ON ALL TABLES IN SCHEMA app FROM ieol_app;
GRANT SELECT ON ALL TABLES IN SCHEMA src TO ieol_app;
REVOKE CREATE ON SCHEMA app FROM ieol_app;
