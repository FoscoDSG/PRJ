-- Non riscrive le righe già presenti: nessun UPDATE di dati.
-- Una lettura per nome di fonte filtra anche l'azienda. Lo snapshot di
-- schema non aveva company_id: la colonna nuova resta NULL sulle righe
-- vecchie. L'indice serve a quelle letture.
--
-- Due aziende possono avere lo stesso codice canonico. Il vincolo nuovo
-- non assegna valori alle righe già scritte: quelle restano con
-- company_id NULL e restano uniche fra loro.
--
-- Allowlist, righe esatte:
--   ALTER TABLE app.entities DROP CONSTRAINT IF EXISTS entities_type_business_key_key
--   ALTER TABLE app.identity_aliases DROP CONSTRAINT IF EXISTS identity_aliases_pkey

ALTER TABLE app.schema_snapshots
  ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES app.companies (id);

COMMENT ON COLUMN app.schema_snapshots.company_id IS
  'Azienda del caricamento che ha fotografato le colonne. NULL sulle righe scritte prima di questa migrazione.';

CREATE INDEX IF NOT EXISTS schema_snapshots_company_source_idx
  ON app.schema_snapshots (company_id, source_name);

CREATE INDEX IF NOT EXISTS field_mappings_company_source_idx
  ON app.field_mappings (company_id, source_name)
  WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS landing_batches_company_source_idx
  ON app.landing_batches (company_id, source_name);

ALTER TABLE app.entities
  ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES app.companies (id);

COMMENT ON COLUMN app.entities.company_id IS
  'Azienda del fatto canonico. NULL sulle righe scritte prima di questa migrazione. Lo stesso codice in un''altra azienda è un''altra entità.';

ALTER TABLE app.facts
  ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES app.companies (id);

COMMENT ON COLUMN app.facts.company_id IS
  'Azienda del fatto. NULL sulle righe scritte prima di questa migrazione.';

ALTER TABLE app.identity_aliases
  ADD COLUMN IF NOT EXISTS company_id uuid REFERENCES app.companies (id);

COMMENT ON COLUMN app.identity_aliases.company_id IS
  'Azienda dell''alias. NULL sulle righe scritte prima di questa migrazione.';

ALTER TABLE app.entities DROP CONSTRAINT IF EXISTS entities_type_business_key_key;

ALTER TABLE app.entities
  ADD CONSTRAINT entities_company_type_key
  UNIQUE NULLS NOT DISTINCT (company_id, type, business_key);

ALTER TABLE app.identity_aliases DROP CONSTRAINT IF EXISTS identity_aliases_pkey;

ALTER TABLE app.identity_aliases
  ADD CONSTRAINT identity_aliases_company_type_alias_key
  UNIQUE NULLS NOT DISTINCT (company_id, entity_type, alias);

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
             e.company_id,
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
     AND a.company_id IS NOT DISTINCT FROM b.company_id
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
  JOIN app.entities ord ON ord.id = f.entity_id
  JOIN app.identity_aliases ia
    ON ia.entity_type = 'Customer'
   AND ia.alias = (f.value #>> '{}')
   AND ia.company_id IS NOT DISTINCT FROM ord.company_id
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

-- La 0005 elenca le colonne di facts che ieol_app può leggere. Una colonna
-- nuova non entra da sola in quell'elenco. Senza questa lettura il join
-- per azienda fallisce. Il chiaro non si aggiunge.
GRANT SELECT (company_id) ON app.facts TO ieol_app;
