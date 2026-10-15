"""Decisions log, declared provenance, checksums, and the ieol_app role."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.cli import main
from services.agent.baseline import BASELINE_CONFIDENCE_RULE
from services.agent.order_agent import AGENT_VERSION, build_context, run_order_agent
from services.agent.stub_model import StubOrderModel
from services.canonical.store import _source_class
from services.db import SOURCE_RANK, app_connect, assert_demo_database, connect, database_url, reset_password
from services.errors import AppError
from services.ingest.landing import MAX_LANDING_ROWS
from services.mapping.engine import THRESHOLD
from services.migrate import demo_database_url, ensure_database, migrate, migration_checksum, require_migrated
from services.rules.engine import CFO_THRESHOLD, RULES_VERSION
from services.sources import SOURCE_CLASSES, register_source
from tests.test_flow import decide, load_demo, login

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_decision_log_uses_the_database_and_rejects_a_stale_hash(client: TestClient):
    load_demo()
    login(client, "approver")
    item = client.get("/api/inbox").json()["recommendations"][0]
    assert item["confidence_method"] == "rule"
    assert item["confidence_rule"] == BASELINE_CONFIDENCE_RULE
    assert item["blocked_reason"] is None
    assert item["confidence"] == 0.86
    extra = client.post(
        f"/api/recommendations/{item['id']}/approve",
        json={
            "decision": "approve",
            "evidence_hash": item["evidence_hash"],
            "confidence": 0.1,
            "recommendation": "testo inviato dal client",
            "actor": "intruso",
        },
    )
    assert extra.status_code == 422
    stale = client.post(
        f"/api/recommendations/{item['id']}/approve",
        json={"decision": "approve", "evidence_hash": "0" * 64},
    )
    assert stale.status_code == 409
    assert "evidenze" in stale.json()["detail"]
    with connect() as conn:
        count = conn.execute("SELECT count(*) AS n FROM app.decisions_log").fetchone()["n"]
        status = conn.execute(
            "SELECT status FROM app.recommendations WHERE id = %s",
            (item["id"],),
        ).fetchone()["status"]
    assert count == 0
    assert status == "proposed"

    approved = decide(client, item)
    assert approved.status_code == 200, approved.text
    with connect() as conn:
        logged = conn.execute(
            """
            SELECT recommendation, confidence::text AS confidence, confidence_method, confidence_rule,
                   actor, actor_role, evidence_hash, agent_version, rules_version,
                   confidence_threshold::text AS confidence_threshold,
                   amount_threshold_eur::text AS amount_threshold_eur,
                   batch_id::text AS batch_id, row_index, decided_at
            FROM app.decisions_log
            """
        ).fetchone()
        stored = conn.execute(
            """
            SELECT problem, confidence::text AS confidence, evidence_hash
            FROM app.recommendations WHERE id = %s
            """,
            (item["id"],),
        ).fetchone()
        landing = conn.execute(
            """
            SELECT lr.batch_id::text AS batch_id, lr.row_index
            FROM app.landing_rows lr
            JOIN app.landing_batches lb ON lb.id = lr.batch_id
            WHERE lb.source_name = 'ordini' AND lr.data ->> 'ord_head' = '1024'
            """
        ).fetchone()
        sources = conn.execute(
            """
            SELECT lb.source_name, ls.row_index
            FROM app.decisions_log_sources ls
            JOIN app.landing_batches lb ON lb.id = ls.batch_id
            JOIN app.decisions_log d ON d.id = ls.decision_id
            """
        ).fetchall()
    assert logged["recommendation"] == stored["problem"]
    assert logged["confidence"] == stored["confidence"]
    assert logged["confidence_method"] == "rule"
    assert logged["confidence_rule"] == BASELINE_CONFIDENCE_RULE
    assert {row["source_name"] for row in sources} == {"ordini", "righe"}
    assert all(row["row_index"] == 0 for row in sources)
    assert logged["evidence_hash"] == stored["evidence_hash"]
    assert logged["actor"] == "approver"
    assert logged["actor_role"] == "approver"
    assert logged["decided_at"] is not None
    assert logged["agent_version"] == AGENT_VERSION
    assert logged["rules_version"] == RULES_VERSION
    assert float(logged["confidence_threshold"]) == THRESHOLD == 0.75
    assert int(float(logged["amount_threshold_eur"])) == 50000
    assert CFO_THRESHOLD == 50_000
    assert logged["batch_id"] == landing["batch_id"]
    assert logged["row_index"] == landing["row_index"]
    with connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO app.decisions_log (
                  id, recommendation_id, batch_id, row_index, decision, evidence_hash,
                  recommendation, confidence, confidence_method, actor, actor_role,
                  agent_version, rules_version, confidence_threshold, amount_threshold_eur
                )
                SELECT gen_random_uuid(), id, %s, %s, 'approve', evidence_hash,
                       problem, NULL, 'rule', 'x', 'approver', 'v', 'v', 0.75, 50000
                FROM app.recommendations
                LIMIT 1
                """,
                (landing["batch_id"], landing["row_index"]),
            )
            conn.commit()
        conn.rollback()


