-- Amministratore IEOL, separato dal ruolo aziendale.
-- Il valore iniziale lo mette solo questa migrazione, per chi ha già il ruolo admin.
-- Un utente inserito dopo, anche con ruolo admin, resta spento.
-- Il seme dei conti non accende il flag.
-- Lo può accendere solo il proprietario del database.
-- ieol_app non può cancellare app.users: cancellare e reinserire non ricrea il permesso.
-- Una seconda esecuzione non toglie un permesso dato dopo, e non lo dà a chi non è admin.

ALTER TABLE app.users
  ADD COLUMN IF NOT EXISTS ieol_admin boolean NOT NULL DEFAULT false;

UPDATE app.users
SET ieol_admin = true
WHERE role = 'admin' AND ieol_admin IS DISTINCT FROM true;

COMMENT ON COLUMN app.users.ieol_admin IS
  'Amministratore IEOL. È separato dal ruolo aziendale. La migrazione lo accende solo per chi aveva già il ruolo admin. Un inserimento successivo resta spento: il flag lo può dare solo il proprietario del database.';

-- 0011 revoca INSERT e UPDATE, ma lascia il DELETE.
-- Senza questo REVOKE il conto dell'app cancella la riga e la reinserisce.
REVOKE DELETE ON app.users FROM ieol_app;
