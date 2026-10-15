# Configurazione

La visione di prodotto è in `README (1).md`. Qui ci sono solo i segreti necessari per avviare la V1.

## Sessione

`SESSION_SECRET` firma il cookie di sessione. Se manca, l'API rifiuta l'avvio.

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

Gli hash SHA-256 storici (`sha256("ieol:" + password)` con sale fisso) restano validi al login e, al primo accesso riuscito, vengono sostituiti con argon2id. Non serve un reset di massa, salvo il caso sopra: se la password è il nome del ruolo viene ritirata all'avvio, anche senza un login.

## Database

`DATABASE_URL` ha default `postgresql://ubuntu@/ieol`. I test usano `postgresql://ubuntu@/ieol_test` e richiedono un ruolo PostgreSQL `ubuntu` con accesso peer a quel database.

Vedi anche `.env.example`.
