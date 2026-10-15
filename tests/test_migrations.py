"""Migrations adopt a legacy database, and the demo stays off the main one."""

from __future__ import annotations

import json
import uuid

import pytest

from apps.cli import main
from ontology.loader import load_merged
from services.db import connect, database_url, ensure_ready
from services.migrate import (
    LEGACY_SCHEMA_PATH,
    MIGRATIONS_DIR,
    demo_database_url,
    legacy_schema_state,
    migrate,
    migration_files,
    reset_demo_database,
)
from tests.helpers import wipe_test_database


def _rows(conn, query: str, params: tuple | None = None) -> list[tuple]:
    cursor = conn.execute(query, params) if params is not None else conn.execute(query)
    return [tuple(row.values()) for row in cursor.fetchall()]


def _relation_oids(conn) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT n.nspname || '.' || c.relname AS name, c.oid::text AS oid
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('app', 'src') AND c.relkind IN ('r', 'i', 'S')
        ORDER BY 1
        """
    ).fetchall()
    return {row["name"]: row["oid"] for row in rows}


def _audit(conn) -> list[tuple]:
    return _rows(
        conn,
        """
        SELECT id::text, at, actor, role, action, entity_ref, payload::text
        FROM app.audit_log
        ORDER BY id
        """,
    )


def _catalog(conn) -> dict[str, list[tuple]]:
    return {
        "columns": _rows(
            conn,
            """
            SELECT n.nspname, c.relname, a.attname, t.typname, a.attnotnull
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_type t ON t.oid = a.atttypid
            WHERE n.nspname IN ('app', 'src')
              AND c.relkind = 'r'
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY 1, 2, a.attnum
            """,
        ),
        "constraints": _rows(
            conn,
            """
            SELECT n.nspname, c.relname, con.conname, con.contype::text, pg_get_constraintdef(con.oid)
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname IN ('app', 'src')
            ORDER BY 1, 2, 3
            """,
        ),
        "indexes": _rows(
            conn,
            """
            SELECT schemaname, indexname, indexdef
            FROM pg_indexes
            WHERE schemaname IN ('app', 'src')
            ORDER BY 1, 2
            """,
        ),
        "triggers": _rows(
            conn,
            """
            SELECT tgname, pg_get_triggerdef(oid)
            FROM pg_trigger
            WHERE NOT tgisinternal AND tgrelid = 'app.audit_log'::regclass
            ORDER BY 1
            """,
        ),
        "functions": _rows(
            conn,
            """
            SELECT p.proname, pg_get_functiondef(p.oid)
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'app'
            ORDER BY 1
            """,
        ),
        "sequences": _rows(
            conn,
            """
            SELECT n.nspname, c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname IN ('app', 'src') AND c.relkind = 'S'
            ORDER BY 1, 2
            """,
        ),
    }


def _install_legacy(conn) -> None:
    wipe_test_database(conn)
    conn.execute(LEGACY_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def _seed_legacy_rows(conn) -> dict[str, str]:
    audit_id = str(uuid.uuid4())
    entity_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.users (username, password_hash, role)
        VALUES ('historian', 'not-a-password', 'mapper')
        """
    )
    conn.execute(
        """
        INSERT INTO app.entities (id, type, business_key)
        VALUES (%s, 'Customer', 'CUST-KEPT')
        """,
        (entity_id,),
    )
    conn.execute(
        """
        INSERT INTO src.clienti (anag_cli, ragione_sociale, piva, email)
        VALUES ('C-KEPT', 'Acme', 'IT123', 'kept@example.com')
        """
    )
    conn.execute(
        """
        INSERT INTO app.audit_log (id, actor, role, action, entity_ref, payload)
        VALUES (%s, 'ada', 'mapper', 'proposal', 'order:kept', '{"kept":"audit"}'::jsonb)
        """,
        (audit_id,),
    )
    conn.commit()
    return {"audit_id": audit_id, "entity_id": entity_id}


def test_migration_files_do_not_drop_or_truncate():
    files = migration_files()
    assert [item.version for item in files] == ["0001"]
    assert files[0].name == "initial"
    for item in files:
        script = item.path.read_text(encoding="utf-8").lower()
        assert "drop " not in script
        assert "truncate" not in script
    assert MIGRATIONS_DIR.joinpath("0001_initial.sql").is_file()


def test_initial_migration_reproduces_the_legacy_catalog():
    with connect() as conn:
        _install_legacy(conn)
        legacy = _catalog(conn)
        assert legacy_schema_state(conn) == "complete"
        wipe_test_database(conn)
        applied = migrate(conn)
        created = _catalog(conn)
    assert applied == [{"version": "0001", "name": "initial", "baselined": False}]
    assert created == legacy


