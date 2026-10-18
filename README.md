# IEOL — guida operativa

Visione di prodotto: [docs/VISIONE.md](docs/VISIONE.md).

Il confine della V1 è in `docs/v1.md`. Qui c'è solo come si avvia, si migra, si prova e si mette in esercizio.

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

Lo script di init gira solo a volume vuoto. Un volume già presente non crea i database mancanti. Al primo avvio lo script crea anche il ruolo `ieol_app` con password `ieol-app-local-only`, usabile solo in questo container. `ieol migrate` e `ieol reset-password` usano l'owner di `DATABASE_URL` e concedono a `ieol_app` i privilegi sulle tabelle. L'API non fa `SET ROLE`: entra con `IEOL_APP_DATABASE_URL`, il cui utente è `ieol_app`.

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
| `IEOL_DEMO_MODE` | `1` abilita `POST /api/demo/load`. Fuori da una demo esplicita resta spento. La chiamata è accettata solo se il database collegato si chiama `ieol_demo` e il ruolo è `admin`. |

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

### `POST /api/demo/load`

L'endpoint scrive sul database di `DATABASE_URL`. Non dirotta la scrittura su un altro nome: se il database collegato non si chiama `ieol_demo`, la risposta è 409 (`Il demo si carica solo sul database ieol_demo`) e non c'è scrittura. Con `IEOL_DEMO_MODE=1` la chiamata è consentita solo ad `admin`. `mapper`, `operator` e `approver` ricevono 403 (`Serve il ruolo admin`). Senza sessione la risposta è 401. Senza `IEOL_DEMO_MODE=1` la risposta è 403 (`Il demo è chiuso`). Il pulsante «Carica fixture» compare solo per `admin` quando la modalità demo è accesa.

Per caricare la demo dall'API, l'API va avviata con `DATABASE_URL` puntato a `ieol_demo` e con `IEOL_DEMO_MODE=1`. `ieol demo` da riga di comando resta sul database dedicato e non passa da questo endpoint.

Un file caricato da CSV o Excel non può superare 5 MiB né 5000 righe. La classe di provenienza è obbligatoria: `system_of_record`, `operational`, `external`, `manual` o `unknown`. Non si ricava dal nome della fonte.

### Aziende

Le aziende si creano e gli utenti si collegano solo da questi comandi, sul server, con l'owner di `DATABASE_URL`. Non c'è una pagina e non c'è un'API. Ogni operazione andata a buon fine scrive una riga di audit (`company_create` o `user_company_assign`) nella stessa transazione.

Un'azienda pilota si crea solo dopo la firma dell'accordo sui dati. È una regola operativa: il comando non controlla la firma e non blocca l'esecuzione.

```text
ieol company create --name "Officina Nord"
ieol company assign --username mapper --company-id <uuid stampato dal create>
ieol company create-demo --name "Demo interna"
```

`create` lascia `is_demo` falso. `create-demo` lo imposta vero, qualunque sia il nome: le misure non cercano la parola «demo». Un utente riceve l'azienda una sola volta. Se ne ha già una, il comando si ferma e non scrive l'audit di successo. Per cambiare azienda serve un utente nuovo.

Chi non ha un'azienda non può caricare un file nuovo: CSV, Excel, SQL, email e PDF rispondono 403 con «Il tuo utente non è ancora collegato a un'azienda: chiedi all'amministratore». La stessa risposta vale per ogni scrittura di decisione: conferma, rifiuto, annullamento di una correzione, tipo di file, «Conferma i suggerimenti forti» e «Aggiorna modello». La lettura resta aperta. In pagina non compaiono il caricamento né quei pulsanti, e si legge la stessa frase, scritta come testo e non come HTML.

`is_demo` si imposta alla creazione. Il database rifiuta di cambiarlo dopo: rinominare l'azienda non la fa entrare né uscire dalle misure.

La fonte del primo caricamento prende l'azienda di chi carica. Una fonte già presente senza azienda non viene aggiornata: lo stesso nome in un'altra azienda è un'altra fonte. Il batch nasce con l'azienda del caricamento e il database rifiuta di aggiungerla o cambiarla dopo.

### Password di un ruolo già creato

```text
ieol reset-password approver
```

Il comando legge `IEOL_SEED_<RUOLO>_PASSWORD` (`admin`, `mapper`, `operator`, `approver`), rifiuta una password uguale al nome del ruolo e riscrive l'hash con argon2id. Gira come owner di `DATABASE_URL`, non come `ieol_app`. Non stampa la password. Se la variabile o l'utente mancano, si ferma senza scrivere.

