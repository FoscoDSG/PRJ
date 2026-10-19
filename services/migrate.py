"""Numbered SQL migrations.

The schema already lives in SQL files, so a registry table is enough.
Alembic would add a dependency, a config file, and an env module without
changing what gets applied.

``migrate`` never drops objects. A database created from the historical
``db/schema.sql`` is baselined: ``0001_initial`` is recorded and its SQL
is not executed. Later files still run.

The API does not call ``migrate``. It only asks whether versions are
pending, and it refuses to start when a stored checksum does not match
the file on disk. ``pg_advisory_xact_lock`` with ``MIGRATE_LOCK_KEY``
keeps two overlapping ``migrate`` calls from applying the same version.
"""

from __future__ import annotations

import hashlib
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

# Fixed key for pg_advisory_xact_lock. One value, shared by every process.
MIGRATE_LOCK_KEY = 0x49454F4C4D494752
DEMO_DATABASE_DEFAULT = "ieol_demo"
_RESERVED_DATABASES = frozenset({"postgres", "template0", "template1"})
_DBNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_MIGRATION_FILE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
# Deny by default. Any of these DROP forms is refused, at any position,
# in any case, after SQL comments are removed. A TRUNCATE statement is
# refused too. BEFORE TRUNCATE and REVOKE ... TRUNCATE stay: they do not
# empty a relation.
_DROP = re.compile(
    r"(?i)\bDROP\s+(TABLE|SCHEMA|DATABASE|COLUMN|CONSTRAINT|NOT\s+NULL|DEFAULT|INDEX|FUNCTION|TRIGGER)\b"
)
_TRUNCATE = re.compile(r"(?i)\bTRUNCATE\b")
_BEFORE_TRUNCATE = re.compile(r"(?i)\bBEFORE\s+TRUNCATE\b")
_REVOKE_LINE = re.compile(r"(?i)^\s*REVOKE\b")
_DISABLE_TRIGGER = re.compile(r"(?i)\bDISABLE\s+TRIGGER\b")
_ENABLE_TRIGGER = re.compile(r"(?i)\bENABLE\s+TRIGGER\b")
_ALTER_TABLE_DISABLE = re.compile(r"(?i)\bALTER\s+TABLE\b[^;]*\bDISABLE\b")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# Exact statements, whitespace-normalized and without a trailing semicolon,
# allowed only in the named migration file. The same text in a new file
# is refused. 0005's trigger lines are the only disable/enable exception,
# and only inside shield_existing_pii.
_MIGRATION_ALLOWLIST: dict[str, frozenset[str]] = {
    "0003_decisions_source_class_checksums.sql": frozenset({
        "ALTER TABLE app.facts DROP CONSTRAINT IF EXISTS facts_source_class_check",
    }),
    "0004_source_vocabulary_decision_sources.sql": frozenset({
        "ALTER TABLE app.facts DROP CONSTRAINT IF EXISTS facts_source_class_check",
        "ALTER COLUMN confidence DROP NOT NULL",
    }),
    "0005_pii_testset_audit.sql": frozenset({
        "ALTER TABLE app.audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check",
        "ALTER TABLE app.audit_log DISABLE TRIGGER audit_no_update",
        "ALTER TABLE app.audit_log ENABLE TRIGGER audit_no_update",
        "ALTER TABLE app.decisions_log DISABLE TRIGGER decisions_log_no_update",
        "ALTER TABLE app.decisions_log ENABLE TRIGGER decisions_log_no_update",
    }),
    "0006_hash_version_v2.sql": frozenset({
        "ALTER TABLE app.decisions_log DROP CONSTRAINT IF EXISTS decisions_log_hash_version_check",
    }),
    "0007_mapping_decisions_key_audit.sql": frozenset({
        "ALTER TABLE app.audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check",
        "ALTER TABLE app.labelled_cases DROP CONSTRAINT IF EXISTS labelled_cases_origin_check",
    }),
    "0008_mapping_decision_snapshot.sql": frozenset({
        "ALTER TABLE app.mapping_decisions DROP CONSTRAINT IF EXISTS mapping_decisions_decision_check",
        "ALTER TABLE app.mapping_decisions DROP CONSTRAINT IF EXISTS mapping_decisions_confidence_method_check",
    }),
    "0010_companies_learned_corrections.sql": frozenset({
        "ALTER TABLE app.mapping_decisions DROP CONSTRAINT IF EXISTS mapping_decisions_decision_check",
        "ALTER TABLE app.sources DROP CONSTRAINT IF EXISTS sources_pkey",
        "ALTER TABLE app.landing_batches DROP CONSTRAINT IF EXISTS landing_batches_source_name_watermark_key",
        "ALTER TABLE app.ingest_watermarks DROP CONSTRAINT IF EXISTS ingest_watermarks_pkey",
    }),
    "0011_company_assignment.sql": frozenset({
        "ALTER TABLE app.audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check",
    }),
}

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