def test_legacy_database_is_baselined_without_touching_data_or_audit():
    with connect() as conn:
        _install_legacy(conn)
        ids = _seed_legacy_rows(conn)
        before_oids = _relation_oids(conn)
        before_audit = _audit(conn)
        before_user = _rows(
            conn,
            "SELECT username, password_hash, role FROM app.users WHERE username = 'historian'",
        )
        before_entity = _rows(
            conn,
            "SELECT id::text, type, business_key, valid_from, valid_to FROM app.entities",
        )
        before_client = _rows(conn, "SELECT anag_cli, ragione_sociale, piva, email FROM src.clienti")
        applied = migrate(conn)
        again = migrate(conn)
        after_oids = _relation_oids(conn)
        after_audit = _audit(conn)
        registry = _rows(
            conn,
            "SELECT version, name, baselined FROM public.schema_migrations ORDER BY version",
        )
        applied_at = _rows(conn, "SELECT applied_at FROM public.schema_migrations WHERE version = '0001'")
        third = migrate(conn)
        assert _rows(conn, "SELECT applied_at FROM public.schema_migrations WHERE version = '0001'") == applied_at
        ensure_ready(conn, load_merged().packages)
        with pytest.raises(Exception, match="append-only"):
            conn.execute("UPDATE app.audit_log SET actor = 'tamper' WHERE id = %s", (ids["audit_id"],))
            conn.commit()
        conn.rollback()
        assert _relation_oids(conn) == before_oids
        assert _audit(conn) == before_audit
        assert _rows(
            conn,
            "SELECT username, password_hash, role FROM app.users WHERE username = 'historian'",
        ) == before_user
        assert _rows(
            conn,
            "SELECT id::text, type, business_key, valid_from, valid_to FROM app.entities",
        ) == before_entity
        assert _rows(conn, "SELECT anag_cli, ragione_sociale, piva, email FROM src.clienti") == before_client
    assert applied == [{"version": "0001", "name": "initial", "baselined": True}]
    assert again == []
    assert third == []
    assert after_oids == before_oids
    assert after_audit == before_audit
    assert len(before_audit) == 1
    assert before_audit[0][0] == ids["audit_id"]
    assert before_audit[0][6] == '{"kept": "audit"}'
    assert registry == [("0001", "initial", True)]


def test_migrate_twice_on_an_empty_database_keeps_inserted_rows():
    with connect() as conn:
        wipe_test_database(conn)
        first = migrate(conn)
        audit_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO app.audit_log (id, actor, role, action, entity_ref, payload)
            VALUES (%s, 'bea', 'approver', 'outcome', 'order:twice', '{"pass":2}'::jsonb)
            """,
            (audit_id,),
        )
        conn.execute(
            """
            INSERT INTO app.users (username, password_hash, role)
            VALUES ('twice', 'not-a-password', 'operator')
            """
        )
        conn.commit()
        before_oids = _relation_oids(conn)
        before_audit = _audit(conn)
        before_registry = _rows(
            conn,
            "SELECT version, name, baselined, applied_at FROM public.schema_migrations",
        )
        second = migrate(conn)
        third = migrate(conn)
        assert _relation_oids(conn) == before_oids
        assert _audit(conn) == before_audit
        assert _rows(
            conn,
            "SELECT version, name, baselined, applied_at FROM public.schema_migrations",
        ) == before_registry
        assert _rows(conn, "SELECT username, role FROM app.users WHERE username = 'twice'") == [("twice", "operator")]
    assert first == [{"version": "0001", "name": "initial", "baselined": False}]
    assert second == []
    assert third == []


def test_initial_sql_executed_twice_keeps_audit_rows():
    script = (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
    with connect() as conn:
        wipe_test_database(conn)
        conn.execute(script)
        audit_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO app.audit_log (id, actor, role, action, entity_ref, payload)
            VALUES (%s, 'cy', 'mapper', 'dry_run', 'order:sql', '{"sql":true}'::jsonb)
            """,
            (audit_id,),
        )
        conn.commit()
        before_oids = _relation_oids(conn)
        before_audit = _audit(conn)
        conn.execute(script)
        conn.commit()
        assert _relation_oids(conn) == before_oids
        assert _audit(conn) == before_audit
        with pytest.raises(Exception, match="append-only"):
            conn.execute("DELETE FROM app.audit_log WHERE id = %s", (audit_id,))
            conn.commit()
        conn.rollback()