## Mapping delle colonne

Una colonna si assegna in quest'ordine. La chiave italiana del profilo, scritta com'è (`email`, `ord_head`, `cod_art`), usa `mapping.header_similarity@v1`. Prima del confronto il nome viene normalizzato: minuscolo, accenti tolti, camelCase spezzato, e ogni carattere che non è una lettera o una cifra diventa uno spazio (`Weight (lbs)` → `weight lbs`, `In stock?` → `in stock`, `dell'imposta` → `dell imposta`). La stessa chiave già nel profilo usa allora `mapping.normalized_header@v1` e tiene lo stesso bersaglio e lo stesso punteggio. Un sinonimo di `ontology/synonyms.yaml` usa `mapping.synonym@v1`. Se resta libera, una parola normalizzata può coincidere con `ontology/tokens.yaml` (`mapping.token@v1`): il vocabolario è schema.org (Order, Offer, Invoice, PostalAddress, Person, Organization) ed è stato aggiunto dopo la prima misura hold-out. Il suo punteggio di regola sta sotto quello dei sinonimi. Il numero non è una probabilità. Se non c'è un bersaglio unico la colonna resta `Document.title` a 0,42 con `mapping.unknown_header@v1`.

`First Name` e `Last Name` sono parti di un nome composto. L'ontologia non ha `Person.given_name` né `Person.family_name`, quindi restano sotto 0,75 e non scrivono `Person.full_name`: una colonna non sostituisce l'altra.

Il tipo di tabella si riconosce anche da intestazioni inglesi e dalle varianti di Odoo, HubSpot e Danea (per esempio `Amount` con `Deal Name`, `Internal Reference` con `Cost`, `Quantity On Hand`, `Delivery Lead Time`). Se quei nomi non ci sono, `mapping.table_shape@v1` guarda la forma: ruoli già noti a sinonimi e token, più il tipo dei valori (date, importi con due decimali, email, partita IVA, telefono) e una colonna identificativa ripetuta sulle righe. Un intero o un numero con una sola cifra decimale (`18`, `45.0`) conta come importo solo se l'intestazione è già un prezzo o un importo: una colonna quantità di interi non diventa un prezzo. La chiave ripetuta richiede almeno 5 valori distinti, una quota di valori distinti almeno del 10% e sotto il 100%, e una ripetizione su righe consecutive. Le righe d'ordine chiedono una quantità sulla riga e, insieme, una data oppure un riferimento d'ordine: la chiave ripetuta vale come riferimento solo se la riga non è già un prodotto a sé. Se un codice prodotto (`sku` o un id già noto) è unico su ogni riga e non c'è una chiave d'ordine né una data d'ordine nell'intestazione, il file è un catalogo. Il ruolo `catalog` blocca le regole delle righe d'ordine: una colonna attributo che si ripete, un prezzo intero e una quantità di giacenza non bastano. Se il tipo letto dalle intestazioni e il tipo letto dalla forma non coincidono, il punteggio combinato scende a 0,40, sotto 0,75, e il file resta sconosciuto. Il numero è un punteggio di regola, non una probabilità. Sotto 0,75 il file resta sconosciuto e non viene indovinato. Nell'interfaccia il tipo compare solo come etichetta fissa, per esempio `Probabile export ordini`, oppure `Tipo non riconosciuto`. Non c'è una percentuale.

### Fonti

I sinonimi sono costruiti da Odoo 17 (`res_partner.xlsx`, `product_template.xls`, `product_purchase.xls` e le etichette italiane in `it.po`), HubSpot (pagine delle proprietà di contatti, aziende e trattative), Danea Easyfatt (pagina del tracciato XML) e, per `sku` e `price`, da schema.org (`Product.sku`, `Offer.price`). Ogni voce ha `source` con `file:<nome>` oppure `doc:<url>`. `Regular price` è un'intestazione di WooCommerce, non di schema.org, e non è nel dizionario: un file così resta `Tipo non riconosciuto`. WooCommerce è rivisto dopo la prova e non è un test indipendente. Magento è visto dopo la prova cieca sui CSV prodotto pubblici e non è un test indipendente: i suoi nomi colonna non entrano nei sinonimi. La prova è PROVVISORIA. La misura vera richiede file pilota. Shopify (export ordini) e FatturaPA 1.2.2 sono tenute fuori dal dizionario. Quelle intestazioni stanno in `evals/holdout_headers.yaml` e servono solo alla misura PROVVISORIA. La soglia non cambia: resta il ripiego 0,75. I punteggi sono punteggi di regola, non probabilità.