def test_unknown_provenance_cannot_be_approved(client: TestClient):
    load_demo()
    login(client, "approver")
    item = client.get("/api/inbox").json()["recommendations"][0]
    fact_id = next(fact["fact_id"] for fact in item["evidence"] if fact.get("fact_id"))
    with connect() as conn:
        conn.execute(
            "UPDATE app.facts SET source_class = 'unknown' WHERE id = %s",
            (fact_id,),
        )
        conn.commit()
    blocked = client.get("/api/inbox").json()["recommendations"][0]
    assert blocked["status"] == "proposed"
    assert blocked["blocked_reason"] == "provenienza sconosciuta"
    denied = decide(client, item)
    assert denied.status_code == 422
    assert denied.json()["detail"] == "Approvazione rifiutata: provenienza sconosciuta"
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.decisions_log").fetchone()["n"] == 0
        status = conn.execute(
            "SELECT status FROM app.recommendations WHERE id = %s",
            (item["id"],),
        ).fetchone()["status"]
    assert status == "proposed"


def test_source_class_is_declared_and_not_inferred_from_the_name(client: TestClient):
    login(client, "mapper")
    denied = client.post(
        "/api/ingest/csv",
        files={"file": ("erp_export.csv", b"ANAG_CLI,RAGIONE_SOCIALE\nE1,Nome\n", "text/csv")},
        data={"source_name": "erp_export", "source_class": "erp"},
    )
    assert denied.status_code == 422
    loaded = client.post(
        "/api/ingest/csv",
        files={"file": ("erp_export.csv", b"ANAG_CLI,RAGIONE_SOCIALE,PIVA\nE1,Nome Erp,IT00000000001\n", "text/csv")},
        data={"source_name": "erp_export", "source_class": "external"},
    )
    assert loaded.status_code == 200, loaded.text
    assert client.post("/api/mappings/confirm-confident").status_code == 200
    assert client.post("/api/materialize").status_code == 200
    with connect() as conn:
        registered = conn.execute(
            "SELECT source_class FROM app.sources WHERE name = 'erp_export'"
        ).fetchone()
        fact = conn.execute(
            """
            SELECT source_class FROM app.facts
            WHERE property = 'company_name' AND value = '"Nome Erp"'::jsonb AND valid_to IS NULL
            """
        ).fetchone()
        assert _source_class(conn, "erp_clienti", "original") == "unknown"
        assert _source_class(conn, "crm_contacts", "original") == "unknown"
        assert _source_class(conn, "manual_adjustment", "original") == "unknown"
        register_source(conn, "erp_clienti", "operational")
        assert _source_class(conn, "erp_clienti", "original") == "operational"
        assert _source_class(conn, "erp_clienti", "calculated") == "operational"
        assert _source_class(conn, "erp_clienti", "inferred") == "unknown"
        assert set(SOURCE_RANK) == set(SOURCE_CLASSES)
        with pytest.raises(AppError, match="altra provenienza"):
            register_source(conn, "erp_clienti", "manual")
        conn.rollback()
    assert registered["source_class"] == "external"
    assert fact["source_class"] == "external"


