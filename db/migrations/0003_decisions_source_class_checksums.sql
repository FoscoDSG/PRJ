-- Decisions log, declared source class, checksum column, and ieol_app grants.
-- Safe to re-run: new objects are created only when missing, and existing
-- fact rows keep their source_class. Nothing backfills confidence.
--
-- app.decisions_log is an append-only audit of human decisions. It is not a
-- training set: the order agent does not read this table and does not learn
-- from these rows.
--
-- schema_migrations.checksum is filled by `ieol migrate`. The runner refuses
-- to continue when an applied migration file no longer matches the stored
-- checksum. This file only adds the column.

ALTER TABLE public.schema_migrations ADD COLUMN IF NOT EXISTS checksum text;

CREATE TABLE IF NOT EXISTS app.sources (
  name text PRIMARY KEY,
  source_class text NOT NULL,
  registered_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT sources_source_class_check CHECK (
    source_class IN ('system_of_record', 'operational', 'external', 'manual', 'unknown')
  )
);

ALTER TABLE app.facts DROP CONSTRAINT IF EXISTS facts_source_class_check;
ALTER TABLE app.facts ADD CONSTRAINT facts_source_class_check CHECK (
  source_class IN (
    'erp', 'crm', 'manual', 'ai', 'calculated', 'unknown',
    'system_of_record', 'operational', 'external'
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS landing_rows_batch_row_key
  ON app.landing_rows (batch_id, row_index);

CREATE TABLE IF NOT EXISTS app.decisions_log (
  id uuid PRIMARY KEY,
  recommendation_id uuid NOT NULL REFERENCES app.recommendations (id),
  batch_id uuid NOT NULL,
  row_index integer NOT NULL,
  decision text NOT NULL CHECK (decision IN ('approve', 'reject', 'modify')),
  evidence_hash text NOT NULL,
  recommendation text NOT NULL,
  confidence numeric NOT NULL,
  actor text NOT NULL,
  actor_role text NOT NULL,
  decided_at timestamptz NOT NULL DEFAULT now(),
  agent_version text NOT NULL,
  rules_version text NOT NULL,
  confidence_threshold numeric NOT NULL,
  amount_threshold_eur numeric NOT NULL,
  FOREIGN KEY (batch_id, row_index)
    REFERENCES app.landing_rows (batch_id, row_index)
);

COMMENT ON TABLE app.decisions_log IS
  'Registro append-only delle decisioni umane, collegato a batch_id e row_index. Non è un dataset di training: l''agente non legge questa tabella e non impara da queste righe.';

CREATE INDEX IF NOT EXISTS decisions_log_recommendation_idx
  ON app.decisions_log (recommendation_id);

CREATE OR REPLACE FUNCTION app.prevent_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'append-only';
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'decisions_log_no_update'
      AND tgrelid = 'app.decisions_log'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER decisions_log_no_update
    BEFORE UPDATE OR DELETE ON app.decisions_log
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'decisions_log_no_truncate'
      AND tgrelid = 'app.decisions_log'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER decisions_log_no_truncate
    BEFORE TRUNCATE ON app.decisions_log
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'agent_overrides_no_update'
      AND tgrelid = 'app.agent_overrides'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER agent_overrides_no_update
    BEFORE UPDATE OR DELETE ON app.agent_overrides
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'agent_overrides_no_truncate'
      AND tgrelid = 'app.agent_overrides'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER agent_overrides_no_truncate
    BEFORE TRUNCATE ON app.agent_overrides
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
END;
$$;

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
GRANT SELECT ON public.schema_migrations TO ieol_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO ieol_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO ieol_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA src TO ieol_app;

REVOKE UPDATE, DELETE, TRUNCATE ON app.audit_log FROM ieol_app;
GRANT INSERT, SELECT ON app.audit_log TO ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.decisions_log FROM ieol_app;
GRANT INSERT, SELECT ON app.decisions_log TO ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.agent_overrides FROM ieol_app;
GRANT INSERT, SELECT ON app.agent_overrides TO ieol_app;
REVOKE TRUNCATE ON ALL TABLES IN SCHEMA app FROM ieol_app;
REVOKE TRUNCATE ON ALL TABLES IN SCHEMA src FROM ieol_app;
REVOKE CREATE ON SCHEMA app FROM ieol_app;
REVOKE CREATE ON SCHEMA src FROM ieol_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ieol_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA app
  GRANT USAGE, SELECT ON SEQUENCES TO ieol_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA src
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ieol_app;