La misura hold-out usa 2 fonti. Il minimo per una soglia misurata resta 30 casi umani e 3 fonti distinte. Con 2 fonti la soglia resta non misurata e vale il ripiego 0,75 (`calibrated=false`). La prima misura, prima del vocabolario token, ha 2 colonne su 32 sopra 0,75. Lo script `python scripts/holdout_mapping.py` ristampa quella cifra e la misura successiva a `mapping.token@v1`. Non scrive su `app.mapping_decisions` né su `mapping_thresholds`.

## Migrazioni

I file in `db/migrations/` sono numerati e si applicano in ordine. Il runner registra le versioni in `public.schema_migrations`. È un esecutore SQL piccolo, senza Alembic: lo schema è già SQL e non serve un secondo modello da generare.

```text
ieol migrate
```

Il comando legge `DATABASE_URL`, applica solo le migrazioni non ancora registrate e fa commit. Rifiuta ogni `DROP` di `TABLE`, `SCHEMA`, `DATABASE`, `COLUMN`, `CONSTRAINT`, `NOT NULL`, `DEFAULT`, `INDEX`, `FUNCTION` e `TRIGGER`, in qualsiasi punto della riga, senza distinzione di maiuscole, dopo aver ignorato i commenti SQL. Rifiuta anche `TRUNCATE`, `DISABLE TRIGGER`, `ENABLE TRIGGER` e `ALTER TABLE ... DISABLE`. `BEFORE TRUNCATE` e `REVOKE ... TRUNCATE` restano ammessi: non svuotano una relazione. L'eccezione è un'allowlist di istruzioni esatte, per file. In `0003_decisions_source_class_checksums.sql` e in `0004_source_vocabulary_decision_sources.sql` è ammessa la riga `ALTER TABLE app.facts DROP CONSTRAINT IF EXISTS facts_source_class_check`. In `0004` è ammessa anche `ALTER COLUMN confidence DROP NOT NULL`. In `0005_pii_testset_audit.sql` è ammessa `ALTER TABLE app.audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check` e, solo dentro `shield_existing_pii`, le quattro righe che disabilitano e riabilitano i trigger append-only di audit e decisioni. La stessa istruzione in un file nuovo è rifiutata. In `0006_hash_version_v2.sql` è ammessa solo `ALTER TABLE app.decisions_log DROP CONSTRAINT IF EXISTS decisions_log_hash_version_check`. La stessa riga in `0005` o in un file successivo è rifiutata. In `0007_mapping_decisions_key_audit.sql` sono ammesse `ALTER TABLE app.audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check` e `ALTER TABLE app.labelled_cases DROP CONSTRAINT IF EXISTS labelled_cases_origin_check`. La prima resta ammessa anche in `0005`. La seconda solo in `0007`. In `0008_mapping_decision_snapshot.sql` sono ammesse solo le due `DROP CONSTRAINT` di `mapping_decisions_decision_check` e `mapping_decisions_confidence_method_check`. In `0010_companies_learned_corrections.sql` sono ammesse quattro righe: `ALTER TABLE app.mapping_decisions DROP CONSTRAINT IF EXISTS mapping_decisions_decision_check`, `ALTER TABLE app.sources DROP CONSTRAINT IF EXISTS sources_pkey`, `ALTER TABLE app.landing_batches DROP CONSTRAINT IF EXISTS landing_batches_source_name_watermark_key` e `ALTER TABLE app.ingest_watermarks DROP CONSTRAINT IF EXISTS ingest_watermarks_pkey`. La `DROP` di `mapping_decisions_confidence_method_check` resta solo in `0008`. La stessa riga di `0010` in un file successivo è rifiutata. In `0011_company_assignment.sql` è ammessa solo `ALTER TABLE app.audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check`, la stessa riga già ammessa in `0005` e `0007`. In un file successivo è rifiutata. Un altro `DISABLE TRIGGER` nello stesso file è rifiutato. Eseguirlo di nuovo lascia i dati invariati. Se `app.evidence_hmac_key` ha già una riga e `IEOL_EVIDENCE_HMAC_KEY` è diversa, il comando si ferma senza scrivere. `ieol migrate --rotate-evidence-key` sostituisce quell'unica riga. Due processi che lo lanciano insieme prendono `pg_advisory_xact_lock` con una chiave fissa: il secondo attende il commit del primo e trova le versioni già registrate.

