-- Personal data, labelled test set, readable audit support, mapping threshold.
-- Safe to re-run the object creation. Existing rows are scanned and, only
-- when they still contain cleartext personal data, rewritten.
--
-- ieol_app can read the masked columns. Cleartext columns are granted to
-- ieol_pii only. The application login does not select them.
--
-- Allowlist of this file, exact statements only:
--   ALTER TABLE app.audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check
--   ALTER TABLE app.audit_log DISABLE TRIGGER audit_no_update
--   ALTER TABLE app.audit_log ENABLE TRIGGER audit_no_update
--   ALTER TABLE app.decisions_log DISABLE TRIGGER decisions_log_no_update
--   ALTER TABLE app.decisions_log ENABLE TRIGGER decisions_log_no_update
-- The trigger lines exist only inside shield_existing_pii. Every other
-- DROP, TRUNCATE, DISABLE TRIGGER, ENABLE TRIGGER and ALTER TABLE ... DISABLE
-- stays refused. hmac_evidence takes fact ids only. The key is one row
-- ieol_app cannot read or replace.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ieol_pii') THEN
    CREATE ROLE ieol_pii NOLOGIN;
  END IF;
END $$;

GRANT ieol_pii TO CURRENT_USER;
GRANT USAGE ON SCHEMA app, src TO ieol_pii;

DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO ieol_pii', current_database());
END $$;

ALTER TABLE app.facts ADD COLUMN IF NOT EXISTS value_cleartext jsonb;
ALTER TABLE app.landing_rows ADD COLUMN IF NOT EXISTS data_cleartext jsonb;
ALTER TABLE app.field_mappings ADD COLUMN IF NOT EXISTS samples_cleartext jsonb;
ALTER TABLE app.email_outbox ADD COLUMN IF NOT EXISTS recipient_cleartext text;

ALTER TABLE app.decisions_log ADD COLUMN IF NOT EXISTS action_label jsonb;
ALTER TABLE app.decisions_log ADD COLUMN IF NOT EXISTS context_snapshot jsonb;

-- Existing rows receive sha256 from the column default. No UPDATE: the log
-- is append-only. Rows inserted after this statement omit the column and
-- receive hmac-sha256-v1, which the application also writes explicitly.
ALTER TABLE app.decisions_log
  ADD COLUMN IF NOT EXISTS hash_version text NOT NULL DEFAULT 'sha256';
ALTER TABLE app.decisions_log
  ALTER COLUMN hash_version SET DEFAULT 'hmac-sha256-v1';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'decisions_log_hash_version_check'
      AND conrelid = 'app.decisions_log'::regclass
  ) THEN
    ALTER TABLE app.decisions_log
      ADD CONSTRAINT decisions_log_hash_version_check
      CHECK (hash_version IN ('sha256', 'hmac-sha256-v1'));
  END IF;
END $$;

COMMENT ON COLUMN app.decisions_log.hash_version IS
  'sha256 sulle righe già presenti all''aggiunta della colonna. Le righe nuove usano hmac-sha256-v1. Nessun UPDATE.';

ALTER TABLE app.decisions_log_sources
  ADD COLUMN IF NOT EXISTS confidence_method text NOT NULL DEFAULT 'unknown';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'decisions_log_sources_confidence_method_check'
      AND conrelid = 'app.decisions_log_sources'::regclass
  ) THEN
    ALTER TABLE app.decisions_log_sources
      ADD CONSTRAINT decisions_log_sources_confidence_method_check
      CHECK (confidence_method IN ('rule', 'human', 'unknown'));
  END IF;
END $$;

COMMENT ON COLUMN app.facts.value_cleartext IS
  'Chiaro dei dati personali. ieol_app non può selezionarlo. Lo legge solo ieol_pii.';
COMMENT ON COLUMN app.landing_rows.data_cleartext IS
  'Chiaro dei campi personali della riga di origine. ieol_app legge solo data, già mascherato.';
COMMENT ON COLUMN app.field_mappings.samples_cleartext IS
  'Campioni in chiaro. La colonna samples vista da ieol_app è mascherata.';
COMMENT ON COLUMN app.email_outbox.recipient_cleartext IS
  'Indirizzo in chiaro. ieol_app legge recipient mascherato.';

