-- Non riscrive le righe già presenti: nessun UPDATE di dati.
-- La coda dei nomi vicini prende l'azienda delle due schede. Le righe
-- già scritte restano con company_id NULL e restano uniche fra loro.
--
-- Allowlist, riga esatta:
--   ALTER TABLE app.identity_reviews DROP CONSTRAINT IF EXISTS identity_reviews_entity_type_left_key_right_key_key

ALTER TABLE app.identity_reviews DROP CONSTRAINT IF EXISTS identity_reviews_entity_type_left_key_right_key_key;

ALTER TABLE app.identity_reviews
  ADD CONSTRAINT identity_reviews_company_keys_key
  UNIQUE NULLS NOT DISTINCT (company_id, entity_type, left_key, right_key);

COMMENT ON COLUMN app.identity_reviews.company_id IS
  'Azienda della coppia in coda. Si scrive all''inserimento. NULL sulle righe scritte prima di questa migrazione. Chi decide deve essere della stessa azienda.';

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
           a.company_id,
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
      id, entity_type, left_key, right_key, score, reason, status, company_id
    )
    VALUES (
      gen_random_uuid(), rec.entity_type, rec.left_key, rec.right_key, rec.score,
      'Nomi vicini senza identità comune', 'open', rec.company_id
    )
    ON CONFLICT (company_id, entity_type, left_key, right_key) DO NOTHING;
    GET DIAGNOSTICS inserted = ROW_COUNT;
    created := created + inserted;
  END LOOP;
  RETURN created;
END;
$$;