Un database già creato con `db/schema.sql` viene riconosciuto. Se ci sono tutte le tabelle, le colonne, la funzione e il trigger dell'audit, la migrazione `0001_initial` è segnata come baseline (`baselined = true`) e il suo SQL non viene eseguito. Righe, identità delle tabelle e registro audit restano quelli già presenti. Se lo schema c'è solo in parte, il comando si ferma senza scrivere.

`0002_provenance_audit_quality` aggiunge la provenienza dei fatti (`source_class` con default `unknown`, confidenza facoltativa, `confidence_method` con default `unknown` e `confidence_rule` nullable con id e versione della regola), il blocco del `TRUNCATE` sull'audit, le tabelle di revisione identità, override, qualità, listino e quarantena, e i permessi di `ieol_app` (inserisce nell'audit e non può aggiornarlo né svuotarlo). Non riempie la confidenza a 1, non marca i fatti esistenti come manuali e non inserisce righe di listino: quelle stanno nelle fixture della demo. Se quegli oggetti ci sono già, anche `0002` è baseline e il suo SQL non viene eseguito. Un database fermo allo schema storico riceve la `0001` come baseline e poi la `0002`.

`0003_decisions_source_class_checksums` aggiunge il registro delle decisioni (`app.decisions_log`, append-only, collegato a `batch_id` e `row_index`), la tabella `app.sources` con la classe dichiarata del connettore, il vincolo che impedisce update, delete e truncate anche su `app.agent_overrides`, la colonna `checksum` in `schema_migrations`, e i grant di `ieol_app` comprese le `ALTER DEFAULT PRIVILEGES` per le tabelle create dopo. Il registro delle decisioni non è un dataset di training: l'agente non lo legge. `ieol migrate` registra lo SHA-256 di ogni file applicato. Se un file già applicato cambia, il comando si ferma e non scrive. Una riga vecchia senza checksum viene riempita una sola volta con l'hash del file attuale; da quel momento il file non si modifica. L'unica istruzione di questo file in allowlist è `ALTER TABLE app.facts DROP CONSTRAINT IF EXISTS facts_source_class_check`: sostituisce `facts_source_class_check`.

`0004_source_vocabulary_decision_sources` converte le classi legacy dei fatti e lascia un solo vocabolario: `system_of_record`, `operational`, `external`, `manual`, `unknown`. La mappa è `erp` → `system_of_record`, `crm` → `operational`, `ai` → `unknown`, `calculated` → `operational`. `manual`, `external` e `unknown` restano. Quel `calculated` → `operational` riguarda il token già scritto nei fatti vecchi. Un fatto calcolato nuovo tiene `origin = calculated` e prende la classe più debole tra gli input, in ordine `system_of_record`, `operational`, `external`, `manual`, `unknown`: se un input è `unknown`, il risultato è `unknown`. La confidenza resta null e `confidence_method` resta `unknown`, salvo una regola dedicata dichiarata in `confidence_rule`. Poi riusa la `DROP CONSTRAINT` in allowlist, stringe il `CHECK`, e con la seconda istruzione in allowlist toglie il `NOT NULL` da `decisions_log.confidence`. Sul registro aggiunge `confidence_method` e `confidence_rule`; se `confidence` è null, `confidence_method` deve essere `unknown`. Aggiunge `app.decisions_log_sources`, append-only, con tutte le righe di origine della testata e delle righe ordine. Le raccomandazioni già presenti ricevono `confidence_method = unknown`: il numero, se c'è, non viene rietichettato. Le proposte nuove registrano il punteggio di regola `order-agent.baseline@v1`. Il commento in testa a `0004` è stato aggiornato prima del merge e il checksum del file è cambiato: un database di sviluppo che aveva già applicato la versione precedente di questo file va ricreato prima di un nuovo `ieol migrate`.

