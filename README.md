# IEOL — guida operativa

La visione di prodotto è in `README (1).md`. Il confine della V1 è in `docs/v1.md`. Qui c'è solo come si avvia, si migra, si prova e si mette in esercizio.

## Requisiti

- Python 3.11 o successivo
- PostgreSQL 16, la stessa versione usata da Compose e dalla CI
- `pip`

Il ruolo usato da `DATABASE_URL` crea il database della demo solo se manca: in quel caso servono `CREATEDB` e l'accesso al database `postgres`. Se `ieol_demo` esiste già, basta poterci entrare.

## Setup locale

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

`.env` resta fuori dal repository. I valori vuoti di `.env.example` non bastano per avviare l'API: vanno compilati, come sotto.

### PostgreSQL con Docker Compose

`docker-compose.yml` avvia un Postgres 16 locale. La porta dell'host è `127.0.0.1:5432`, quindi il container non è raggiungibile dalle altre interfacce. L'utente è `ieol`, la password è `ieol-local-only`, i database creati al primo avvio sono `ieol`, `ieol_test` e `ieol_demo`. Quella password vale solo per questo container di sviluppo.

```bash
docker compose up -d
```

Poi, nello stesso ambiente in cui lanci i comandi:

```bash
export DATABASE_URL='postgresql://ieol:ieol-local-only@127.0.0.1:5432/ieol'
export TEST_DATABASE_URL='postgresql://ieol:ieol-local-only@127.0.0.1:5432/ieol_test'
```

Lo script di init gira solo a volume vuoto. Un volume già presente non crea i database mancanti.

### PostgreSQL già installato sulla macchina

Il default di `DATABASE_URL` è `postgresql://ubuntu@/ieol`: ruolo `ubuntu`, autenticazione peer, database `ieol`. I test, se `TEST_DATABASE_URL` manca, usano `postgresql://ubuntu@/ieol_test`. Il nome del database di test deve finire con `_test`.

```bash
createdb ieol
createdb ieol_test
createdb ieol_demo
```

### Schema e processo

`ieol migrate` aggiorna il database di `DATABASE_URL`. `ieol demo` legge un database dedicato e non applica migrazioni, quindi quello va migrato prima, con `DATABASE_URL` puntato lì. Con Compose:

```bash
ieol migrate
DATABASE_URL='postgresql://ieol:ieol-local-only@127.0.0.1:5432/ieol_demo' ieol migrate
ieol demo
```

Con l'installazione peer il secondo comando è `DATABASE_URL=postgresql://ubuntu@/ieol_demo ieol migrate`.

L'API non applica migrazioni. Si avvia dopo `ieol migrate`:

```bash
uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

In HTTP locale il cookie va reso leggibile dal browser con `IEOL_INSECURE_COOKIES=1`. Su HTTPS il cookie resta `https_only` e quella variabile non si imposta.

## Variabili d'ambiente

| Variabile | Uso |
| --- | --- |
| `DATABASE_URL` | Database dell'applicazione. Default `postgresql://ubuntu@/ieol`. |
| `TEST_DATABASE_URL` | Database di pytest. Se manca, `postgresql://ubuntu@/ieol_test`. |
| `IEOL_SESSION_SECRET` | Firma il cookie di sessione. In alternativa vale `SESSION_SECRET`. |
| `SESSION_SECRET` | Stesso ruolo di `IEOL_SESSION_SECRET`. Una delle due basta. |
| `IEOL_INSECURE_COOKIES` | `1` solo per HTTP locale. Altrimenti il cookie è `https_only`. |
| `IEOL_DEV` | Solo sviluppo locale. Vedi sotto. |
| `IEOL_SEED_ADMIN_PASSWORD` | Password iniziale del ruolo `admin`. |
| `IEOL_SEED_MAPPER_PASSWORD` | Password iniziale del ruolo `mapper`. |
| `IEOL_SEED_OPERATOR_PASSWORD` | Password iniziale del ruolo `operator`. |
| `IEOL_SEED_APPROVER_PASSWORD` | Password iniziale del ruolo `approver`. |
| `IEOL_DEMO_DATABASE` | Nome del database di `ieol demo`. Default `ieol_demo`. |
| `IEOL_DEMO_DATABASE_URL` | URL completo del database di demo, al posto del nome. |
| `IEOL_DEMO_MODE` | `1` abilita `POST /api/demo/load` sul database di `DATABASE_URL`. Fuori da una demo esplicita resta spento. Vedi il limite noto sotto. |

### Sessione

`IEOL_SESSION_SECRET` firma il cookie di sessione. È accettato anche `SESSION_SECRET`. Se mancano entrambi, o se sono solo spazi, l'API rifiuta l'avvio.

Il cookie è `https_only`, salvo `IEOL_INSECURE_COOKIES=1` per un HTTP locale.

