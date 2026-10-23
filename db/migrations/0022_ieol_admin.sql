-- Amministratore IEOL, separato dal ruolo aziendale.
-- Chi ha già il ruolo admin resta amministratore IEOL.
-- Gli altri ruoli non guadagnano questo permesso.
-- Il flag si può rileggere dal ruolo: solo admin era vero oggi.
-- Una seconda esecuzione non cambia chi ha già il valore giusto.

ALTER TABLE app.users
  ADD COLUMN IF NOT EXISTS ieol_admin boolean NOT NULL DEFAULT false;

UPDATE app.users
SET ieol_admin = true
WHERE role = 'admin' AND ieol_admin IS DISTINCT FROM true;

COMMENT ON COLUMN app.users.ieol_admin IS
  'Amministratore IEOL. È separato dal ruolo aziendale. Alla nascita copia chi aveva già il ruolo admin, così nessuno perde o guadagna permessi.';

-- Il conto di partenza si chiama admin. ieol_app può inserire solo
-- username, password_hash e role: non può scrivere questo flag.
-- Il trigger lo accende solo per quel conto, all'inserimento.
-- Un admin aziendale con un altro nome resta spento.

CREATE OR REPLACE FUNCTION app.users_builtin_ieol_admin()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
  IF NEW.username = 'admin' AND NEW.role = 'admin' THEN
    NEW.ieol_admin := true;
  END IF;
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION app.users_builtin_ieol_admin() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.users_builtin_ieol_admin() TO ieol_app;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'users_builtin_ieol_admin'
      AND tgrelid = 'app.users'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER users_builtin_ieol_admin
    BEFORE INSERT ON app.users
    FOR EACH ROW EXECUTE FUNCTION app.users_builtin_ieol_admin();
  END IF;
END $$;