`0005_pii_testset_audit` aggiunge le colonne di chiaro (`value_cleartext`, `data_cleartext`, `samples_cleartext`, `recipient_cleartext`) e il ruolo `ieol_pii`, senza login finché non si esegue `ALTER ROLE`. `ieol_app` perde il `SELECT` di tabella e riceve solo le colonne mascherate: non può leggere il chiaro. I trigger spostano i dati personali al momento dell'inserimento. Lo stesso file scansiona le righe già presenti e le riscrive solo se contengono ancora chiaro; audit e decisioni, append-only, vengono aggiornati solo in quel caso e il chiaro non viene copiato nel payload. L'hash delle evidenze è un HMAC. `ieol migrate` inserisce `IEOL_EVIDENCE_HMAC_KEY` in `app.evidence_hmac_key` se la riga manca. Se la riga c'è e la variabile è diversa, si ferma, salvo `--rotate-evidence-key`, che sovrascrive quell'unica riga. `ieol_app` non può leggere né sostituire quella riga. `app.hmac_evidence` accetta solo gli id dei fatti, non una chiave scelta dal chiamante. Il chiaro delle altre letture passa da una sola funzione auditata, come `ieol_pii`. Aggiunge `hash_version` su `decisions_log`: le righe già presenti restano `sha256` (il default della colonna, senza `UPDATE`), le righe nuove sono `hmac-sha256-v1`. Aggiunge il test set (`testset_versions`, `labelled_cases`, `testset_evals`) e `mapping_thresholds`, tutti append-only. `ieol testset build` e `ieol testset eval` usano `IEOL_APP_DATABASE_URL`. I casi vengono da decisioni umane con tutte le fonti a confidenza diversa da `unknown`, da fixture `hand_written`, oppure da etichette `synthetic` (quelle scritte dall'agente in `evals/hand_labels.json` e `evals/order_cases.json`). I synthetic restano nel test set per i test del codice e non contano nel minimo di 30 né nella scelta della soglia. Sotto 30 casi di mapping non synthetic la soglia resta 0,75, metodo `fallback`, `measured` false, con la versione di regola `mapping.header_similarity@v1`. La soglia è misurata solo con almeno 30 casi non synthetic e almeno 3 fonti distinte: è la mediana dei fold per fonte. Una fonte non sta sia nella scelta della soglia sia nella valutazione. Con meno di cinque fonti il fold è leave-one-source-out; da cinque in su è un fold di fonti, non di indice. Sotto quei minimi, o con una sola fonte, la soglia resta 0,75. Un caso senza `source_name` non entra nei fold e viene contato a parte. Il punteggio e la regola sono quelli fotografati sulla decisione, non ricalcolati. `measured` non è una calibrazione: l'interfaccia non mostra una percentuale e, se la soglia è misurata, scrive `soglia misurata su N casi`. L'agente in esecuzione è lo stesso delle regole, quindi il report dice `agente = regole, nessun confronto possibile` e non pubblica due gruppi di metriche. Se `shield_existing_pii` maschera delle righe, scrive in audit quante sono e con quale versione. Il check delle azioni di audit ammette `pii_unmask`, `pii_read` e `pii_mask`. Un database di sviluppo che aveva già applicato una `0005` precedente va ricreato: il file è cambiato e il checksum non coincide.

`0006_hash_version_v2` allarga il `CHECK` di `decisions_log.hash_version` e ammette anche `hmac-sha256-v2`. Non cambia il default, che resta `hmac-sha256-v1`. Non riscrive le righe. L'applicazione continua a scrivere `hmac-sha256-v1`. Non ruota la chiave. L'unica istruzione in allowlist è `ALTER TABLE app.decisions_log DROP CONSTRAINT IF EXISTS decisions_log_hash_version_check`: toglie il vincolo della `0005` e lo ricrea con i tre valori `sha256`, `hmac-sha256-v1` e `hmac-sha256-v2`.

### Rotazione della chiave HMAC

Questa versione non ruota la chiave. Prepara solo il valore `hmac-sha256-v2` sul registro delle decisioni.

`app.evidence_hmac_key` ha una sola riga. Il primo `ieol migrate` che trova la tabella vuota scrive `IEOL_EVIDENCE_HMAC_KEY`. Se la riga c'è già e la variabile è uguale, il comando non la tocca. Se la variabile è diversa, `ieol migrate` si ferma con un errore e non scrive. Per sovrascrivere quell'unica riga serve `ieol migrate --rotate-evidence-key`. Il flag non aggiunge una seconda chiave. La verifica ricalcola l'HMAC con la chiave corrente, quindi dopo la sostituzione le raccomandazioni già firmate scadono. Una rotazione che tenga la chiave vecchia non è questo flag. Resta da fare in un cambio successivo:

1. Tenere la chiave `hmac-sha256-v1` finché il codice non verifica gli hash vecchi in base a `hash_version`.
2. Solo allora si può cambiare la chiave delle decisioni nuove, senza far scadere le firme vecchie.
3. Scrivere `hmac-sha256-v2` solo sulle decisioni nuove. Le righe già presenti restano `sha256` o `hmac-sha256-v1`. Il registro è append-only: nessun aggiornamento delle righe esistenti.

Finché quei tre punti non sono nel codice, le decisioni nuove restano `hmac-sha256-v1`. `--rotate-evidence-key` non va usato al loro posto. Quando quel flag sostituisce davvero la chiave, scrive in audit una riga `evidence_key_rotate`: ora, utente del database, nessun valore della chiave. La prima scrittura della riga e una variabile uguale a quella già salvata non scrivono questa riga.

`0007_mapping_decisions_key_audit` aggiunge l'azione `evidence_key_rotate` e la tabella append-only `app.mapping_decisions`. Una conferma esplicita di mapping (`POST /api/mappings/{id}/confirm`) e un rifiuto scrivono una decisione `human`, con attore e `decided_at`. I synthetic restano esclusi dal minimo e dalla soglia.

`0008_mapping_decision_snapshot` non tocca `0007`. Allarga `decision` a `confirm` e `reject`, e `confidence_method` a `human` e `bulk_rule`. Aggiunge la fotografia `source_name`, `source_field`, `target_property`, `score`, `confidence_rule` e la colonna `target_property_corrected`. `POST /api/mappings/confirm-confident` scrive una riga `bulk_rule` con attore e ora: non è un'etichetta umana. Il report conta i casi umani sopra e sotto la soglia, le righe `bulk_rule` ancora ultime per il loro mapping, e i casi senza fonte.

`0009_mapping_decision_snapshot_checks` non tocca `0007` né `0008`. Due `CHECK` `NOT VALID`: le righe nuove hanno `source_name`, `source_field`, `target_property`, `score` e `confidence_rule` non null; `target_property_corrected` è ammessa solo con `decision = reject`. Le righe già scritte non vengono rilette. Il test set tiene l'ultima decisione `human` di ogni mapping, conferma o rifiuto, e solo se il punteggio è fotografato. Un rifiuto è un errore. Una correzione è una previsione sbagliata. Il punteggio non si ricalcola.

`0011_company_assignment` non riscrive le righe già presenti: non contiene `UPDATE`. Aggiunge `app.companies.is_demo`, falso di default, quindi un'azienda già creata non è demo. Il flag, non il nome, esclude i dati di quell'azienda dalle misure di riconoscimento delle colonne e di tipo file, anche quando il file sarebbe la prima volta. Un trigger impedisce di cambiare `is_demo` dopo la creazione: il nome si può ancora aggiornare. Un trigger lascia scrivere l'azienda di un utente solo da NULL a un valore: un cambiamento successivo è un utente nuovo. Un trigger impedisce di aggiungere o cambiare l'azienda di un batch dopo la creazione, e lo stesso vale per una fonte. I batch senza azienda restano NULL, fuori dalla prima volta e fuori dai suggerimenti appresi. Le azioni di audit ammesse includono `company_create` e `user_company_assign`. `ieol_app` può leggere le aziende e non può crearle né aggiornarle; sugli utenti può inserire le colonne dell'account e aggiornare solo `password_hash`.

`0010_companies_learned_corrections` non riscrive le righe già presenti. Aggiunge `app.companies` e un `company_id` nullable su utenti, fonti, mapping e decisioni. Un `company_id` NULL non entra mai in un suggerimento. Il `company_id` di una richiesta si legge da `app.users` per l'utente autenticato: non è un parametro del client, né in query, né in form, né nel JSON. Assegnarlo agli utenti è un `UPDATE` operativo successivo, fuori da questa migrazione. Senza quell'assegnazione le decisioni nuove restano NULL e non diventano suggerimenti. La `0007` diceva che `app.mapping_decisions` non è un dataset che l'agente legge per proporre. La `0010` cambia quella regola nel commento vivo e non la aggira: il file `0007` non si riscrive. La review del mapping legge la tabella solo per un suggerimento appreso, con la stessa azienda, la stessa fonte e la stessa intestazione normalizzata. L'agente degli ordini non la legge. La chiave di `app.sources` diventa `(company_id, name)` con `UNIQUE NULLS NOT DISTINCT`: due aziende possono avere lo stesso nome e resta un solo nome per il `company_id` assente. Lo stesso vale per il batch di landing e per il watermark. `decision` ammette `confirm`, `reject` e `revoked`. `revoked` cita una conferma o un rifiuto, una sola volta. Il trigger esige la stessa azienda dell'attore su ogni decisione di mapping: un attore senza azienda coincide solo con una decisione senza azienda. L'annullamento in pagina resta sul rifiuto che ha una proprietà corretta. `app.table_type_decisions` è append-only e sta fuori da `mapping_decisions`, con decisioni `correct`, `wrong` e `not_sure`. `not_sure` non è il tipo proposto `unknown`. Il file deve essere della stessa azienda. `wrong` non può ripetere il tipo proposto. `rule_version` è la regola già fotografata sul batch (`proposed_rule`), non un ricalcolo. Sul mapping e sulla decisione si registra il batch del file. `origin` è `rule` o `learned`, con l'id della decisione sorgente. «Suggerimento forte» è il ripiego non calibrato 0,75. La nota «Indicazione automatica, da verificare» e l'elenco dei tipi arrivano dal server: in elenco non c'è «Probabile», il menu parte da «Scegli il tipo…» e «Registra tipo» resta disabilitato finché non si sceglie. Sotto i 700 px i pulsanti Conferma, Rifiuta e Annulla vanno a capo. Un utente senza azienda non vede i file. La conferma in blocco vale solo per i suggerimenti forti di regola, mai per quelli appresi. La prima volta è il batch più antico di un'azienda e di una fonte (`loaded_at`, poi l'id), non l'ora del mapping. Una decisione senza batch conta solo se quell'azienda e quella fonte non hanno nessun batch. Le conferme in blocco restano fuori dal riconoscimento. Conferme e rifiuti dei suggerimenti appresi escono da quella metrica. La metrica appresa conta l'ultima decisione di ogni mapping: confermati, rifiutati, e le revoche divise in `revoked.rule` e `revoked.learned` (un `origin` assente conta come regola). Sul tipo di file contano l'ultima decisione del primo batch: `not_sure` è un errore e non è un esempio di apprendimento; `wrong` con un tipo della lista chiusa è un esempio e non cambia da solo la regola del file successivo.

`db/schema.sql` resta il riferimento storico. Una modifica successiva è un file nuovo in `db/migrations/`. Una migrazione già applicata non si riscrive. In esercizio lo schema si aggiorna solo con `ieol migrate`: `db/schema.sql` non si applica a mano.

### Aggiornare un database già in uso

Su un database che ha già la `0002`:

1. Backup con `pg_dump`, come nel deploy.
2. `ieol migrate` con `DATABASE_URL` dell'owner. Applica solo i file non ancora registrati (`0003` fino a `0011` se il database è fermo alla `0002`; solo `0011` se è già alla `0010`) e scrive i checksum. Non riesegue i file già registrati. `0001`–`0006` non si modificano. `0008` non modifica `0007`. `0009` non modifica `0008`. `0010` non riscrive le righe già presenti: i `company_id` NULL restano NULL e non diventano suggerimenti. `0011` non assegna aziende alle righe già presenti e non marca come demo le aziende già create. Nessuna di queste migrazioni ruota la chiave da sola.
3. Se `ieol_app` non può ancora autenticarsi: `ALTER ROLE ieol_app WITH LOGIN PASSWORD '...'`. Impostare `IEOL_APP_DATABASE_URL` con quell'utente e lo stesso database. L'API entra così, senza `SET ROLE`. `RESET ROLE` su quella sessione non restituisce i privilegi dell'owner. Per il chiaro: `ALTER ROLE ieol_pii WITH LOGIN PASSWORD '...'` e `IEOL_PII_DATABASE_URL`. Impostare `IEOL_EVIDENCE_HMAC_KEY`: senza quella variabile l'API non parte. La chiave non va nel client.
4. Riavviare l'API con `IEOL_APP_DATABASE_URL`, `IEOL_PII_DATABASE_URL` e `IEOL_EVIDENCE_HMAC_KEY`. Se resta una migrazione in sospeso, l'avvio indica di eseguire `ieol migrate`.
5. Le fonti già caricate non hanno una classe dichiarata. I fatti `erp` e `crm` sono stati convertiti. I fatti che restano `unknown` bloccano l'approvazione con 422 (`provenienza sconosciuta`) e il pulsante Approva resta disabilitato con la stessa dicitura. Per sbloccarla si registra di nuovo la fonte con la classe giusta e si rimaterializza: un fatto il cui valore non cambia mantiene la classe precedente finché non viene riscritto.
6. `ieol reset-password <ruolo>` si usa se le password vanno riallineate alle variabili `IEOL_SEED_<RUOLO>_PASSWORD`. Gira sull'owner di `DATABASE_URL`. Non è obbligatorio per la migrazione.

Un volume Compose già esistente non rilancia lo script di init. Il ruolo `ieol_app` nasce dalla `0003`, senza password di login, finché non si esegue `ALTER ROLE`. L'API non usa `SET ROLE`.

All'avvio l'API non applica migrazioni. Controlla se ne restano in sospeso e, in quel caso, rifiuta di partire con il messaggio di eseguire `ieol migrate`. Solo quel comando aggiorna lo schema.

## Demo da riga di comando

`ieol demo` usa un database dedicato, `ieol_demo`, e lo crea se manca. Le query sono qualificate con `app.` e `src.`, quindi un altro schema nello stesso database non le isolerebbe. Il database di `DATABASE_URL` non viene letto né modificato.

```text
ieol demo
ieol demo --reset
ieol expire
ieol expire-stale
ieol execute
```

`ieol expire` e `ieol execute` continuano la stessa demo, sullo stesso database dedicato. `ieol expire-stale` gira sul database di `IEOL_APP_DATABASE_URL` e segna scadute le raccomandazioni le cui evidenze sono cambiate: la lettura dell'inbox non scrive. Nessuno di questi comandi applica migrazioni.

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

Con il default peer e il database `ieol_test` già creato, basta `pytest`. `pytest` applica da solo le migrazioni sul database di test e imposta `IEOL_APP_DATABASE_URL` verso `ieol_app` su `127.0.0.1`. La CI, in più, lancia `ieol migrate` su quello stesso database prima della suite e controlla che la sessione applicativa sia `ieol_app` anche dopo `RESET ROLE`.

`IEOL_DEV` non serve ai test: la suite rifiuta l'avvio senza segreto di sessione e copre il caso `IEOL_DEV=1` in un processo separato.

## Deploy

Ordine, su un database che contiene già dati:

1. Backup, prima di qualsiasi migrazione. Il dump è dato personale: vedi la sezione Backup.

   ```bash
   pg_dump --format=custom --file=ieol-pre-migrate.dump "$DATABASE_URL"
   ```

2. Impostare l'ambiente di produzione:
   - `DATABASE_URL` verso il database da aggiornare
   - `IEOL_SESSION_SECRET` oppure `SESSION_SECRET`, con un valore non vuoto
   - `IEOL_SEED_ADMIN_PASSWORD`, `IEOL_SEED_MAPPER_PASSWORD`, `IEOL_SEED_OPERATOR_PASSWORD`, `IEOL_SEED_APPROVER_PASSWORD`, diverse dal nome del ruolo
   - `IEOL_DEV` assente
   - `IEOL_INSECURE_COOKIES` assente, così il cookie di sessione resta `https_only`
   - `IEOL_APP_DATABASE_URL` con utente `ieol_app` e password propria, sullo stesso database di `DATABASE_URL`
   - `IEOL_PII_DATABASE_URL` con utente `ieol_pii` e password propria
   - `IEOL_EVIDENCE_HMAC_KEY`, valore non vuoto, solo sul server
   - `IEOL_DEMO_MODE` assente, salvo un ambiente di demo esplicito il cui database applicativo si chiama `ieol_demo`

3. Applicare lo schema solo con il runner:

   ```bash
   ieol migrate
   ```

   `db/schema.sql` non si esegue a mano, né su un database nuovo né su uno già popolato. È il riferimento storico: un database nato da quel file viene riconosciuto da `ieol migrate` e marcato come baseline, senza rieseguire `0001_initial`.

4. Riavviare l'API. Se resta una migrazione in sospeso il processo si rifiuta di partire e indica `ieol migrate`. L'avvio non migra da solo.

### Backup

`pg_dump` e la copia del volume del database contengono, nello stesso archivio, i dati personali in chiaro e la chiave HMAC delle evidenze (`app.evidence_hmac_key`). Il backup va protetto come dato personale: accesso limitato, cifratura a riposo, fuori da git, dai ticket e dai log. Non si invia in chiaro e non si lascia sul disco di una macchina condivisa.

Gli account il cui hash è ancora la password uguale al nome del ruolo, e per i quali manca `IEOL_SEED_<RUOLO>_PASSWORD`, vengono bloccati all'avvio: l'hash è sostituito con un segreto casuale che non viene stampato. Il login con il nome del ruolo fallisce. Reimpostare la variabile a un avvio successivo non sblocca quell'account, perché l'hash non coincide più con il nome del ruolo. Un ruolo assente dal database, con variabile vuota, non viene creato e quindi non ha accesso.

La CI (`.github/workflows/ci.yml`) usa solo valori di test scritti nel workflow. Quei valori non sono segreti di produzione e non vanno copiati in esercizio.