def test_partial_schema_is_left_unchanged():
    with connect() as conn:
        wipe_test_database(conn)
        conn.execute("CREATE SCHEMA app")
        conn.execute(
            """
            CREATE TABLE app.users (
              username text PRIMARY KEY,
              password_hash text NOT NULL,
              role text NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO app.users (username, password_hash, role) VALUES ('partial', 'not-a-password', 'admin')"
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="incompleto"):
            migrate(conn)
        assert legacy_schema_state(conn) == "partial"
        assert conn.execute("SELECT to_regclass('app.audit_log') AS rel").fetchone()["rel"] is None
        assert conn.execute("SELECT to_regclass('public.schema_migrations') AS rel").fetchone()["rel"] is None
        kept = _rows(conn, "SELECT username, password_hash, role FROM app.users")
    assert kept == [("partial", "not-a-password", "admin")]


def test_cli_migrate_is_repeatable(capsys: pytest.CaptureFixture[str]):
    with connect() as conn:
        wipe_test_database(conn)
    main(["migrate"])
    first = json.loads(capsys.readouterr().out)
    main(["migrate"])
    second = json.loads(capsys.readouterr().out)
    assert first["applied"] == [{"version": "0001", "name": "initial", "baselined": False}]
    assert second["applied"] == []


def test_demo_database_must_differ_from_the_main_one(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("IEOL_DEMO_DATABASE", "ieol_test")
    with pytest.raises(RuntimeError, match="diverso"):
        demo_database_url(database_url())
    with pytest.raises(SystemExit, match="diverso"):
        main(["demo"])
    with connect() as conn:
        assert conn.execute("SELECT current_database() AS name").fetchone()["name"] == "ieol_test"
        assert conn.execute("SELECT to_regclass('app.users') AS rel").fetchone()["rel"] is not None


def test_demo_reset_refuses_the_main_database():
    with connect() as conn:
        name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        before_oids = _relation_oids(conn)
        before_users = conn.execute("SELECT count(*) AS n FROM app.users").fetchone()["n"]
        with pytest.raises(RuntimeError, match="rifiutato"):
            reset_demo_database(conn, main_dbname=name, expected_dbname="ieol_test_demo")
        assert conn.execute("SELECT count(*) AS n FROM app.users").fetchone()["n"] == before_users
        assert _relation_oids(conn) == before_oids


def test_demo_reset_does_not_touch_the_main_database(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("IEOL_DEMO_DATABASE", "ieol_test_demo")
    audit_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.audit_log (id, actor, role, action, entity_ref, payload)
            VALUES (%s, 'keeper', 'mapper', 'proposal', 'main', '{"scope":"main"}'::jsonb)
            """,
            (audit_id,),
        )
        conn.execute(
            """
            INSERT INTO src.clienti (anag_cli, ragione_sociale, piva, email)
            VALUES ('MAIN', 'Main Co', 'IT9', 'main@example.com')
            """
        )
        conn.commit()
        before_oids = _relation_oids(conn)
        before_audit = _audit(conn)
        main_name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
    main(["demo"])
    first_out = json.loads(capsys.readouterr().out)
    assert first_out["shortage_order"]
    with connect() as conn:
        assert _relation_oids(conn) == before_oids
        assert _audit(conn) == before_audit
        assert _rows(conn, "SELECT anag_cli FROM src.clienti WHERE anag_cli = 'MAIN'") == [("MAIN",)]
    main(["demo", "--reset"])
    second_out = json.loads(capsys.readouterr().out)
    assert second_out["shortage_order"]
    with connect() as conn:
        assert conn.execute("SELECT current_database() AS name").fetchone()["name"] == main_name
        assert _relation_oids(conn) == before_oids
        assert _audit(conn) == before_audit
        assert _rows(conn, "SELECT anag_cli FROM src.clienti WHERE anag_cli = 'MAIN'") == [("MAIN",)]
    demo_url = demo_database_url(database_url())
    with connect(demo_url) as conn:
        assert conn.execute("SELECT current_database() AS name").fetchone()["name"] == "ieol_test_demo"
        assert conn.execute("SELECT count(*) AS n FROM app.recommendations").fetchone()["n"] >= 1
        leaked = conn.execute("SELECT 1 FROM app.audit_log WHERE id = %s", (audit_id,)).fetchone()
        assert leaked is None
        assert _rows(
            conn,
            "SELECT version, baselined FROM public.schema_migrations WHERE version = '0001'",
        ) == [("0001", False)]