`IEOL_DEV=1` è solo per una macchina di sviluppo. In quel caso, e solo in quel caso, una chiave incorporata nel codice firma la sessione quando il segreto manca, e `scripts/seed_dev_users.py` accetta di creare gli utenti. Quella chiave è inadatta a qualsiasi ambiente condiviso. In produzione `IEOL_DEV` resta assente e il segreto di sessione è obbligatorio.

### Utenti

Le password non sono nel codice, nelle fixture o nel form di login. All'avvio, per ogni ruolo con la variabile impostata, viene creato l'utente se non esiste già:

- `IEOL_SEED_ADMIN_PASSWORD`
- `IEOL_SEED_MAPPER_PASSWORD`
- `IEOL_SEED_OPERATOR_PASSWORD`
- `IEOL_SEED_APPROVER_PASSWORD`

La password non può coincidere con il nome del ruolo (`admin`, `mapper`, `operator`, `approver`). Un valore vuoto non crea il ruolo se manca. Se l'utente esiste già e la password non è il nome del ruolo, l'hash non viene toccato.

Se l'utente esiste e la password è ancora il nome del ruolo (hash SHA-256 storico oppure già argon2), all'avvio viene sostituita: con la password della variabile d'ambiente, se presente; altrimenti con un segreto casuale che non viene stampato. La vecchia password smette di funzionare e l'account resta bloccato: il segreto casuale non è recuperabile dai log, e un avvio successivo con la variabile impostata non riscrive un hash che non è più il nome del ruolo. Prima del primo avvio in produzione le quattro variabili vanno quindi impostate.

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

Lo script non stampa password né hash. Senza `IEOL_DEV=1` si ferma.

### Hash delle password

Le password nuove sono argon2id (parametri di default di argon2-cffi: memoria 64 MiB, 3 iterazioni, parallelismo 4).

Gli hash SHA-256 storici (`sha256("ieol:" + password)` con sale fisso) e gli hash `scrypt$` restano validi al login e, al primo accesso riuscito, vengono sostituiti con argon2id. Non serve un reset di massa, salvo il caso sopra: se la password è il nome del ruolo viene ritirata all'avvio, anche senza un login.

### Demo

`IEOL_DEMO_DATABASE` cambia il nome (default `ieol_demo`). `IEOL_DEMO_DATABASE_URL` indica un URL completo. Il nome deve essere diverso da quello del database principale e non può essere `postgres`, `template0` o `template1`.

`ieol demo` da riga di comando non dipende da `IEOL_DEMO_MODE`: usa il database dedicato descritto sotto.

### Limite noto di `POST /api/demo/load`

L'endpoint scrive sul database di `DATABASE_URL`, lo stesso collegato all'API. Non dirotta la scrittura su `ieol_demo` e non rifiuta la richiesta quando il database collegato ha un altro nome: con `DATABASE_URL` puntato a `ieol`, `mapper` e `admin` caricano le fixture lì e `ieol_demo` resta vuoto. Con `IEOL_DEMO_MODE=1` la chiamata è consentita a `admin` e a `mapper`; `operator` e `approver` ricevono 403 (`Serve il ruolo mapper`) e non scrivono. Senza sessione la risposta è 401. Senza `IEOL_DEMO_MODE=1` la risposta è 403 (`Il demo è chiuso`) e non c'è scrittura. Il pulsante «Carica fixture» compare per ogni ruolo autenticato quando la modalità demo è accesa; il rifiuto dei ruoli diversi da `admin` e `mapper` avviene solo sulla richiesta.

## Migrazioni

I file in `db/migrations/` sono numerati e si applicano in ordine. Il runner registra le versioni in `public.schema_migrations`. È un esecutore SQL piccolo, senza Alembic: lo schema è già SQL e non serve un secondo modello da generare.

```text
ieol migrate
```

Il comando legge `DATABASE_URL`, applica solo le migrazioni non ancora registrate e fa commit. Non esegue `DROP` né `TRUNCATE`. Eseguirlo di nuovo lascia i dati invariati. Due processi che lo lanciano insieme prendono `pg_advisory_xact_lock` con una chiave fissa: il secondo attende il commit del primo e trova le versioni già registrate.

Un database già creato con `db/schema.sql` viene riconosciuto. Se ci sono tutte le tabelle, le colonne, la funzione e il trigger dell'audit, la migrazione `0001_initial` è segnata come baseline (`baselined = true`) e il suo SQL non viene eseguito. Righe, identità delle tabelle e registro audit restano quelli già presenti. Se lo schema c'è solo in parte, il comando si ferma senza scrivere.

`0002_provenance_audit_quality` aggiunge la provenienza dei fatti (`source_class` con default `unknown`, confidenza facoltativa e `confidence_method` con default `unknown`), il blocco del `TRUNCATE` sull'audit, le tabelle di revisione identità, override, qualità, listino e quarantena, e i permessi di `ieol_app` (inserisce nell'audit e non può aggiornarlo né svuotarlo). Non riempie la confidenza a 1, non marca i fatti esistenti come manuali e non inserisce righe di listino: quelle stanno nelle fixture della demo. Se quegli oggetti ci sono già, anche `0002` è baseline e il suo SQL non viene eseguito. Un database fermo allo schema storico riceve la `0001` come baseline e poi la `0002`.

