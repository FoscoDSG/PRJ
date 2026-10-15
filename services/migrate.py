"""Numbered SQL migrations.

The schema already lives in SQL files, so a registry table is enough.
Alembic would add a dependency, a config file, and an env module without
changing what gets applied.

``migrate`` never drops objects. A database created from the historical
``db/schema.sql`` is baselined: ``0001_initial`` is recorded and its SQL
is not executed. Later files still run.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "db" / "migrations"
LEGACY_SCHEMA_PATH = ROOT / "db" / "schema.sql"

DEMO_DATABASE_DEFAULT = "ieol_demo"
_RESERVED_DATABASES = frozenset({"postgres", "template0", "template1"})
_DBNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_MIGRATION_FILE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_DESTRUCTIVE = re.compile(r"\b(DROP|TRUNCATE)\b", re.IGNORECASE)

# Columns created by db/schema.sql. Extra columns still count as the legacy
# schema; missing ones mean the database is partial and must not be rewritten.
LEGACY_COLUMNS: dict[str, frozenset[str]] = {
    "app.users": frozenset({"username", "password_hash", "role"}),
    "app.audit_log": frozenset({"id", "at", "actor", "role", "action", "entity_ref", "payload"}),
    "app.ontology_versions": frozenset({"id", "package", "version", "changelog", "loaded_at"}),
    "app.schema_snapshots": frozenset({"id", "source_name", "column_hash", "columns", "captured_at"}),
    "app.field_mappings": frozenset({
        "id", "source_name", "source_field", "target_type", "target_property",
        "confidence", "status", "samples", "profile", "valid_from", "valid_to",
    }),
    "app.drift_reviews": frozenset({
        "id", "source_name", "previous_hash", "new_hash", "diff", "status", "created_at",
    }),
    "app.landing_batches": frozenset({"id", "source_name", "watermark", "row_count", "loaded_at"}),
    "app.landing_rows": frozenset({"id", "batch_id", "row_index", "data"}),
    "app.ingest_watermarks": frozenset({"source_name", "watermark", "updated_at"}),
    "app.entities": frozenset({"id", "type", "business_key", "valid_from", "valid_to"}),
    "app.identity_aliases": frozenset({"entity_type", "alias", "entity_id"}),
    "app.facts": frozenset({
        "id", "entity_id", "property", "value", "classification", "origin",
        "source", "confidence", "valid_from", "valid_to",
    }),
    "app.graph_edges": frozenset({
        "id", "from_id", "rel", "to_id", "origin", "valid_from", "valid_to",
    }),
    "app.recommendations": frozenset({
        "id", "order_entity_id", "problem", "evidence", "evidence_hash", "actions",
        "expected_impact", "risk", "confidence", "status", "requires_cfo", "created_at",
    }),
    "app.workflow_events": frozenset({
        "id", "recommendation_id", "from_status", "to_status", "actor", "at",
    }),
    "app.action_batches": frozenset({
        "id", "recommendation_id", "kind", "status", "payloads", "created_at",
    }),
    "app.sandbox_documents": frozenset({
        "id", "kind", "idempotency_key", "payload", "status", "compensation", "created_at",
    }),
    "app.email_outbox": frozenset({
        "id", "idempotency_key", "recipient", "subject", "body", "created_at",
    }),
    "src.clienti": frozenset({"anag_cli", "ragione_sociale", "piva", "email"}),
    "src.ordini": frozenset({"ord_head", "anag_cli", "importo", "stato", "data"}),
    "src.righe": frozenset({"ord_head", "cod_art", "qty", "prezzo"}),
    "src.articoli": frozenset({"cod_art", "descrizione", "costo"}),
    "src.giacenze": frozenset({"cod_art", "magazzino", "giacenza", "impegnato"}),
    "src.fornitori": frozenset({"forn", "nome", "cod_art", "ritardo_giorni"}),
}


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path


def migration_files() -> list[Migration]:
    if not MIGRATIONS_DIR.is_dir():
        raise RuntimeError(f"Directory migrazioni assente: {MIGRATIONS_DIR}")
    found: list[Migration] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_FILE.fullmatch(path.name)
        if match is None:
            raise RuntimeError(f"Nome migrazione non valido: {path.name}")
        version = match.group(1)
        name = path.stem.split("_", 1)[1]
        found.append(Migration(version, name, path))
    if not found:
        raise RuntimeError("Nessuna migrazione in db/migrations")
    versions = [item.version for item in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Versioni di migrazione duplicate")
    found.sort(key=lambda item: int(item.version))
    return found


def legacy_schema_state(conn: psycopg.Connection) -> str:
    """Return ``complete``, ``partial``, or ``absent`` for the historical schema."""
    present = False
    missing = False
    for relation in LEGACY_COLUMNS:
        row = conn.execute("SELECT to_regclass(%s) AS rel", (relation,)).fetchone()
        if row is None or row["rel"] is None:
            missing = True
        else:
            present = True
    if not present:
        return "absent"
    if missing or not _legacy_details_match(conn):
        return "partial"
    return "complete"


def migrate(conn: psycopg.Connection) -> list[dict]:
    """Apply pending migrations and commit. Never drops or truncates.

    Returns the versions recorded by this call. An empty list means the
    database was already current.
    """
    applied: list[dict] = []
    try:
        _ensure_registry(conn)
        done = _applied_versions(conn)
        files = migration_files()
        first = files[0].version
        state = legacy_schema_state(conn) if first not in done else "absent"
        for item in files:
            if item.version in done:
                continue
            if item.version == first and state == "complete":
                _record(conn, item, baselined=True)
                applied.append(_describe(item, baselined=True))
                continue
            if item.version == first and state == "partial":
                raise RuntimeError(
                    "Schema esistente incompleto rispetto a db/schema.sql: "
                    "migrazione iniziale non applicata e nessun dato cancellato."
                )
            _execute_migration(conn, item)
            _record(conn, item, baselined=False)
            applied.append(_describe(item, baselined=False))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return applied


def database_name(url: str) -> str:
    name = conninfo_to_dict(url).get("dbname")
    if not name:
        raise RuntimeError("URL del database senza nome")
    return name


def demo_database_url(main_url: str) -> str:
    """URL of the dedicated demo database. It never matches the main database."""
    explicit = os.environ.get("IEOL_DEMO_DATABASE_URL")
    if explicit is not None and explicit.strip() != "":
        url = explicit.strip()
    else:
        name = os.environ.get("IEOL_DEMO_DATABASE", DEMO_DATABASE_DEFAULT)
        if not _DBNAME.fullmatch(name or ""):
            raise RuntimeError("IEOL_DEMO_DATABASE non è un nome di database valido")
        info = conninfo_to_dict(main_url)
        if "dbname" not in info:
            raise RuntimeError("URL del database senza nome")
        info["dbname"] = name
        url = make_conninfo(**info)
    _require_separate_demo(database_name(url), database_name(main_url))
    return url


def ensure_database(url: str) -> None:
    """Create the database named by ``url`` when it is missing. Never drops one.

    An existing database is opened directly. ``CREATEDB`` and a connection to
    the ``postgres`` database are used only when that open fails because the
    database is absent.
    """
    info = conninfo_to_dict(url)
    dbname = info.get("dbname")
    if not dbname:
        raise RuntimeError("URL del database senza nome")
    if dbname in _RESERVED_DATABASES:
        raise RuntimeError("Creazione rifiutata per un database di sistema")
    try:
        with psycopg.connect(url) as conn:
            conn.execute("SELECT 1")
        return
    except psycopg.OperationalError as original:
        _create_database(info, dbname, original)


def _create_database(info: dict, dbname: str, original: BaseException) -> None:
    admin = dict(info)
    admin["dbname"] = "postgres"
    try:
        admin_conn = psycopg.connect(make_conninfo(**admin), autocommit=True)
    except psycopg.OperationalError:
        raise original
    with admin_conn:
        exists = admin_conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (dbname,),
        ).fetchone()
        if exists:
            raise original
        try:
            admin_conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
        except errors.DuplicateDatabase:
            return


def reset_demo_database(conn: psycopg.Connection, *, main_dbname: str, expected_dbname: str) -> None:
    """Drop ``app`` and ``src`` on the demo database only, then commit.

    The connection must already be on ``expected_dbname``, which must be
    distinct from the main database.
    """
    _require_separate_demo(expected_dbname, main_dbname)
    current = conn.execute("SELECT current_database() AS name").fetchone()
    current_name = None if current is None else current["name"]
    if current_name in _RESERVED_DATABASES or current_name == main_dbname or current_name != expected_dbname:
        raise RuntimeError(f"Reset della demo rifiutato sul database {current_name}")
    conn.execute("DROP SCHEMA IF EXISTS app CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS src CASCADE")
    conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    conn.commit()


def _require_separate_demo(demo_name: str, main_dbname: str) -> None:
    if demo_name == main_dbname:
        raise RuntimeError("Il database della demo deve essere diverso dal database principale")
    if demo_name in _RESERVED_DATABASES:
        raise RuntimeError("Il database della demo non può essere un database di sistema")


def _ensure_registry(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.schema_migrations (
          version text PRIMARY KEY,
          name text NOT NULL,
          baselined boolean NOT NULL DEFAULT false,
          applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def _applied_versions(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("SELECT version FROM public.schema_migrations").fetchall()
    return {row["version"] for row in rows}


def _record(conn: psycopg.Connection, item: Migration, *, baselined: bool) -> None:
    conn.execute(
        """
        INSERT INTO public.schema_migrations (version, name, baselined)
        VALUES (%s, %s, %s)
        ON CONFLICT (version) DO NOTHING
        """,
        (item.version, item.name, baselined),
    )


def _describe(item: Migration, *, baselined: bool) -> dict:
    return {"version": item.version, "name": item.name, "baselined": baselined}


def _execute_migration(conn: psycopg.Connection, item: Migration) -> None:
    script = item.path.read_text(encoding="utf-8")
    if _DESTRUCTIVE.search(_without_line_comments(script)):
        raise RuntimeError(f"La migrazione {item.path.name} contiene DROP o TRUNCATE e non viene applicata")
    conn.execute(script)


def _without_line_comments(script: str) -> str:
    kept = []
    for line in script.splitlines():
        if "--" in line:
            line = line.split("--", 1)[0]
        kept.append(line)
    return "\n".join(kept)


def _legacy_details_match(conn: psycopg.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT table_schema || '.' || table_name AS relation, column_name
        FROM information_schema.columns
        WHERE table_schema IN ('app', 'src')
        """
    ).fetchall()
    found: dict[str, set[str]] = {}
    for row in rows:
        found.setdefault(row["relation"], set()).add(row["column_name"])
    for relation, columns in LEGACY_COLUMNS.items():
        if not columns.issubset(found.get(relation, set())):
            return False
    trigger = conn.execute(
        """
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'audit_no_update'
          AND tgrelid = 'app.audit_log'::regclass
          AND NOT tgisinternal
        """
    ).fetchone()
    if trigger is None:
        return False
    function = conn.execute(
        """
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'app' AND p.proname = 'prevent_audit_mutation'
        """
    ).fetchone()
    if function is None:
        return False
    index = conn.execute(
        """
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'app' AND indexname = 'facts_open_idx'
        """
    ).fetchone()
    return index is not None