def test_migration_checksum_mismatch_stops_migrate():
    with connect() as conn:
        recorded = conn.execute(
            "SELECT version, checksum FROM public.schema_migrations ORDER BY version"
        ).fetchall()
        files = list((ROOT / "db" / "migrations").glob("*.sql"))
        by_version = {path.name.split("_", 1)[0]: path for path in files}
        assert [row["version"] for row in recorded] == ["0001", "0002", "0003", "0004"]
        for row in recorded:
            assert row["checksum"] == migration_checksum(by_version[row["version"]])
        conn.execute(
            "UPDATE public.schema_migrations SET checksum = NULL WHERE version = '0002'"
        )
        conn.commit()
        assert migrate(conn) == []
        restored = conn.execute(
            "SELECT checksum FROM public.schema_migrations WHERE version = '0002'"
        ).fetchone()["checksum"]
        assert restored == migration_checksum(by_version["0002"])
        conn.execute(
            "UPDATE public.schema_migrations SET checksum = 'deadbeef' WHERE version = '0001'"
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="cambiata"):
            migrate(conn)
        conn.rollback()
        with pytest.raises(RuntimeError, match="cambiata"):
            require_migrated(conn)


def test_reset_password_uses_the_role_env_and_argon2id(client: TestClient):
    role = "approver"
    env_name = f"IEOL_SEED_{role.upper()}_PASSWORD"
    previous = os.environ[env_name]
    updated = "reset-password-value-42"
    os.environ[env_name] = updated
    try:
        main(["reset-password", role])
        denied = client.post("/api/login", json={"username": role, "password": previous})
        assert denied.status_code == 401
        allowed = client.post("/api/login", json={"username": role, "password": updated})
        assert allowed.status_code == 200, allowed.text
        with connect() as conn:
            stored = conn.execute(
                "SELECT password_hash FROM app.users WHERE username = %s",
                (role,),
            ).fetchone()["password_hash"]
        assert stored.startswith("$argon2id$")
    finally:
        os.environ[env_name] = previous
        with connect() as conn:
            reset_password(conn, role)
    missing = os.environ.pop(env_name)
    try:
        with pytest.raises(SystemExit, match="assente"):
            main(["reset-password", role])
    finally:
        os.environ[env_name] = missing


def test_ieol_app_cannot_update_or_truncate_audit_or_decisions(client: TestClient):
    load_demo()
    login(client, "approver")
    item = client.get("/api/inbox").json()["recommendations"][0]
    modified = decide(client, item, "modify", quantity=12)
    assert modified.status_code == 200, modified.text
    approved = decide(client, item)
    assert approved.status_code == 200, approved.text
    statements = (
        "UPDATE app.audit_log SET actor = 'x'",
        "TRUNCATE app.audit_log",
        "UPDATE app.decisions_log SET actor = 'x'",
        "TRUNCATE app.decisions_log",
        "UPDATE app.agent_overrides SET actor = 'x'",
        "TRUNCATE app.agent_overrides",
    )
    for statement in statements:
        with app_connect() as conn:
            conn.execute("RESET ROLE")
            who = conn.execute(
                "SELECT session_user AS session_user, current_user AS current_user"
            ).fetchone()
            assert who["session_user"] == "ieol_app"
            assert who["current_user"] == "ieol_app"
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()
    with app_connect() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE app.decisions_log_sources SET row_index = 1")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("TRUNCATE app.decisions_log_sources")
        conn.rollback()
    with connect() as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute("DELETE FROM app.decisions_log")
            conn.commit()
        conn.rollback()
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute("TRUNCATE app.agent_overrides")
            conn.commit()
        conn.rollback()
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute("DELETE FROM app.decisions_log_sources")
            conn.commit()
        conn.rollback()