`db/schema.sql` resta il riferimento storico. Una modifica successiva è un file nuovo in `db/migrations/`. Una migrazione già applicata non si riscrive. In esercizio lo schema si aggiorna solo con `ieol migrate`: `db/schema.sql` non si applica a mano.

All'avvio l'API non applica migrazioni. Controlla se ne restano in sospeso e, in quel caso, rifiuta di partire con il messaggio di eseguire `ieol migrate`. Solo quel comando aggiorna lo schema.

## Demo da riga di comando

`ieol demo` usa un database dedicato, `ieol_demo`, e lo crea se manca. Le query sono qualificate con `app.` e `src.`, quindi un altro schema nello stesso database non le isolerebbe. Il database di `DATABASE_URL` non viene letto né modificato.

```text
ieol demo
ieol demo --reset
ieol expire
ieol execute
```

`ieol expire` e `ieol execute` continuano la stessa demo, sullo stesso database dedicato. Nessuno dei tre comandi applica migrazioni.

Prima di `ieol demo` lo schema del database dedicato va aggiornato con `ieol migrate`, puntando `DATABASE_URL` a quel database:

```text
DATABASE_URL=postgresql://ubuntu@/ieol_demo ieol migrate
ieol demo
```

`--reset` è la conferma esplicita: cancella gli schemi `app` e `src` solo dentro il database della demo. Dopo il reset lo schema si ricostruisce di nuovo con `ieol migrate` su quel database. Senza `--reset` quegli schemi restano al loro posto. `ieol demo --reset` su un database senza migrazioni si ferma e chiede `ieol migrate`.

Se `ieol_demo` esiste già basta poterci entrare. Se manca, `ieol demo` lo crea: servono `CREATEDB` e l'accesso al database `postgres`. In alternativa si crea `ieol_demo` prima e si concede l'accesso al ruolo dell'applicazione.

## Test

I test vivono in `tests/` e richiedono il database di `TEST_DATABASE_URL` (oppure `postgresql://ubuntu@/ieol_test`). Ogni test cancella gli schemi `app` e `src` solo se il database corrente è quello di `DATABASE_URL` e il nome finisce con `_test`.

```bash
TEST_DATABASE_URL='postgresql://ieol:ieol-local-only@127.0.0.1:5432/ieol_test' pytest
```

Con il default peer e il database `ieol_test` già creato, basta `pytest`. `pytest` applica da solo le migrazioni sul database di test. La CI, in più, lancia `ieol migrate` su quello stesso database prima della suite.

`IEOL_DEV` non serve ai test: la suite rifiuta l'avvio senza segreto di sessione e copre il caso `IEOL_DEV=1` in un processo separato.

## Deploy

Ordine, su un database che contiene già dati:

1. Backup, prima di qualsiasi migrazione:

   ```bash
   pg_dump --format=custom --file=ieol-pre-migrate.dump "$DATABASE_URL"
   ```

2. Impostare l'ambiente di produzione:
   - `DATABASE_URL` verso il database da aggiornare
   - `IEOL_SESSION_SECRET` oppure `SESSION_SECRET`, con un valore non vuoto
   - `IEOL_SEED_ADMIN_PASSWORD`, `IEOL_SEED_MAPPER_PASSWORD`, `IEOL_SEED_OPERATOR_PASSWORD`, `IEOL_SEED_APPROVER_PASSWORD`, diverse dal nome del ruolo
   - `IEOL_DEV` assente
   - `IEOL_INSECURE_COOKIES` assente, così il cookie di sessione resta `https_only`
   - `IEOL_DEMO_MODE` assente, salvo un ambiente di demo esplicito

3. Applicare lo schema solo con il runner:

   ```bash
   ieol migrate
   ```

   `db/schema.sql` non si esegue a mano, né su un database nuovo né su uno già popolato. È il riferimento storico: un database nato da quel file viene riconosciuto da `ieol migrate` e marcato come baseline, senza rieseguire `0001_initial`.

4. Riavviare l'API. Se resta una migrazione in sospeso il processo si rifiuta di partire e indica `ieol migrate`. L'avvio non migra da solo.

Gli account il cui hash è ancora la password uguale al nome del ruolo, e per i quali manca `IEOL_SEED_<RUOLO>_PASSWORD`, vengono bloccati all'avvio: l'hash è sostituito con un segreto casuale che non viene stampato. Il login con il nome del ruolo fallisce. Reimpostare la variabile a un avvio successivo non sblocca quell'account, perché l'hash non coincide più con il nome del ruolo. Un ruolo assente dal database, con variabile vuota, non viene creato e quindi non ha accesso.

La CI (`.github/workflows/ci.yml`) usa solo valori di test scritti nel workflow. Quei valori non sono segreti di produzione e non vanno copiati in esercizio.