CREATE OR REPLACE FUNCTION app.key_is_pii(key text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT lower(coalesce(key, '')) IN (
    'company_name', 'full_name', 'customer_name', 'email', 'e_mail', 'mail', 'pec',
    'vat', 'piva', 'partita_iva', 'phone', 'telephone', 'telefono', 'cellulare',
    'tel', 'mobile', 'iban', 'address', 'indirizzo', 'codice_fiscale', 'tax_code',
    'cf', 'via', 'cap', 'citta', 'city', 'comune', 'provincia', 'ragione_sociale',
    'nome', 'recipient', 'referente', 'intestatario', 'cognome'
  );
$$;

CREATE OR REPLACE FUNCTION app.field_is_pii(entity_id uuid, property text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT CASE
    WHEN app.key_is_pii(property) THEN true
    WHEN property = 'name' AND EXISTS (
      SELECT 1 FROM app.entities e
      WHERE e.id = entity_id
        AND e.type IN ('Person', 'Customer', 'Supplier', 'Organization')
    ) THEN true
    ELSE false
  END;
$$;

CREATE OR REPLACE FUNCTION app.text_has_pii(raw text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT raw IS NOT NULL AND (
    raw ~ '[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}'
    OR raw ~* '\mIT[0-9]{2}[A-Z0-9]{23}\M'
    OR raw ~* '\m[A-Z]{6}[0-9]{2}[A-Z][0-9]{2}[A-Z][0-9]{3}[A-Z]\M'
    OR raw ~* '\mIT[0-9]{11}\M'
    OR raw ~ '\+[0-9][0-9 ./\-]{7,}[0-9]'
  );
$$;

CREATE OR REPLACE FUNCTION app.redact_text(raw text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN raw IS NULL THEN NULL
    ELSE regexp_replace(
      regexp_replace(
        regexp_replace(
          regexp_replace(
            regexp_replace(raw, '[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', '••••', 'g'),
            '\mIT[0-9]{2}[A-Z0-9]{23}\M', '••••', 'gi'),
          '\m[A-Z]{6}[0-9]{2}[A-Z][0-9]{2}[A-Z][0-9]{3}[A-Z]\M', '••••', 'gi'),
        '\mIT[0-9]{11}\M', '••••', 'gi'),
      '\+[0-9][0-9 ./\-]{7,}[0-9]', '••••', 'g')
  END;
$$;

CREATE OR REPLACE FUNCTION app.redact_json(value jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  key text;
  item jsonb;
  result jsonb;
  prop text;
  entity_type text;
BEGIN
  IF value IS NULL THEN
    RETURN NULL;
  END IF;
  IF jsonb_typeof(value) = 'array' THEN
    result := '[]'::jsonb;
    FOR item IN SELECT * FROM jsonb_array_elements(value) LOOP
      result := result || jsonb_build_array(app.redact_json(item));
    END LOOP;
    RETURN result;
  END IF;
  IF jsonb_typeof(value) = 'object' THEN
    prop := value->>'property';
    entity_type := value->>'entity_type';
    result := '{}'::jsonb;
    FOR key, item IN SELECT * FROM jsonb_each(value) LOOP
      IF key = 'value' AND (
        app.key_is_pii(prop)
        OR (prop = 'name' AND entity_type IN ('Supplier', 'Person', 'Customer', 'Organization'))
      ) THEN
        result := result || jsonb_build_object(key, to_jsonb('••••'::text));
      ELSIF app.key_is_pii(key) THEN
        result := result || jsonb_build_object(key, to_jsonb('••••'::text));
      ELSIF jsonb_typeof(item) IN ('object', 'array') THEN
        result := result || jsonb_build_object(key, app.redact_json(item));
      ELSIF jsonb_typeof(item) = 'string' AND app.text_has_pii(item #>> '{}') THEN
        result := result || jsonb_build_object(key, to_jsonb('••••'::text));
      ELSE
        result := result || jsonb_build_object(key, item);
      END IF;
    END LOOP;
    RETURN result;
  END IF;
  IF jsonb_typeof(value) = 'string' AND app.text_has_pii(value #>> '{}') THEN
    RETURN to_jsonb('••••'::text);
  END IF;
  RETURN value;
END;
$$;

CREATE OR REPLACE FUNCTION app.shield_fact() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, public AS $$
BEGIN
  IF TG_OP = 'UPDATE'
     AND NEW.value IS NOT DISTINCT FROM OLD.value
     AND NEW.property IS NOT DISTINCT FROM OLD.property
     AND (
       OLD.value_cleartext IS NOT NULL
       OR NOT app.field_is_pii(NEW.entity_id, NEW.property)
       OR OLD.value = to_jsonb('••••'::text)
     )
  THEN
    NEW.value_cleartext := OLD.value_cleartext;
    RETURN NEW;
  END IF;
  IF app.field_is_pii(NEW.entity_id, NEW.property) THEN
    IF NEW.value IS DISTINCT FROM to_jsonb('••••'::text) THEN
      NEW.value_cleartext := NEW.value;
      NEW.value := to_jsonb('••••'::text);
    END IF;
  ELSE
    NEW.value_cleartext := NULL;
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'facts_shield_pii' AND tgrelid = 'app.facts'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER facts_shield_pii
    BEFORE INSERT OR UPDATE ON app.facts
    FOR EACH ROW EXECUTE FUNCTION app.shield_fact();
  END IF;
END $$;

CREATE OR REPLACE FUNCTION app.landing_has_pii(data jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT EXISTS (
    SELECT 1
    FROM jsonb_each(coalesce(data, '{}'::jsonb)) AS item(key, val)
    WHERE app.key_is_pii(item.key)
       OR (jsonb_typeof(item.val) = 'string' AND app.text_has_pii(item.val #>> '{}'))
  );
$$;

CREATE OR REPLACE FUNCTION app.shield_landing() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, public AS $$
DECLARE
  masked jsonb := '{}'::jsonb;
  clear jsonb := '{}'::jsonb;
  key text;
  val jsonb;
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.data IS NOT DISTINCT FROM OLD.data THEN
    IF OLD.data_cleartext IS NOT NULL OR NOT app.landing_has_pii(OLD.data) THEN
      NEW.data_cleartext := OLD.data_cleartext;
      RETURN NEW;
    END IF;
  END IF;
  FOR key, val IN SELECT * FROM jsonb_each(coalesce(NEW.data, '{}'::jsonb)) LOOP
    IF app.key_is_pii(key) OR (jsonb_typeof(val) = 'string' AND app.text_has_pii(val #>> '{}')) THEN
      clear := clear || jsonb_build_object(key, val);
      masked := masked || jsonb_build_object(key, to_jsonb('••••'::text));
    ELSE
      masked := masked || jsonb_build_object(key, val);
    END IF;
  END LOOP;
  NEW.data := masked;
  NEW.data_cleartext := CASE WHEN clear = '{}'::jsonb THEN NULL ELSE clear END;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'landing_shield_pii' AND tgrelid = 'app.landing_rows'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER landing_shield_pii
    BEFORE INSERT OR UPDATE ON app.landing_rows
    FOR EACH ROW EXECUTE FUNCTION app.shield_landing();
  END IF;
END $$;

CREATE OR REPLACE FUNCTION app.shield_samples() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, public AS $$
DECLARE
  item jsonb;
  masked jsonb := '[]'::jsonb;
  personal boolean;
BEGIN
  personal := app.key_is_pii(NEW.source_field) OR app.key_is_pii(NEW.target_property);
  IF NOT personal AND jsonb_typeof(NEW.samples) = 'array' THEN
    SELECT EXISTS (
      SELECT 1 FROM jsonb_array_elements(NEW.samples) AS element(val)
      WHERE jsonb_typeof(element.val) = 'string' AND app.text_has_pii(element.val #>> '{}')
    ) INTO personal;
  END IF;
  IF NOT personal THEN
    IF TG_OP = 'UPDATE' THEN
      NEW.samples_cleartext := OLD.samples_cleartext;
    END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'UPDATE'
     AND OLD.samples_cleartext IS NOT NULL
     AND NEW.samples IS NOT DISTINCT FROM OLD.samples
  THEN
    NEW.samples_cleartext := OLD.samples_cleartext;
    RETURN NEW;
  END IF;
  NEW.samples_cleartext := NEW.samples;
  IF jsonb_typeof(NEW.samples) = 'array' THEN
    FOR item IN SELECT * FROM jsonb_array_elements(NEW.samples) LOOP
      masked := masked || jsonb_build_array(to_jsonb('••••'::text));
    END LOOP;
    NEW.samples := masked;
  ELSE
    NEW.samples := '[]'::jsonb;
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'mappings_shield_pii' AND tgrelid = 'app.field_mappings'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER mappings_shield_pii
    BEFORE INSERT OR UPDATE ON app.field_mappings
    FOR EACH ROW EXECUTE FUNCTION app.shield_samples();
  END IF;
END $$;

CREATE OR REPLACE FUNCTION app.shield_email() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, public AS $$
BEGIN
  IF NEW.recipient IS NOT NULL AND NEW.recipient <> '••••' AND NEW.recipient_cleartext IS NULL THEN
    NEW.recipient_cleartext := NEW.recipient;
    NEW.recipient := '••••';
  END IF;
  NEW.subject := coalesce(app.redact_text(NEW.subject), '');
  NEW.body := coalesce(app.redact_text(NEW.body), '');
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'email_shield_pii' AND tgrelid = 'app.email_outbox'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER email_shield_pii
    BEFORE INSERT OR UPDATE ON app.email_outbox
    FOR EACH ROW EXECUTE FUNCTION app.shield_email();
  END IF;
END $$;

CREATE OR REPLACE FUNCTION app.shield_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, public AS $$
BEGIN
  NEW.payload := coalesce(app.redact_json(NEW.payload), '{}'::jsonb);
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'audit_shield_pii' AND tgrelid = 'app.audit_log'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER audit_shield_pii
    BEFORE INSERT OR UPDATE ON app.audit_log
    FOR EACH ROW EXECUTE FUNCTION app.shield_audit();
  END IF;
END $$;

CREATE OR REPLACE FUNCTION app.shield_decision() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, public AS $$
BEGIN
  NEW.recommendation := coalesce(app.redact_text(NEW.recommendation), NEW.recommendation);
  NEW.action_label := app.redact_json(NEW.action_label);
  NEW.context_snapshot := app.redact_json(NEW.context_snapshot);
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'decisions_shield_pii' AND tgrelid = 'app.decisions_log'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER decisions_shield_pii
    BEFORE INSERT OR UPDATE ON app.decisions_log
    FOR EACH ROW EXECUTE FUNCTION app.shield_decision();
  END IF;
END $$;

CREATE OR REPLACE FUNCTION app.shield_recommendation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, public AS $$
BEGIN
  NEW.problem := coalesce(app.redact_text(NEW.problem), NEW.problem);
  NEW.evidence := app.redact_json(NEW.evidence);
  NEW.actions := app.redact_json(NEW.actions);
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'recommendations_shield_pii'
      AND tgrelid = 'app.recommendations'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER recommendations_shield_pii
    BEFORE INSERT OR UPDATE ON app.recommendations
    FOR EACH ROW EXECUTE FUNCTION app.shield_recommendation();
  END IF;
END $$;

-- The key is one row. ieol_app cannot read it or replace it. The function
-- takes only fact ids, so the caller cannot supply a key and brute-force offline.
CREATE TABLE IF NOT EXISTS app.evidence_hmac_key (
  id integer PRIMARY KEY CHECK (id = 1),
  secret text NOT NULL
);

REVOKE ALL ON app.evidence_hmac_key FROM PUBLIC;
REVOKE ALL ON app.evidence_hmac_key FROM ieol_app;

CREATE OR REPLACE FUNCTION app.hmac_evidence(fact_ids uuid[]) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, public AS $$
DECLARE
  payload text;
  secret text;
BEGIN
  SELECT evidence_hmac_key.secret INTO secret FROM app.evidence_hmac_key WHERE id = 1;
  IF secret IS NULL OR length(btrim(secret)) = 0 THEN
    RAISE EXCEPTION 'chiave hmac assente';
  END IF;
  SELECT coalesce(string_agg(
    f.id::text || '|' || f.property || '|' || coalesce(f.origin, '') || '|' ||
    coalesce(f.classification, '') || '|' || coalesce(f.value_cleartext, f.value)::text,
    E'\n' ORDER BY f.id::text, f.property
  ), '')
  INTO payload
  FROM app.facts f
  WHERE f.id = ANY(fact_ids);
  RETURN encode(hmac(convert_to(payload, 'UTF8'), convert_to(secret, 'UTF8'), 'sha256'), 'hex');
END;
$$;

REVOKE ALL ON FUNCTION app.hmac_evidence(uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.hmac_evidence(uuid[]) TO ieol_app;

CREATE OR REPLACE FUNCTION app.vat_is_valid(entity_id uuid) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path = app, public AS $$
  SELECT EXISTS (
    SELECT 1 FROM app.facts
    WHERE facts.entity_id = vat_is_valid.entity_id
      AND property = 'vat'
      AND valid_to IS NULL
      AND coalesce(value_cleartext, value) #>> '{}' ~ '^IT[0-9]{11}$'
  );
$$;

REVOKE ALL ON FUNCTION app.vat_is_valid(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.vat_is_valid(uuid) TO ieol_app;

CREATE OR REPLACE FUNCTION app.normalize_name(raw text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  text_value text;
BEGIN
  IF raw IS NULL THEN
    RETURN '';
  END IF;
  text_value := lower(btrim(raw));
  text_value := regexp_replace(text_value, '\m(srl|srls|spa|sas|snc|llc|ltd)\M', ' ', 'g');
  text_value := regexp_replace(text_value, '[^a-z0-9 ]', ' ', 'g');
  text_value := regexp_replace(text_value, '\s+', ' ', 'g');
  RETURN btrim(text_value);
END;
$$;

CREATE OR REPLACE FUNCTION app.name_score(left_name text, right_name text) RETURNS numeric
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  left_tokens text[];
  right_tokens text[];
BEGIN
  IF left_name IS NULL OR right_name IS NULL OR left_name = '' OR right_name = '' THEN
    RETURN 0;
  END IF;
  IF left_name = right_name THEN
    RETURN 0.95;
  END IF;
  left_tokens := regexp_split_to_array(left_name, '\s+');
  right_tokens := regexp_split_to_array(right_name, '\s+');
  IF left_tokens[array_length(left_tokens, 1)] = right_tokens[array_length(right_tokens, 1)]
     AND left(left_tokens[1], 1) = left(right_tokens[1], 1) THEN
    RETURN 0.72;
  END IF;
  RETURN 0;
END;
$$;

CREATE OR REPLACE FUNCTION app.queue_name_reviews() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, public AS $$
DECLARE
  created integer := 0;
  inserted integer;
  rec record;
BEGIN
  FOR rec IN
    WITH named AS (
      SELECT e.type AS entity_type,
             e.business_key,
             app.normalize_name(
               CASE
                 WHEN e.type = 'Product' THEN f.value #>> '{}'
                 ELSE coalesce(f.value_cleartext, f.value) #>> '{}'
               END
             ) AS norm
      FROM app.entities e
      JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL
      WHERE e.valid_to IS NULL
        AND (
          (e.type = 'Customer' AND f.property = 'company_name')
          OR (e.type = 'Product' AND f.property = 'name')
          OR (e.type = 'Supplier' AND f.property = 'name')
        )
    )
    SELECT a.entity_type,
           a.business_key AS left_key,
           b.business_key AS right_key,
           app.name_score(a.norm, b.norm) AS score
    FROM named a
    JOIN named b
      ON a.entity_type = b.entity_type
     AND a.business_key < b.business_key
    WHERE app.name_score(a.norm, b.norm) >= 0.7
  LOOP
    INSERT INTO app.identity_reviews (
      id, entity_type, left_key, right_key, score, reason, status
    )
    VALUES (
      gen_random_uuid(), rec.entity_type, rec.left_key, rec.right_key, rec.score,
      'Nomi vicini senza identità comune', 'open'
    )
    ON CONFLICT (entity_type, left_key, right_key) DO NOTHING;
    GET DIAGNOSTICS inserted = ROW_COUNT;
    created := created + inserted;
  END LOOP;
  RETURN created;
END;
$$;

REVOKE ALL ON FUNCTION app.queue_name_reviews() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.queue_name_reviews() TO ieol_app;

CREATE OR REPLACE FUNCTION app.queue_customer_email(
  p_order uuid, p_key text, p_subject text, p_body text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, public AS $$
DECLARE
  address text;
  customer_id uuid;
BEGIN
  SELECT ia.entity_id INTO customer_id
  FROM app.facts f
  JOIN app.identity_aliases ia
    ON ia.entity_type = 'Customer'
   AND ia.alias = (f.value #>> '{}')
  WHERE f.entity_id = p_order
    AND f.property = 'customer_source_key'
    AND f.valid_to IS NULL
  LIMIT 1;
  IF customer_id IS NOT NULL THEN
    SELECT coalesce(value_cleartext, value) #>> '{}' INTO address
    FROM app.facts
    WHERE facts.entity_id = customer_id AND property = 'email' AND valid_to IS NULL
    LIMIT 1;
  END IF;
  INSERT INTO app.email_outbox (
    id, idempotency_key, recipient, recipient_cleartext, subject, body
  )
  VALUES (
    gen_random_uuid(), p_key, '••••',
    CASE WHEN address IS NULL OR address = '••••' THEN NULL ELSE address END,
    coalesce(app.redact_text(p_subject), ''),
    coalesce(app.redact_text(p_body), '')
  )
  ON CONFLICT (idempotency_key) DO NOTHING;
END;
$$;

REVOKE ALL ON FUNCTION app.queue_customer_email(uuid, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.queue_customer_email(uuid, text, text, text) TO ieol_app;

-- Scan first. Rewrite a row only when cleartext personal data is still there.
-- When at least one row changes, write one audit row with the counts and the
-- migration version. The payload has numbers only.
CREATE OR REPLACE FUNCTION app.shield_existing_pii() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, public AS $$
DECLARE
  n_facts integer := 0;
  n_landing integer := 0;
  n_samples integer := 0;
  n_email integer := 0;
  n_recommendations integer := 0;
  n_batches integer := 0;
  n_quality integer := 0;
  n_identity integer := 0;
  n_audit integer := 0;
  n_decisions integer := 0;
  total integer;
BEGIN
  SELECT count(*) INTO n_facts
  FROM app.facts
  WHERE app.field_is_pii(entity_id, property)
    AND value_cleartext IS NULL
    AND value IS DISTINCT FROM to_jsonb('••••'::text);
  IF n_facts > 0 THEN
    UPDATE app.facts
    SET value = value
    WHERE app.field_is_pii(entity_id, property)
      AND value_cleartext IS NULL
      AND value IS DISTINCT FROM to_jsonb('••••'::text);
  END IF;

  SELECT count(*) INTO n_landing
  FROM app.landing_rows
  WHERE data_cleartext IS NULL AND app.landing_has_pii(data);
  IF n_landing > 0 THEN
    UPDATE app.landing_rows
    SET data = data
    WHERE data_cleartext IS NULL AND app.landing_has_pii(data);
  END IF;

  SELECT count(*) INTO n_samples
  FROM app.field_mappings
  WHERE samples_cleartext IS NULL
    AND (app.key_is_pii(source_field) OR app.key_is_pii(target_property));
  IF n_samples > 0 THEN
    UPDATE app.field_mappings
    SET samples = samples
    WHERE samples_cleartext IS NULL
      AND (app.key_is_pii(source_field) OR app.key_is_pii(target_property));
  END IF;

  UPDATE app.email_outbox
  SET recipient = recipient
  WHERE recipient IS NOT NULL AND recipient <> '••••' AND recipient_cleartext IS NULL;
  GET DIAGNOSTICS n_email = ROW_COUNT;

  UPDATE app.recommendations
  SET problem = problem
  WHERE problem IS DISTINCT FROM app.redact_text(problem)
     OR evidence IS DISTINCT FROM app.redact_json(evidence)
     OR actions IS DISTINCT FROM app.redact_json(actions);
  GET DIAGNOSTICS n_recommendations = ROW_COUNT;

  UPDATE app.action_batches
  SET payloads = app.redact_json(payloads)
  WHERE payloads IS DISTINCT FROM app.redact_json(payloads);
  GET DIAGNOSTICS n_batches = ROW_COUNT;

  UPDATE app.quality_issues
  SET detail = app.redact_text(detail)
  WHERE detail IS DISTINCT FROM app.redact_text(detail);
  GET DIAGNOSTICS n_quality = ROW_COUNT;

  UPDATE app.identity_reviews
  SET reason = 'Nomi vicini senza identità comune'
  WHERE reason ILIKE 'Nomi vicini%' AND reason <> 'Nomi vicini senza identità comune';
  GET DIAGNOSTICS n_identity = ROW_COUNT;

  SELECT count(*) INTO n_audit
  FROM app.audit_log
  WHERE payload IS DISTINCT FROM app.redact_json(payload);
  IF n_audit > 0 THEN
    ALTER TABLE app.audit_log DISABLE TRIGGER audit_no_update;
    BEGIN
      UPDATE app.audit_log
      SET payload = app.redact_json(payload)
      WHERE payload IS DISTINCT FROM app.redact_json(payload);
      ALTER TABLE app.audit_log ENABLE TRIGGER audit_no_update;
    EXCEPTION WHEN OTHERS THEN
      ALTER TABLE app.audit_log ENABLE TRIGGER audit_no_update;
      RAISE;
    END;
  END IF;

  SELECT count(*) INTO n_decisions
  FROM app.decisions_log
  WHERE recommendation IS DISTINCT FROM app.redact_text(recommendation)
     OR action_label IS DISTINCT FROM app.redact_json(action_label)
     OR context_snapshot IS DISTINCT FROM app.redact_json(context_snapshot);
  IF n_decisions > 0 THEN
    ALTER TABLE app.decisions_log DISABLE TRIGGER decisions_log_no_update;
    BEGIN
      UPDATE app.decisions_log
      SET recommendation = app.redact_text(recommendation)
      WHERE recommendation IS DISTINCT FROM app.redact_text(recommendation)
         OR action_label IS DISTINCT FROM app.redact_json(action_label)
         OR context_snapshot IS DISTINCT FROM app.redact_json(context_snapshot);
      ALTER TABLE app.decisions_log ENABLE TRIGGER decisions_log_no_update;
    EXCEPTION WHEN OTHERS THEN
      ALTER TABLE app.decisions_log ENABLE TRIGGER decisions_log_no_update;
      RAISE;
    END;
  END IF;

  total := n_facts + n_landing + n_samples + n_email + n_recommendations
    + n_batches + n_quality + n_identity + n_audit + n_decisions;
  IF total > 0 THEN
    INSERT INTO app.audit_log (id, actor, role, action, entity_ref, payload)
    VALUES (
      gen_random_uuid(),
      'sistema',
      'sistema',
      'pii_mask',
      '0005',
      jsonb_build_object(
        'migration', '0005',
        'hash_version', 'hmac-sha256-v1',
        'facts', n_facts,
        'landing_rows', n_landing,
        'field_mappings', n_samples,
        'email_outbox', n_email,
        'recommendations', n_recommendations,
        'action_batches', n_batches,
        'quality_issues', n_quality,
        'identity_reviews', n_identity,
        'audit_log', n_audit,
        'decisions_log', n_decisions
      )
    );
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION app.shield_existing_pii() FROM PUBLIC;

ALTER TABLE app.audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check;
ALTER TABLE app.audit_log ADD CONSTRAINT audit_log_action_check CHECK (action IN (
  'proposal', 'human_decision', 'dry_run', 'external_call', 'outcome',
  'pii_unmask', 'pii_read', 'pii_mask'
));

SELECT app.shield_existing_pii();

CREATE TABLE IF NOT EXISTS app.testset_versions (
  version integer PRIMARY KEY,
  built_at timestamptz NOT NULL DEFAULT now(),
  case_count integer NOT NULL,
  recommendation_count integer NOT NULL,
  mapping_count integer NOT NULL
);

CREATE TABLE IF NOT EXISTS app.labelled_cases (
  id uuid PRIMARY KEY,
  testset_version integer NOT NULL REFERENCES app.testset_versions (version),
  case_kind text NOT NULL CHECK (case_kind IN ('recommendation', 'mapping')),
  label_origin text NOT NULL CHECK (label_origin IN ('human_decision', 'hand_written', 'synthetic')),
  decision_id uuid REFERENCES app.decisions_log (id),
  label jsonb NOT NULL,
  built_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT labelled_cases_origin_check CHECK (
    (label_origin = 'human_decision' AND decision_id IS NOT NULL)
    OR (label_origin IN ('hand_written', 'synthetic') AND decision_id IS NULL)
  )
);

CREATE INDEX IF NOT EXISTS labelled_cases_version_idx
  ON app.labelled_cases (testset_version);

COMMENT ON TABLE app.labelled_cases IS
  'Casi etichettati, append-only. Origine: decisione umana, fixture manoscritta, oppure synthetic. I synthetic non contano per il minimo di 30 né per la soglia. Non è il registro decisioni e l''agente non lo legge per proporre.';

CREATE TABLE IF NOT EXISTS app.testset_evals (
  id uuid PRIMARY KEY,
  testset_version integer NOT NULL REFERENCES app.testset_versions (version),
  subject text NOT NULL CHECK (subject IN ('agent', 'rules', 'mapping')),
  agent_version text NOT NULL,
  rules_version text NOT NULL,
  method text NOT NULL,
  separated boolean NOT NULL,
  n integer NOT NULL,
  errors integer,
  accuracy numeric,
  precision numeric,
  recall numeric,
  accuracy_low numeric,
  accuracy_high numeric,
  precision_low numeric,
  precision_high numeric,
  recall_low numeric,
  recall_high numeric,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  evaluated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.mapping_thresholds (
  id uuid PRIMARY KEY,
  threshold numeric NOT NULL,
  method text NOT NULL,
  measured boolean NOT NULL,
  rules_version text NOT NULL,
  metrics jsonb NOT NULL,
  testset_version integer,
  support integer NOT NULL,
  chosen_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE app.mapping_thresholds IS
  'Soglia di mapping. measured = false è il fallback 0,75, non una probabilità calibrata. Minimo documentato: 30 casi di mapping.';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'testset_versions_no_update'
      AND tgrelid = 'app.testset_versions'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER testset_versions_no_update
    BEFORE UPDATE OR DELETE ON app.testset_versions
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'testset_versions_no_truncate'
      AND tgrelid = 'app.testset_versions'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER testset_versions_no_truncate
    BEFORE TRUNCATE ON app.testset_versions
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'labelled_cases_no_update'
      AND tgrelid = 'app.labelled_cases'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER labelled_cases_no_update
    BEFORE UPDATE OR DELETE ON app.labelled_cases
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'labelled_cases_no_truncate'
      AND tgrelid = 'app.labelled_cases'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER labelled_cases_no_truncate
    BEFORE TRUNCATE ON app.labelled_cases
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'testset_evals_no_update'
      AND tgrelid = 'app.testset_evals'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER testset_evals_no_update
    BEFORE UPDATE OR DELETE ON app.testset_evals
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'testset_evals_no_truncate'
      AND tgrelid = 'app.testset_evals'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER testset_evals_no_truncate
    BEFORE TRUNCATE ON app.testset_evals
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'mapping_thresholds_no_update'
      AND tgrelid = 'app.mapping_thresholds'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER mapping_thresholds_no_update
    BEFORE UPDATE OR DELETE ON app.mapping_thresholds
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'mapping_thresholds_no_truncate'
      AND tgrelid = 'app.mapping_thresholds'::regclass AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER mapping_thresholds_no_truncate
    BEFORE TRUNCATE ON app.mapping_thresholds
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
END $$;

REVOKE UPDATE, DELETE, TRUNCATE ON app.testset_versions FROM ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.labelled_cases FROM ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.testset_evals FROM ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.mapping_thresholds FROM ieol_app;
GRANT SELECT, INSERT ON app.testset_versions TO ieol_app;
GRANT SELECT, INSERT ON app.labelled_cases TO ieol_app;
GRANT SELECT, INSERT ON app.testset_evals TO ieol_app;
GRANT SELECT, INSERT ON app.mapping_thresholds TO ieol_app;

-- Table-level SELECT includes every column, including ones added above.
-- Replace it with an explicit column list that omits the cleartext.
REVOKE SELECT ON app.facts FROM ieol_app;
GRANT SELECT (
  id, entity_id, property, value, classification, origin, source, confidence,
  valid_from, valid_to, source_class, confidence_method, confidence_rule
) ON app.facts TO ieol_app;
GRANT SELECT (id, entity_id, property, value_cleartext, valid_to) ON app.facts TO ieol_pii;

REVOKE SELECT ON app.landing_rows FROM ieol_app;
GRANT SELECT (id, batch_id, row_index, data) ON app.landing_rows TO ieol_app;
GRANT SELECT (batch_id, row_index, data_cleartext) ON app.landing_rows TO ieol_pii;

REVOKE SELECT ON app.field_mappings FROM ieol_app;
GRANT SELECT (
  id, source_name, source_field, target_type, target_property, confidence,
  status, samples, profile, valid_from, valid_to
) ON app.field_mappings TO ieol_app;
GRANT SELECT (id, source_name, source_field, samples_cleartext) ON app.field_mappings TO ieol_pii;

REVOKE SELECT ON app.email_outbox FROM ieol_app;
GRANT SELECT (id, idempotency_key, recipient, subject, body, created_at) ON app.email_outbox TO ieol_app;
GRANT SELECT (id, idempotency_key, recipient_cleartext) ON app.email_outbox TO ieol_pii;

REVOKE SELECT ON src.clienti FROM ieol_app;
GRANT SELECT (anag_cli) ON src.clienti TO ieol_app;
GRANT SELECT ON src.clienti TO ieol_pii;

REVOKE SELECT ON src.fornitori FROM ieol_app;
GRANT SELECT (forn, cod_art, ritardo_giorni) ON src.fornitori TO ieol_app;
GRANT SELECT ON src.fornitori TO ieol_pii;
