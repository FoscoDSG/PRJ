-- Pacchetto di estensione dell'azienda. Le aziende già create restano
-- senza: la colonna nasce vuota e questo file non esegue UPDATE.

ALTER TABLE app.companies
  ADD COLUMN IF NOT EXISTS extension_package text;

COMMENT ON COLUMN app.companies.extension_package IS
  'Pacchetto di ontologia che aggiunge solo campi, per questa azienda. NULL: nessuna estensione. Un''altra azienda non lo riceve in lettura.';