def pending_migrations(conn: psycopg.Connection) -> list[dict]:
    """Return versions not yet recorded. Reads only: no lock, no writes."""
    files = migration_files()
    done = _recorded_versions(conn)
    state = _state_for(files, done, conn)
    pending = []
    for item, action in _select_pending(files, done, state, conn):
        pending.append(_describe(item, baselined=action == "baseline"))
    return pending


def require_migrated(conn: psycopg.Connection) -> None:
    """Refuse when versions are pending or a stored checksum does not match.

    Does not apply migrations and does not fill a missing checksum.
    """
    pending = pending_migrations(conn)
    if pending:
        versions = ", ".join(item["version"] for item in pending)
        name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        raise RuntimeError(
            f"Migrazioni in sospeso ({versions}) sul database {name}. "
            "Avvio rifiutato. Esegui `ieol migrate`."
        )
    _assert_recorded_checksums(conn)


def migration_violation(script: str, filename: str | None = None) -> str | None:
    """Return why a migration script is refused, or None when it may run.

    Every DROP of a table, schema, database, column, constraint, NOT NULL,
    default, index, function, or trigger is refused, wherever it sits in
    the script and in any letter case. Comments are ignored. TRUNCATE
    statements are refused too, as are DISABLE TRIGGER, ENABLE TRIGGER,
    and ALTER TABLE ... DISABLE. The only exceptions are the exact
    statements listed for that migration file.
    """
    text = _without_line_comments(_BLOCK_COMMENT.sub("", script))
    allowed = _MIGRATION_ALLOWLIST.get(filename or "", frozenset())
    kept = [line for line in text.splitlines() if _normalize_sql(line) not in allowed]
    body = "\n".join(kept)
    if _DROP.search(body):
        return "DROP fuori dall'allowlist"
    scrubbed = _BEFORE_TRUNCATE.sub(" ", body)
    without_revoke = "\n".join(
        line for line in scrubbed.splitlines() if not _REVOKE_LINE.match(line)
    )
    if _TRUNCATE.search(without_revoke):
        return "TRUNCATE fuori dall'allowlist"
    if (
        _DISABLE_TRIGGER.search(body)
        or _ENABLE_TRIGGER.search(body)
        or _ALTER_TABLE_DISABLE.search(body)
    ):
        return "DISABLE TRIGGER fuori dall'allowlist"
    return None


def _normalize_sql(line: str) -> str:
    return " ".join(line.split()).rstrip(";").strip()


def migrate(conn: psycopg.Connection, *, rotate_evidence_key: bool = False) -> list[dict]:
    """Apply pending migrations and commit. Never drops tables or truncates.

    Takes ``pg_advisory_xact_lock(MIGRATE_LOCK_KEY)`` for the whole
    transaction, so a second caller waits and then sees the versions
    already recorded. Returns the versions recorded by this call.

    A different ``IEOL_EVIDENCE_HMAC_KEY`` does not replace the stored
    row unless ``rotate_evidence_key`` is true. The first insert still runs.
    """
    applied: list[dict] = []
    try:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATE_LOCK_KEY,))
        _ensure_registry(conn)
        files = migration_files()
        _verify_checksums(conn, files)
        done = _applied_versions(conn)
        state = _state_for(files, done, conn)
        for item, action in _select_pending(files, done, state, conn):
            if action == "partial":
                raise RuntimeError(
                    "Schema esistente incompleto rispetto a db/schema.sql: "
                    "migrazione iniziale non applicata e nessun dato cancellato."
                )
            if action == "baseline":
                _record(conn, item, baselined=True)
                applied.append(_describe(item, baselined=True))
                continue
            _execute_migration(conn, item)
            _record(conn, item, baselined=False)
            applied.append(_describe(item, baselined=False))
        from services.evidence import install_evidence_key

        install_evidence_key(conn, rotate=rotate_evidence_key)
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


