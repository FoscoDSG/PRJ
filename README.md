# Configurazione

La visione di prodotto è in `README (1).md`. Qui ci sono solo i segreti necessari per avviare la V1.

## Sessione

`IEOL_SESSION_SECRET` firma il cookie di sessione. È accettato anche `SESSION_SECRET`. Se mancano entrambi, l'API rifiuta l'avvio.

Il cookie è `https_only`, salvo `IEOL_INSECURE_COOKIES=1` per un HTTP locale.

In sviluppo locale si può impostare `IEOL_DEV=1`: in quel caso, e solo in quel caso, viene usata una chiave incorporata, inadatta a qualsiasi ambiente condiviso. Non impostare `IEOL_DEV` in produzione.

## Utenti

Le password non sono nel codice, nelle fixture o nel form di login. All'avvio, per ogni ruolo con la variabile impostata, viene creato l'utente se non esiste già:

- `IEOL_SEED_ADMIN_PASSWORD`
- `IEOL_SEED_MAPPER_PASSWORD`
- `IEOL_SEED_OPERATOR_PASSWORD`
- `IEOL_SEED_APPROVER_PASSWORD`

La password non può coincidere con il nome del ruolo (`admin`, `mapper`, `operator`, `approver`). Un valore vuoto non crea il ruolo se manca. Se l'utente esiste già e la password non è il nome del ruolo, l'hash non viene toccato.

Se l'utente esiste e la password è ancora il nome del ruolo (hash SHA-256 storico oppure già argon2), all'avvio viene sostituita: con la password della variabile d'ambiente, se presente; altrimenti con un segreto casuale che non viene stampato, così la vecchia password smette di funzionare.

In alternativa, solo con `IEOL_DEV=1`:

```text
IEOL_DEV=1 \
IEOL_SEED_ADMIN_PASSWORD='...' \
IEOL_SEED_MAPPER_PASSWORD='...' \
IEOL_SEED_OPERATOR_PASSWORD='...' \
IEOL_SEED_APPROVER_PASSWORD='...' \
DATABASE_URL='postgresql://ubuntu@/ieol' \
python scripts/seed_dev_users.py
```

Lo script non stampa password né hash.

## Hash delle password

Le password nuove sono argon2id (parametri di default di argon2-cffi: memoria 64 MiB, 3 iterazioni, parallelismo 4).

Gli hash SHA-256 storici (`sha256("ieol:" + password)` con sale fisso) e gli hash `scrypt$` restano validi al login e, al primo accesso riuscito, vengono sostituiti con argon2id. Non serve un reset di massa, salvo il caso sopra: se la password è il nome del ruolo viene ritirata all'avvio, anche senza un login.

## Database

`DATABASE_URL` ha default `postgresql://ubuntu@/ieol`. I test usano `TEST_DATABASE_URL` se presente, altrimenti `postgresql://ubuntu@/ieol_test`, e richiedono un ruolo PostgreSQL `ubuntu` con accesso peer a quel database.

Lo schema si aggiorna con `ieol migrate`. Vedi sotto.

Vedi anche `.env.example`.

## Migrazioni

I file in `db/migrations/` sono numerati e si applicano in ordine. Il runner registra le versioni in `public.schema_migrations`. È un esecutore SQL piccolo, senza Alembic: lo schema è già SQL e non serve un secondo modello da generare.

```text
ieol migrate
```

Il comando applica solo le migrazioni non ancora registrate e fa commit. Non esegue `DROP` né `TRUNCATE`. Eseguirlo di nuovo lascia i dati invariati.

Un database già creato con `db/schema.sql` viene riconosciuto. Se ci sono tutte le tabelle, le colonne, la funzione e il trigger dell'audit, la migrazione `0001_initial` è segnata come baseline (`baselined = true`) e il suo SQL non viene eseguito. Righe, identità delle tabelle e registro audit restano quelli già presenti. Se lo schema c'è solo in parte, il comando si ferma senza scrivere.

`db/schema.sql` resta il riferimento storico. Una modifica successiva è un file nuovo in `db/migrations/`. Una migrazione già applicata non si riscrive.

All'avvio l'API esegue le stesse migrazioni, sempre senza cancellare dati.

## Demo

`ieol demo` usa un database dedicato, `ieol_demo`, e lo crea se manca. Le query sono qualificate con `app.` e `src.`, quindi un altro schema nello stesso database non le isolerebbe. Il database di `DATABASE_URL` non viene letto né modificato.

```text
ieol demo
ieol demo --reset
ieol expire
ieol execute
```

`ieol expire` e `ieol execute` continuano la stessa demo, sullo stesso database dedicato.

`--reset` è la conferma esplicita: cancella gli schemi `app` e `src` solo dentro il database della demo e poi riapplica le migrazioni. Senza `--reset` quegli schemi restano al loro posto.

`IEOL_DEMO_DATABASE` cambia il nome (default `ieol_demo`). `IEOL_DEMO_DATABASE_URL` indica un URL completo. Il nome deve essere diverso da quello del database principale.

Se `ieol_demo` esiste già basta poterci entrare. Se manca, `ieol demo` lo crea: servono `CREATEDB` e l'accesso al database `postgres`. In alternativa si crea `ieol_demo` prima e si concede l'accesso al ruolo dell'applicazione.
