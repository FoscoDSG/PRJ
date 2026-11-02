-- Human mapping confirmations, and an audit action for evidence-key rotation.
-- Does not rewrite decisions_log. Does not store the HMAC key.
--
-- Allowlist, exact lines:
--   ALTER TABLE app.audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check
--   ALTER TABLE app.labelled_cases DROP CONSTRAINT IF EXISTS labelled_cases_origin_check

CREATE TABLE IF NOT EXISTS app.mapping_decisions (
  id uuid PRIMARY KEY,
  mapping_id uuid NOT NULL REFERENCES app.field_mappings (id),
  decision text NOT NULL CHECK (decision IN ('confirm')),
  confidence_method text NOT NULL CHECK (confidence_method = 'human'),
  actor text NOT NULL,
  actor_role text NOT NULL,
  decided_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE app.mapping_decisions IS
  'Conferme umane di mapping, append-only. confidence_method è human. actor è chi conferma, decided_at è quando. Non è un dataset che l''agente legge per proporre.';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'mapping_decisions_no_update'
      AND tgrelid = 'app.mapping_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER mapping_decisions_no_update
    BEFORE UPDATE OR DELETE ON app.mapping_decisions
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'mapping_decisions_no_truncate'
      AND tgrelid = 'app.mapping_decisions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER mapping_decisions_no_truncate
    BEFORE TRUNCATE ON app.mapping_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
END $$;

REVOKE UPDATE, DELETE, TRUNCATE ON app.mapping_decisions FROM ieol_app;
GRANT INSERT, SELECT ON app.mapping_decisions TO ieol_app;

ALTER TABLE app.labelled_cases
  ADD COLUMN IF NOT EXISTS mapping_decision_id uuid REFERENCES app.mapping_decisions (id);

ALTER TABLE app.labelled_cases DROP CONSTRAINT IF EXISTS labelled_cases_origin_check;

ALTER TABLE app.labelled_cases ADD CONSTRAINT labelled_cases_origin_check CHECK (
  (
    label_origin = 'human_decision'
    AND decision_id IS NOT NULL
    AND mapping_decision_id IS NULL
  )
  OR (
    label_origin = 'human_decision'
    AND mapping_decision_id IS NOT NULL
    AND decision_id IS NULL
  )
  OR (
    label_origin IN ('hand_written', 'synthetic')
    AND decision_id IS NULL
    AND mapping_decision_id IS NULL
  )
);

ALTER TABLE app.audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check;

ALTER TABLE app.audit_log ADD CONSTRAINT audit_log_action_check CHECK (action IN (
  'proposal', 'human_decision', 'dry_run', 'external_call', 'outcome',
  'pii_unmask', 'pii_read', 'pii_mask', 'evidence_key_rotate'
));
