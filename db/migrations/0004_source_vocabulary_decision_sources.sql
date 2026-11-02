-- Single source_class vocabulary, decision confidence, and every source row
-- behind a decision. Safe to re-run.
--
-- Legacy fact classes are converted before the CHECK is replaced:
--   erp        -> system_of_record
--   crm        -> operational
--   ai         -> unknown          (output di un modello, non è una fonte dichiarata)
--   calculated -> operational      (token storico già scritto; origin resta calculated)
--   manual, external, unknown, system_of_record, operational restano.
-- La classe di un fatto calcolato nuovo non viene da questo CASE: la calcola
-- l'applicazione, come la più debole tra le classi degli input.
--
-- Allowlist di questo file, istruzioni esatte:
--   ALTER TABLE app.facts DROP CONSTRAINT IF EXISTS facts_source_class_check
--   ALTER COLUMN confidence DROP NOT NULL
-- Ogni altro DROP e ogni TRUNCATE restano rifiutati. 0003 ha in allowlist
-- solo la stessa DROP CONSTRAINT, per allargare il CHECK prima della conversione.

UPDATE app.facts
SET source_class = CASE source_class
  WHEN 'erp' THEN 'system_of_record'
  WHEN 'crm' THEN 'operational'
  WHEN 'ai' THEN 'unknown'
  WHEN 'calculated' THEN 'operational'
  ELSE source_class
END
WHERE source_class IN ('erp', 'crm', 'ai', 'calculated');

ALTER TABLE app.facts DROP CONSTRAINT IF EXISTS facts_source_class_check;
ALTER TABLE app.facts ADD CONSTRAINT facts_source_class_check CHECK (
  source_class IN ('system_of_record', 'operational', 'external', 'manual', 'unknown')
);

ALTER TABLE app.recommendations
  ADD COLUMN IF NOT EXISTS confidence_method text NOT NULL DEFAULT 'unknown';
ALTER TABLE app.recommendations
  ADD COLUMN IF NOT EXISTS confidence_rule text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'recommendations_confidence_method_check'
      AND conrelid = 'app.recommendations'::regclass
  ) THEN
    ALTER TABLE app.recommendations
      ADD CONSTRAINT recommendations_confidence_method_check
      CHECK (confidence_method IN ('rule', 'human', 'unknown'));
  END IF;
END $$;

ALTER TABLE app.decisions_log
  ADD COLUMN IF NOT EXISTS confidence_method text NOT NULL DEFAULT 'unknown';
ALTER TABLE app.decisions_log
  ADD COLUMN IF NOT EXISTS confidence_rule text;
ALTER TABLE app.decisions_log
  ALTER COLUMN confidence DROP NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'decisions_log_confidence_method_check'
      AND conrelid = 'app.decisions_log'::regclass
  ) THEN
    ALTER TABLE app.decisions_log
      ADD CONSTRAINT decisions_log_confidence_method_check
      CHECK (confidence_method IN ('rule', 'human', 'unknown'));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'decisions_log_confidence_check'
      AND conrelid = 'app.decisions_log'::regclass
  ) THEN
    ALTER TABLE app.decisions_log
      ADD CONSTRAINT decisions_log_confidence_check
      CHECK (confidence IS NOT NULL OR confidence_method = 'unknown');
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS app.decisions_log_sources (
  decision_id uuid NOT NULL REFERENCES app.decisions_log (id),
  batch_id uuid NOT NULL,
  row_index integer NOT NULL,
  PRIMARY KEY (decision_id, batch_id, row_index),
  FOREIGN KEY (batch_id, row_index)
    REFERENCES app.landing_rows (batch_id, row_index)
);

COMMENT ON TABLE app.decisions_log_sources IS
  'Tutte le righe di origine (testata ordine e righe) dietro una decisione. Append-only. Non è un dataset di training: l''agente non legge questa tabella e non impara da queste righe.';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'decisions_log_sources_no_update'
      AND tgrelid = 'app.decisions_log_sources'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER decisions_log_sources_no_update
    BEFORE UPDATE OR DELETE ON app.decisions_log_sources
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'decisions_log_sources_no_truncate'
      AND tgrelid = 'app.decisions_log_sources'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER decisions_log_sources_no_truncate
    BEFORE TRUNCATE ON app.decisions_log_sources
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
END $$;

REVOKE UPDATE, DELETE, TRUNCATE ON app.decisions_log_sources FROM ieol_app;
GRANT INSERT, SELECT ON app.decisions_log_sources TO ieol_app;