def test_new_tables_grant_ieol_app_by_default():
    with connect() as conn:
        conn.execute("CREATE TABLE app.privilege_probe (id integer PRIMARY KEY)")
        conn.commit()
    with app_connect() as conn:
        conn.execute("RESET ROLE")
        conn.execute("INSERT INTO app.privilege_probe (id) VALUES (1)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("TRUNCATE app.privilege_probe")
        conn.rollback()
    with connect() as conn:
        conn.execute("DROP TABLE app.privilege_probe")
        conn.commit()
    with app_connect() as conn:
        assert conn.execute("SELECT current_user AS name").fetchone()["name"] == "ieol_app"
        conn.rollback()


def test_login_includes_demo_mode_for_the_fixture_button(client: TestClient):
    response = client.post(
        "/api/login",
        json={"username": "admin", "password": os.environ["IEOL_SEED_ADMIN_PASSWORD"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["demo_mode"] is True
    assert response.json()["role"] == "admin"


def test_demo_load_is_admin_only_on_ieol_demo(client: TestClient):
    for role in ("mapper", "operator", "approver"):
        login(client, role)
        denied = client.post("/api/demo/load")
        assert denied.status_code == 403
        assert "admin" in denied.json()["detail"]
    login(client, "admin")
    denied = client.post("/api/demo/load")
    assert denied.status_code == 409
    assert denied.json()["detail"] == "Il demo si carica solo sul database ieol_demo"
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.recommendations").fetchone()["n"] == 0
        with pytest.raises(AppError, match="ieol_demo"):
            assert_demo_database(conn)
    demo_url = demo_database_url(database_url())
    ensure_database(demo_url)
    with connect(demo_url) as conn:
        assert conn.execute("SELECT current_database() AS name").fetchone()["name"] == "ieol_demo"
        assert_demo_database(conn)


def test_upload_row_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    login(client, "mapper")
    monkeypatch.setattr("services.ingest.landing.MAX_LANDING_ROWS", 1)
    assert MAX_LANDING_ROWS == 5000
    denied = client.post(
        "/api/ingest/csv",
        files={"file": ("righe.csv", b"A,B\n1,2\n3,4\n", "text/csv")},
        data={"source_name": "troppe", "source_class": "operational"},
    )
    assert denied.status_code == 413
    assert "righe" in denied.json()["detail"]


def test_agent_does_not_read_or_learn_from_the_decisions_log():
    source = (ROOT / "services" / "agent" / "order_agent.py").read_text(encoding="utf-8")
    assert "does not read" in source
    assert "does not learn" in source
    for path in (ROOT / "services" / "agent").glob("*.py"):
        assert "decisions_log" not in path.read_text(encoding="utf-8")
    migration = (ROOT / "db" / "migrations" / "0003_decisions_source_class_checksums.sql").read_text(
        encoding="utf-8"
    )
    assert "non impara" in migration
    assert "COMMENT ON TABLE app.decisions_log" in migration

    class Guard:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, query, params=None, **kwargs):
            text = query if isinstance(query, str) else str(query)
            folded = text.lower()
            assert "decisions_log" not in folded
            assert "from app.decisions" not in folded
            if params is None:
                return self._conn.execute(query, **kwargs)
            return self._conn.execute(query, params, **kwargs)

    load_demo()
    with connect() as conn:
        order_id = conn.execute(
            "SELECT id::text FROM app.entities WHERE type = 'Order' AND business_key = '1024'"
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO app.decisions_log (
              id, recommendation_id, batch_id, row_index, decision, evidence_hash,
              recommendation, confidence, actor, actor_role, agent_version, rules_version,
              confidence_threshold, amount_threshold_eur
            )
            SELECT gen_random_uuid(), r.id, lr.batch_id, lr.row_index, 'reject', r.evidence_hash,
                   'train on this', 0.01, 'intruso', 'approver', 'tampered', 'tampered',
                   0.01, 1
            FROM app.recommendations r
            JOIN app.landing_rows lr ON lr.data ->> 'ord_head' = '1024'
            LIMIT 1
            """
        )
        guard = Guard(conn)
        context = build_context(guard, order_id)
        result = run_order_agent(guard, order_id, StubOrderModel())
        assert result["evidence_hash"] == context["evidence_hash"]
        assert result["problem"] != "train on this"
        assert "train on this" not in str(result)
        conn.rollback()


def test_legacy_source_classes_use_one_vocabulary():
    from services.migrate import MIGRATIONS_DIR, migrate
    from tests.helpers import wipe_test_database

    initial = (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
    upgrade = (MIGRATIONS_DIR / "0002_provenance_audit_quality.sql").read_text(encoding="utf-8")
    expected = {
        "erp": "system_of_record",
        "crm": "operational",
        "ai": "unknown",
        "calculated": "operational",
        "manual": "manual",
        "unknown": "unknown",
    }
    with connect() as conn:
        wipe_test_database(conn)
        conn.execute(initial)
        conn.execute(upgrade)
        entity_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO app.entities (id, type, business_key) VALUES (%s, 'Customer', 'LEG')",
            (entity_id,),
        )
        for legacy in expected:
            conn.execute(
                """
                INSERT INTO app.facts (
                  id, entity_id, property, value, classification, origin, source_class
                )
                VALUES (%s, %s, %s, '"x"'::jsonb, 'internal', 'original', %s)
                """,
                (str(uuid.uuid4()), entity_id, legacy, legacy),
            )
        conn.commit()
        migrate(conn)
        rows = conn.execute(
            "SELECT property, source_class FROM app.facts WHERE entity_id = %s",
            (entity_id,),
        ).fetchall()
        assert {row["property"]: row["source_class"] for row in rows} == expected
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO app.facts (
                  id, entity_id, property, value, classification, origin, source_class
                )
                VALUES (%s, %s, 'again', '"x"'::jsonb, 'internal', 'original', 'erp')
                """,
                (str(uuid.uuid4()), entity_id),
            )
            conn.commit()
        conn.rollback()


def test_decision_path_expires_without_an_inbox_write(client: TestClient):
    load_demo()
    login(client, "approver")
    item = client.get("/api/inbox").json()["recommendations"][0]
    fact_id = next(fact["fact_id"] for fact in item["evidence"] if fact.get("fact_id"))
    with connect() as conn:
        conn.execute(
            "UPDATE app.facts SET value = '\"stale\"'::jsonb WHERE id = %s",
            (fact_id,),
        )
        conn.commit()
    assert client.get("/api/inbox").json()["recommendations"][0]["status"] == "proposed"
    denied = decide(client, item)
    assert denied.status_code == 409
    assert client.get("/api/inbox").json()["recommendations"][0]["status"] == "expired"


def test_reject_requires_a_reason(client: TestClient):
    load_demo()
    login(client, "approver")
    item = client.get("/api/inbox").json()["recommendations"][0]
    short = client.post(
        f"/api/recommendations/{item['id']}/reject",
        json={"decision": "reject", "evidence_hash": item["evidence_hash"], "reason": "no"},
    )
    assert short.status_code == 422
    missing = client.post(
        f"/api/recommendations/{item['id']}/reject",
        json={"decision": "reject", "evidence_hash": item["evidence_hash"]},
    )
    assert missing.status_code == 422
    kept = client.post(
        f"/api/recommendations/{item['id']}/reject",
        json={
            "decision": "reject",
            "evidence_hash": item["evidence_hash"],
            "reason": "non procede",
        },
    )
    assert kept.status_code == 200, kept.text
    script = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
    catch = script.split("catch (err)", 1)[1]
    assert "err.status === 409" in catch
    assert "loadInbox()" in catch
    assert "data-reason" in script
    card = (ROOT / "apps" / "web" / "escape.js").read_text(encoding="utf-8")
    assert 'data-reason required minlength="5"' in card


def test_inbox_get_does_not_expire_and_the_cli_does(client: TestClient):
    load_demo()
    login(client, "approver")
    item = client.get("/api/inbox").json()["recommendations"][0]
    fact_id = next(fact["fact_id"] for fact in item["evidence"] if fact.get("fact_id"))
    with connect() as conn:
        before = conn.execute(
            """
            SELECT count(*) AS n FROM app.workflow_events
            WHERE recommendation_id = %s AND to_status = 'expired'
            """,
            (item["id"],),
        ).fetchone()["n"]
        conn.execute(
            "UPDATE app.facts SET value = '\"changed-for-expiry\"'::jsonb WHERE id = %s",
            (fact_id,),
        )
        conn.commit()
    again = client.get("/api/inbox").json()["recommendations"][0]
    assert again["id"] == item["id"]
    assert again["status"] == "proposed"
    with connect() as conn:
        after = conn.execute(
            """
            SELECT count(*) AS n FROM app.workflow_events
            WHERE recommendation_id = %s AND to_status = 'expired'
            """,
            (item["id"],),
        ).fetchone()["n"]
    assert after == before
    main(["expire-stale"])
    refreshed = client.get("/api/inbox").json()["recommendations"][0]
    assert refreshed["status"] == "expired"
