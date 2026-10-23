-- Tolleranze degli acquisti, per azienda.
-- Una versione nuova è una riga nuova. La riga precedente resta com'è.
-- Un'azienda senza righe usa i valori predefiniti delle colonne.
-- La 0022 è di un altro ramo: questo numero non deve essere consecutivo.
--
-- Predefiniti: prezzo ±1% e al massimo 5 € a riga, imposta ±0,01 €,
-- totale ±0,05 €, ricevimento parziale ammesso.
-- La quantità fatturata non supera quella ricevuta: non è una soglia da alzare.

CREATE TABLE IF NOT EXISTS app.p2p_tolerances (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL REFERENCES app.companies (id),
  version integer NOT NULL CHECK (version >= 1),
  price_rate numeric NOT NULL DEFAULT 0.01,
  price_abs numeric NOT NULL DEFAULT 5,
  vat_abs numeric NOT NULL DEFAULT 0.01,
  total_abs numeric NOT NULL DEFAULT 0.05,
  partial_receipt boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, version)
);

COMMENT ON TABLE app.p2p_tolerances IS
  'Tolleranze acquisti per azienda, a versioni. La versione corrente è il numero più alto. Nessuna riscrittura della riga precedente.';

COMMENT ON COLUMN app.p2p_tolerances.price_rate IS
  'Scostamento relativo del prezzo unitario. Predefinito 0,01, cioè ±1%.';

COMMENT ON COLUMN app.p2p_tolerances.price_abs IS
  'Tetto in euro dello scostamento di prezzo per riga. Predefinito 5.';

COMMENT ON COLUMN app.p2p_tolerances.vat_abs IS
  'Scostamento in euro dell''imposta, a parità di aliquota. Predefinito 0,01.';

COMMENT ON COLUMN app.p2p_tolerances.total_abs IS
  'Scostamento in euro del totale documento. Predefinito 0,05.';

COMMENT ON COLUMN app.p2p_tolerances.partial_receipt IS
  'Il ricevimento parziale è ammesso. Predefinito vero. La quantità fatturata resta comunque non oltre il ricevuto.';

CREATE OR REPLACE FUNCTION app.key_is_pii(key text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT lower(coalesce(key, '')) IN (
    'company_name', 'full_name', 'customer_name', 'email', 'e_mail', 'mail', 'pec',
    'vat', 'piva', 'partita_iva', 'phone', 'telephone', 'telefono', 'cellulare',
    'tel', 'mobile', 'iban', 'address', 'indirizzo', 'codice_fiscale', 'tax_code',
    'cf', 'via', 'cap', 'citta', 'city', 'comune', 'provincia', 'ragione_sociale',
    'nome', 'recipient', 'referente', 'intestatario', 'cognome',
    'causale', 'customer_vat', 'counterparty_name', 'counterparty_vat',
    'supplier_vat', 'supplier_name'
  );
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'p2p_tolerances_no_rewrite'
      AND tgrelid = 'app.p2p_tolerances'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER p2p_tolerances_no_rewrite
    BEFORE UPDATE OR DELETE ON app.p2p_tolerances
    FOR EACH ROW EXECUTE FUNCTION app.prevent_append_only();
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'p2p_tolerances_no_truncate'
      AND tgrelid = 'app.p2p_tolerances'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER p2p_tolerances_no_truncate
    BEFORE TRUNCATE ON app.p2p_tolerances
    FOR EACH STATEMENT EXECUTE FUNCTION app.prevent_append_only();
  END IF;
END $$;

GRANT SELECT, INSERT ON app.p2p_tolerances TO ieol_app;
REVOKE UPDATE, DELETE, TRUNCATE ON app.p2p_tolerances FROM ieol_app;