def migration_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ensure_registry(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public.schema_migrations (
          version text PRIMARY KEY,
          name text NOT NULL,
          baselined boolean NOT NULL DEFAULT false,
          applied_at timestamptz NOT NULL DEFAULT now(),
          checksum text
        )
        """
    )
    conn.execute("ALTER TABLE public.schema_migrations ADD COLUMN IF NOT EXISTS checksum text")


def _verify_checksums(conn: psycopg.Connection, files: list[Migration]) -> None:
    """Fail when an applied file changed. A missing checksum is recorded once."""
    if not _checksum_column_ready(conn):
        return
    by_version = {item.version: item for item in files}
    recorded = conn.execute(
        "SELECT version, checksum FROM public.schema_migrations ORDER BY version"
    ).fetchall()
    for row in recorded:
        item = by_version.get(row["version"])
        if item is None:
            raise RuntimeError(
                f"Migrazione applicata {row['version']} assente dal disco. ieol migrate si ferma."
            )
        current = migration_checksum(item.path)
        stored = row["checksum"]
        if stored is None:
            conn.execute(
                """
                UPDATE public.schema_migrations
                SET checksum = %s
                WHERE version = %s AND checksum IS NULL
                """,
                (current, row["version"]),
            )
            continue
        if stored != current:
            raise RuntimeError(
                f"La migrazione {item.path.name} è cambiata dopo l'applicazione. "
                "ieol migrate si ferma."
            )


def _assert_recorded_checksums(conn: psycopg.Connection) -> None:
    """Read-only check used at startup. Does not fill a missing checksum."""
    if not _checksum_column_ready(conn):
        raise RuntimeError("Checksum di migrazione assente. Esegui `ieol migrate`.")
    files = {item.version: item for item in migration_files()}
    recorded = conn.execute(
        "SELECT version, checksum FROM public.schema_migrations ORDER BY version"
    ).fetchall()
    for row in recorded:
        item = files.get(row["version"])
        if item is None:
            raise RuntimeError(
                f"Migrazione applicata {row['version']} assente dal disco. Esegui `ieol migrate`."
            )
        if row["checksum"] is None:
            raise RuntimeError("Checksum di migrazione assente. Esegui `ieol migrate`.")
        if row["checksum"] != migration_checksum(item.path):
            raise RuntimeError(
                f"La migrazione {item.path.name} è cambiata dopo l'applicazione. "
                "Avvio rifiutato. Esegui `ieol migrate`."
            )


def _checksum_column_ready(conn: psycopg.Connection) -> bool:
    row = conn.execute("SELECT to_regclass('public.schema_migrations') AS rel").fetchone()
    if row is None or row["rel"] is None:
        return False
    column = conn.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'schema_migrations'
          AND column_name = 'checksum'
        """
    ).fetchone()
    return column is not None


def _recorded_versions(conn: psycopg.Connection) -> set[str]:
    row = conn.execute("SELECT to_regclass('public.schema_migrations') AS rel").fetchone()
    if row is None or row["rel"] is None:
        return set()
    return _applied_versions(conn)


def _applied_versions(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("SELECT version FROM public.schema_migrations").fetchall()
    return {row["version"] for row in rows}


def _state_for(files: list[Migration], done: set[str], conn: psycopg.Connection) -> str:
    if files[0].version in done:
        return "absent"
    return legacy_schema_state(conn)


def _select_pending(
    files: list[Migration],
    done: set[str],
    state: str,
    conn: psycopg.Connection,
) -> list[tuple[Migration, str]]:
    first = files[0].version
    chosen: list[tuple[Migration, str]] = []
    for item in files:
        if item.version in done:
            continue
        if item.version == first and state == "complete":
            chosen.append((item, "baseline"))
        elif item.version == first and state == "partial":
            chosen.append((item, "partial"))
        elif item.name == "provenance_audit_quality" and _provenance_complete(conn):
            chosen.append((item, "baseline"))
        else:
            chosen.append((item, "apply"))
    return chosen


_PROVENANCE_COLUMNS: dict[str, frozenset[str]] = {
    "app.identity_reviews": frozenset({
        "id", "entity_type", "left_key", "right_key", "score", "reason", "status", "created_at",
    }),
    "app.agent_overrides": frozenset({
        "id", "recommendation_id", "field_name", "previous_value", "new_value", "actor", "created_at",
    }),
    "app.quality_issues": frozenset({
        "id", "entity_type", "business_key", "check_name", "detail", "status", "created_at",
    }),
    "app.price_list": frozenset({"product_source_key", "price"}),
    "app.quarantine_documents": frozenset({
        "id", "source_name", "order_key", "reasons", "status", "created_at",
    }),
}


def _provenance_complete(conn: psycopg.Connection) -> bool:
    """True when 0002's objects are already present, so its SQL must not run."""
    rows = conn.execute(
        """
        SELECT table_schema || '.' || table_name AS relation, column_name
        FROM information_schema.columns
        WHERE table_schema = 'app'
        """
    ).fetchall()
    found: dict[str, set[str]] = {}
    for row in rows:
        found.setdefault(row["relation"], set()).add(row["column_name"])
    for relation, columns in _PROVENANCE_COLUMNS.items():
        if not columns.issubset(found.get(relation, set())):
            return False
    facts = found.get("app.facts", set())
    if not {"source_class", "confidence_method", "confidence_rule"}.issubset(facts):
        return False
    columns = conn.execute(
        """
        SELECT column_name, column_default, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'app'
          AND table_name = 'facts'
          AND column_name IN ('source_class', 'confidence', 'confidence_method', 'confidence_rule')
        """
    ).fetchall()
    by_name = {row["column_name"]: row for row in columns}
    expected = {
        "source_class": ("NO", "unknown"),
        "confidence": ("YES", None),
        "confidence_method": ("NO", "unknown"),
        "confidence_rule": ("YES", None),
    }
    for name, (nullable, default) in expected.items():
        column = by_name.get(name)
        if column is None or column["is_nullable"] != nullable:
            return False
        if _quoted_default(column["column_default"]) != default:
            return False
    definitions = [
        row["definition"]
        for row in conn.execute(
            """
            SELECT pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE conrelid = 'app.facts'::regclass AND contype = 'c'
            """
        ).fetchall()
    ]
    if not _constraint_covers(definitions, {"erp", "crm", "manual", "ai", "calculated", "unknown"}):
        return False
    if not _constraint_covers(definitions, {"rule", "human", "unknown"}):
        return False
    audit = conn.execute("SELECT to_regclass('app.audit_log') AS rel").fetchone()
    if audit is None or audit["rel"] is None:
        return False
    trigger = conn.execute(
        """
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'audit_no_truncate'
          AND tgrelid = 'app.audit_log'::regclass
          AND NOT tgisinternal
        """
    ).fetchone()
    if trigger is None:
        return False
    roles = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname IN ('ieol_owner', 'ieol_app')"
    ).fetchall()
    return {row["rolname"] for row in roles} == {"ieol_owner", "ieol_app"}


def _quoted_default(expr: str | None) -> str | None:
    if expr is None:
        return None
    if expr.startswith("'") and "'::" in expr:
        return expr.split("'::", 1)[0][1:]
    return expr


def _constraint_covers(definitions: list[str], values: set[str]) -> bool:
    needed = {f"'{value}'" for value in values}
    return any(needed.issubset(set(_quoted_tokens(definition))) for definition in definitions)


def _quoted_tokens(definition: str) -> list[str]:
    return re.findall(r"'(?:[^']|'')*'", definition)


def _record(conn: psycopg.Connection, item: Migration, *, baselined: bool) -> None:
    conn.execute(
        """
        INSERT INTO public.schema_migrations (version, name, baselined, checksum)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (version) DO NOTHING
        """,
        (item.version, item.name, baselined, migration_checksum(item.path)),
    )


def _describe(item: Migration, *, baselined: bool) -> dict:
    return {"version": item.version, "name": item.name, "baselined": baselined}


def _execute_migration(conn: psycopg.Connection, item: Migration) -> None:
    script = item.path.read_text(encoding="utf-8")
    violation = migration_violation(script, item.path.name)
    if violation is not None:
        raise RuntimeError(
            f"La migrazione {item.path.name} contiene {violation} e non viene applicata"
        )
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
