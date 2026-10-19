"""Company assignment: one write, demo by flag, no upload without a company."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.cli import main
from services.companies import NO_COMPANY_UPLOAD
from services.db import app_connect, connect, password_hash
from services.mapping.corrections import active_correction, file_type_metric
from services.migrate import (
    _ensure_registry,
    _execute_migration,
    _record,
    migrate,
    migration_files,
)
from services.testset import _human_mapping_cases
from tests.helpers import wipe_test_database
from tests.test_flow import login
from tests.test_learning import _batch, _decision, _file_decision, _mapping, _user

ROOT = Path(__file__).resolve().parents[1]
NOTICE = "Il tuo utente non è ancora collegato a un'azienda: chiedi all'amministratore"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_0011_adds_the_flag_and_does_not_rewrite_rows():
    script = (ROOT / "db" / "migrations" / "0011_company_assignment.sql").read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in script.splitlines())
    assert "UPDATE app." not in code
    assert "UPDATE src." not in code
    company_id = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    audit_id = str(uuid.uuid4())
    with connect() as conn:
        wipe_test_database(conn)
        _ensure_registry(conn)
        for item in migration_files():
            if item.version == "0011":
                continue
            _execute_migration(conn, item)
            _record(conn, item, baselined=False)
        conn.execute(
            "INSERT INTO app.companies (id, name) VALUES (%s, 'Storica')",
            (company_id,),
        )
        conn.execute(
            """
            INSERT INTO app.sources (name, source_class, company_id)
            VALUES ('archivio', 'operational', NULL)
            """
        )
        conn.execute(
            """
            INSERT INTO app.landing_batches (id, source_name, watermark, row_count, company_id)
            VALUES (%s, 'archivio', 'wm-vecchio', 3, NULL)
            """,
            (batch_id,),
        )
        conn.execute(
            """
            INSERT INTO app.users (username, password_hash, role, company_id)
            VALUES ('storico', 'not-a-password', 'mapper', NULL)
            """
        )
        conn.execute(
            """
            INSERT INTO app.audit_log (id, actor, role, action, entity_ref, payload)
            VALUES (%s, 'ada', 'mapper', 'proposal', 'order:kept', '{"kept": true}'::jsonb)
            """,
            (audit_id,),
        )
        before_company = conn.execute(
            "SELECT id::text AS id, name FROM app.companies"
        ).fetchall()
        before_source = conn.execute(
            "SELECT name, source_class, company_id::text AS company_id FROM app.sources"
        ).fetchall()
        before_batch = conn.execute(
            """
            SELECT id::text AS id, source_name, watermark, row_count, company_id::text AS company_id
            FROM app.landing_batches
            """
        ).fetchall()
        before_user = conn.execute(
            "SELECT username, role, company_id::text AS company_id FROM app.users WHERE username = 'storico'"
        ).fetchall()
        conn.commit()
        applied = migrate(conn)
        company = conn.execute(
            "SELECT id::text AS id, name, is_demo FROM app.companies"
        ).fetchall()
        source = conn.execute(
            "SELECT name, source_class, company_id::text AS company_id FROM app.sources"
        ).fetchall()
        batch = conn.execute(
            """
            SELECT id::text AS id, source_name, watermark, row_count, company_id::text AS company_id
            FROM app.landing_batches
            """
        ).fetchall()
        user = conn.execute(
            "SELECT username, role, company_id::text AS company_id FROM app.users WHERE username = 'storico'"
        ).fetchall()
        audit = conn.execute(
            "SELECT id::text AS id, action, payload::text AS payload FROM app.audit_log"
        ).fetchall()
    assert applied == [{"version": "0011", "name": "company_assignment", "baselined": False}]
    assert before_company == [{"id": company_id, "name": "Storica"}]
    assert company == [{"id": company_id, "name": "Storica", "is_demo": False}]
    assert source == before_source
    assert source[0]["company_id"] is None
    assert batch == before_batch
    assert batch[0]["company_id"] is None
    assert user == before_user
    assert user[0]["company_id"] is None
    assert audit == [{"id": audit_id, "action": "proposal", "payload": '{"kept": true}'}]


def test_cli_creates_assigns_and_writes_audit_once():
    created = _cli_json(["company", "create", "--name", "Officina Nord"])
    assert created["is_demo"] is False
    assert created["name"] == "Officina Nord"
    named_demo = _cli_json(["company", "create", "--name", "Demo"])
    assert named_demo["is_demo"] is False
    demo = _cli_json(["company", "create-demo", "--name", "Officina"])
    assert demo["is_demo"] is True
    assert demo["name"] == "Officina"
    assigned = _cli_json(
        ["company", "assign", "--username", "mapper", "--company-id", created["company_id"]]
    )
    assert assigned["username"] == "mapper"
    assert assigned["company_id"] == created["company_id"]
    assert assigned["is_demo"] is False
    with pytest.raises(SystemExit, match="una sola volta"):
        main(["company", "assign", "--username", "mapper", "--company-id", demo["company_id"]])
    with pytest.raises(SystemExit, match="Nome azienda non valido"):
        main(["company", "create", "--name", "   "])
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT action, entity_ref, payload
            FROM app.audit_log
            WHERE action IN ('company_create', 'user_company_assign')
            ORDER BY at, action
            """
        ).fetchall()
        stored = {
            row["id"]: row["is_demo"]
            for row in conn.execute(
                "SELECT id::text AS id, is_demo, name FROM app.companies"
            ).fetchall()
        }
        user_company = conn.execute(
            "SELECT company_id::text AS company_id FROM app.users WHERE username = 'mapper'"
        ).fetchone()["company_id"]
    assert user_company == created["company_id"]
    assert stored[created["company_id"]] is False
    assert stored[named_demo["company_id"]] is False
    assert stored[demo["company_id"]] is True
    actions = [row["action"] for row in rows]
    assert actions.count("company_create") == 3
    assert actions.count("user_company_assign") == 1
    assign_row = next(row for row in rows if row["action"] == "user_company_assign")
    assert assign_row["payload"]["username"] == "mapper"
    assert assign_row["payload"]["company_id"] == created["company_id"]
    assert assign_row["entity_ref"] == "user:mapper"
    create_flags = {row["payload"]["company_id"]: row["payload"]["is_demo"] for row in rows if row["action"] == "company_create"}
    assert create_flags[demo["company_id"]] is True
    assert create_flags[named_demo["company_id"]] is False


def test_database_triggers_block_a_second_company_and_a_late_batch_company():
    company_a = _cli_json(["company", "create", "--name", "Alfa"])["company_id"]
    company_b = _cli_json(["company", "create", "--name", "Beta"])["company_id"]
    _cli_json(["company", "assign", "--username", "mapper", "--company-id", company_a])
    with connect() as conn:
        _user(conn, "libera", None)
        batch_id = _batch(conn, None, "vecchia", "wm", loaded_at="2020-01-01T00:00:00Z")
        owned = _batch(conn, company_a, "nuova", "wm", loaded_at="2024-01-01T00:00:00Z")
        conn.execute(
            """
            INSERT INTO app.sources (name, source_class, company_id)
            VALUES ('vecchia', 'operational', NULL)
            """
        )
        conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="una sola volta"):
            conn.execute(
                "UPDATE app.users SET company_id = %s WHERE username = 'mapper'",
                (company_b,),
            )
            conn.commit()
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="una sola volta"):
            conn.execute("UPDATE app.users SET company_id = NULL WHERE username = 'mapper'")
            conn.commit()
        conn.rollback()
        conn.execute(
            "UPDATE app.users SET company_id = %s WHERE username = 'libera'",
            (company_b,),
        )
        conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="batch non si aggiunge"):
            conn.execute(
                "UPDATE app.landing_batches SET company_id = %s WHERE id = %s",
                (company_a, batch_id),
            )
            conn.commit()
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="batch non si aggiunge"):
            conn.execute(
                "UPDATE app.landing_batches SET company_id = %s WHERE id = %s",
                (company_b, owned),
            )
            conn.commit()
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="batch non si aggiunge"):
            conn.execute(
                "UPDATE app.landing_batches SET company_id = NULL WHERE id = %s",
                (owned,),
            )
            conn.commit()
        conn.rollback()
        conn.execute(
            "UPDATE app.landing_batches SET row_count = 4 WHERE id = %s",
            (owned,),
        )
        conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="fonte non si aggiunge"):
            conn.execute(
                "UPDATE app.sources SET company_id = %s WHERE name = 'vecchia' AND company_id IS NULL",
                (company_a,),
            )
            conn.commit()
        conn.rollback()
        left = conn.execute(
            """
            SELECT company_id::text AS company_id, row_count
            FROM app.landing_batches WHERE id = %s
            """,
            (batch_id,),
        ).fetchone()
        owned_row = conn.execute(
            "SELECT company_id::text AS company_id, row_count FROM app.landing_batches WHERE id = %s",
            (owned,),
        ).fetchone()
        legacy = conn.execute(
            "SELECT company_id::text AS company_id FROM app.sources WHERE name = 'vecchia'"
        ).fetchone()
    assert left["company_id"] is None
    assert owned_row == {"company_id": company_a, "row_count": 4}
    assert legacy["company_id"] is None


def test_database_refuses_to_change_the_demo_flag():
    plain = _cli_json(["company", "create", "--name", "Fissa"])["company_id"]
    demo = _cli_json(["company", "create-demo", "--name", "Vetrina"])["company_id"]
    with connect() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="flag demo"):
            conn.execute("UPDATE app.companies SET is_demo = true WHERE id = %s", (plain,))
            conn.commit()
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="flag demo"):
            conn.execute("UPDATE app.companies SET is_demo = false WHERE id = %s", (demo,))
            conn.commit()
        conn.rollback()
        conn.execute("UPDATE app.companies SET is_demo = false WHERE id = %s", (plain,))
        conn.execute("UPDATE app.companies SET name = 'Fissa rinominata' WHERE id = %s", (plain,))
        conn.execute("UPDATE app.companies SET is_demo = true WHERE id = %s", (demo,))
        conn.commit()
        flags = {
            row["id"]: {"name": row["name"], "is_demo": row["is_demo"]}
            for row in conn.execute(
                "SELECT id::text AS id, name, is_demo FROM app.companies"
            ).fetchall()
        }
    assert flags[plain] == {"name": "Fissa rinominata", "is_demo": False}
    assert flags[demo]["is_demo"] is True
    assert flags[demo]["name"] == "Vetrina"


def test_user_without_a_company_cannot_write_a_decision(client: TestClient):
    login(client, "mapper")
    viewed = client.get("/api/mappings")
    assert viewed.status_code == 200
    assert viewed.json()["mappings"] == []
    assert client.get("/api/orders").status_code == 200
    missing = str(uuid.uuid4())
    writes = [
        (f"/api/mappings/{missing}/confirm", None),
        (f"/api/mappings/{missing}/reject", None),
        (f"/api/mapping-decisions/{missing}/revoke", {}),
        (f"/api/files/{missing}/type", {"decision": "correct"}),
        ("/api/mappings/confirm-confident", None),
        ("/api/materialize", None),
        ("/api/agent/run", None),
    ]
    for path, body in writes:
        denied = client.post(path) if body is None else client.post(path, json=body)
        assert denied.status_code == 403, path
        assert denied.json()["detail"] == NO_COMPANY_UPLOAD
    login(client, "operator")
    operator = client.post("/api/mappings/confirm-confident")
    assert operator.status_code == 403
    assert operator.json()["detail"] == NOTICE
    assert client.get("/api/inbox").status_code == 200
    company_id = _cli_json(["company", "create", "--name", "Decisioni"])["company_id"]
    _cli_json(["company", "assign", "--username", "mapper", "--company-id", company_id])
    _cli_json(["company", "assign", "--username", "operator", "--company-id", company_id])
    login(client, "mapper")
    opened = client.post(f"/api/mappings/{missing}/confirm")
    assert opened.status_code == 404
    assert opened.json()["detail"] != NOTICE
    rejected = client.post(f"/api/mappings/{missing}/reject")
    assert rejected.status_code == 404
    revoked = client.post(f"/api/mapping-decisions/{missing}/revoke", json={})
    assert revoked.status_code == 404
    typed = client.post(f"/api/files/{missing}/type", json={"decision": "correct"})
    assert typed.status_code == 404
    bulk = client.post("/api/mappings/confirm-confident")
    assert bulk.status_code == 200
    assert bulk.json()["confirmed"] == 0
    rebuilt = client.post("/api/materialize")
    assert rebuilt.status_code == 200, rebuilt.text
    ran = client.post("/api/agent/run")
    assert ran.status_code == 200, ran.text
    login(client, "operator")
    still_blocked = client.post("/api/materialize")
    assert still_blocked.status_code == 403
    assert still_blocked.json()["detail"] == "Serve il ruolo mapper"
    page = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
    assert 'querySelector("#confirm-high").hidden = !canWrite' in page
    assert 'querySelector("#materialize").hidden = !canWrite' in page
    assert "mappingRow(item, canWriteDecisions(state.hasCompany))" in page
    assert "canWriteDecisions(state.hasCompany)" in page
    assert "innerHTML" not in page.split("companyNotice")[1].split("show(")[0]
    rendered = subprocess.run(
        ["node", "-e", _DECISION_BUTTONS],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"


def test_app_role_cannot_create_a_company_or_assign_a_user_and_there_is_no_api(client: TestClient):
    company_id = _cli_json(["company", "create", "--name", "Chiusa"])["company_id"]
    with app_connect() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO app.companies (id, name, is_demo) VALUES (%s, 'no', false)",
                (str(uuid.uuid4()),),
            )
            conn.commit()
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE app.users SET company_id = %s WHERE username = 'mapper'",
                (company_id,),
            )
            conn.commit()
        conn.rollback()
    login(client, "admin")
    for path in ("/api/companies", "/api/company", "/api/users/mapper/company"):
        assert client.post(path, json={"name": "x"}).status_code == 404
        assert client.get(path).status_code == 404
    assert not any("company" in getattr(route, "path", "") for route in app.routes)


def test_upload_without_a_company_is_rejected_and_the_page_hides_the_button(client: TestClient):
    login(client, "mapper")
    me = client.get("/api/me").json()
    assert me["has_company"] is False
    assert "company_id" not in me
    csv_body = (ROOT / "fixtures" / "ordini.csv").read_bytes()
    denied = client.post(
        "/api/ingest/csv",
        files={"file": ("ordini.csv", csv_body, "text/csv")},
        data={"source_name": "ordini", "source_class": "external", "company_id": str(uuid.uuid4())},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == NO_COMPANY_UPLOAD
    assert denied.json()["detail"] == NOTICE
    excel = client.post(
        "/api/ingest/excel",
        files={"file": ("ordini.xlsx", b"not-a-workbook", "application/vnd.ms-excel")},
        data={"source_name": "ordini", "source_class": "external"},
    )
    assert excel.status_code == 403
    assert excel.json()["detail"] == NOTICE
    document = client.post(
        "/api/ingest/document",
        files={"file": ("ordine.txt", b"ordine", "text/plain")},
    )
    assert document.status_code == 403
    assert document.json()["detail"] == NOTICE
    sql = client.post(
        "/api/ingest/sql",
        json={"table": "src.ordini", "source_class": "system_of_record"},
    )
    assert sql.status_code == 403
    assert sql.json()["detail"] == NOTICE
    company_id = _cli_json(["company", "create", "--name", "Carica"])["company_id"]
    other_id = _cli_json(["company", "create", "--name", "Altra"])["company_id"]
    _cli_json(["company", "assign", "--username", "mapper", "--company-id", company_id])
    refreshed = client.get("/api/me").json()
    assert refreshed["has_company"] is True
    uploaded = client.post(
        "/api/ingest/csv",
        files={"file": ("ordini.csv", csv_body, "text/csv")},
        data={"source_name": "ordini", "source_class": "external", "company_id": other_id},
    )
    assert uploaded.status_code == 200, uploaded.text
    with connect() as conn:
        row = conn.execute(
            """
            SELECT company_id::text AS company_id
            FROM app.sources WHERE name = 'ordini'
            """
        ).fetchone()
        batch = conn.execute(
            "SELECT company_id::text AS company_id FROM app.landing_batches WHERE source_name = 'ordini'"
        ).fetchone()
    assert row["company_id"] == company_id
    assert batch["company_id"] == company_id
    page = (ROOT / "apps" / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
    assert 'id="upload-label"' in page
    assert 'id="company-notice"' in page
    assert "companyNotice.textContent" in script
    assert "companyNotice.innerHTML" not in script
    rendered = subprocess.run(
        ["node", "-e", _NOTICE_CHECK],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"


def test_first_upload_keeps_a_legacy_source_and_opens_one_per_company(client: TestClient):
    company_a = _cli_json(["company", "create", "--name", "Alfa"])["company_id"]
    company_b = _cli_json(["company", "create", "--name", "Beta"])["company_id"]
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.sources (name, source_class, company_id)
            VALUES ('condivisa', 'operational', NULL)
            """
        )
        _user(conn, "bea", None)
        conn.commit()
    _cli_json(["company", "assign", "--username", "mapper", "--company-id", company_a])
    _cli_json(["company", "assign", "--username", "bea", "--company-id", company_b])
    csv_body = (ROOT / "fixtures" / "ordini.csv").read_bytes()
    login(client, "mapper")
    first = client.post(
        "/api/ingest/csv",
        files={"file": ("ordini.csv", csv_body, "text/csv")},
        data={"source_name": "condivisa", "source_class": "external"},
    )
    assert first.status_code == 200, first.text
    again = client.post(
        "/api/ingest/csv",
        files={"file": ("ordini.csv", csv_body, "text/csv")},
        data={"source_name": "condivisa", "source_class": "external"},
    )
    assert again.status_code == 200, again.text
    client.post("/api/logout")
    logged = client.post("/api/login", json={"username": "bea", "password": "bea-secret"})
    assert logged.status_code == 200, logged.text
    second = client.post(
        "/api/ingest/csv",
        files={"file": ("ordini.csv", csv_body, "text/csv")},
        data={"source_name": "condivisa", "source_class": "system_of_record"},
    )
    assert second.status_code == 200, second.text
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT company_id::text AS company_id, source_class
            FROM app.sources
            WHERE name = 'condivisa'
            ORDER BY company_id NULLS FIRST
            """
        ).fetchall()
    by_company = {row["company_id"]: row["source_class"] for row in rows}
    assert by_company[None] == "operational"
    assert by_company[company_a] == "external"
    assert by_company[company_b] == "system_of_record"
    assert len(rows) == 3


def test_null_batches_are_out_of_first_time_and_out_of_suggestions():
    company_a = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            "INSERT INTO app.companies (id, name, is_demo) VALUES (%s, 'Alfa', false)",
            (company_a,),
        )
        _user(conn, "ada", company_a)
        null_batch = _batch(conn, None, "vecchia", "wm", loaded_at="2019-01-01T00:00:00Z")
        real_batch = _batch(conn, company_a, "vecchia", "wm-a", loaded_at="2024-06-01T00:00:00Z")
        null_mapping = _mapping(
            conn, company_id=None, source="vecchia", field="cod_art", batch_id=null_batch,
        )
        null_decision = _decision(
            conn, null_mapping, decision="confirm", company_id=None, actor="mapper",
            source_name="vecchia", source_field="cod_art", origin="rule", batch_id=null_batch,
        )
        loose = _mapping(conn, company_id=None, source="vecchia", field="qty")
        loose_decision = _decision(
            conn, loose, decision="confirm", company_id=None, actor="mapper",
            source_name="vecchia", source_field="qty", origin="rule",
        )
        real_mapping = _mapping(
            conn, company_id=company_a, source="vecchia", field="cod_art", batch_id=real_batch,
        )
        real_decision = _decision(
            conn, real_mapping, decision="confirm", company_id=company_a, actor="ada",
            source_name="vecchia", source_field="cod_art", origin="rule", batch_id=real_batch,
        )
        learned_host = _mapping(conn, company_id=None, source="impara", field="COD_ART")
        _decision(
            conn, learned_host, decision="reject", company_id=None, actor="mapper",
            source_name="impara", source_field="COD_ART", corrected="unit_price",
            batch_id=null_batch,
        )
        open_host = _mapping(conn, company_id=company_a, source="libera", field="COD_ART")
        opened = _decision(
            conn, open_host, decision="reject", company_id=company_a, actor="ada",
            source_name="libera", source_field="COD_ART", corrected="unit_price",
            batch_id=real_batch,
        )
        conn.commit()
        labels = {item["decision_id"] for item in _human_mapping_cases(conn)}
        assert real_decision in labels
        assert null_decision not in labels
        assert loose_decision not in labels
        assert active_correction(conn, company_id=company_a, source_name="impara", header="cod art") is None
        assert active_correction(conn, company_id=None, source_name="impara", header="cod art") is None
        found = active_correction(conn, company_id=company_a, source_name="libera", header="cod art")
        assert found is not None
        assert found["id"] == opened


def test_demo_exclusion_follows_the_flag_and_ignores_the_name():
    named_demo = str(uuid.uuid4())
    flagged = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.companies (id, name, is_demo)
            VALUES (%s, 'Demo', false), (%s, 'Produzione', true)
            """,
            (named_demo, flagged),
        )
        _user(conn, "vera", named_demo)
        _user(conn, "finta", flagged)
        real_batch = _batch(
            conn, named_demo, "alfa", "real", loaded_at="2024-01-01T00:00:00Z", proposed_type="order",
        )
        demo_batch = _batch(
            conn, flagged, "alfa", "demo", loaded_at="2020-01-01T00:00:00Z", proposed_type="customer",
        )
        real_mapping = _mapping(
            conn, company_id=named_demo, source="alfa", field="cod_art", batch_id=real_batch,
        )
        demo_mapping = _mapping(
            conn, company_id=flagged, source="alfa", field="cod_art", batch_id=demo_batch,
        )
        real_decision = _decision(
            conn, real_mapping, decision="confirm", company_id=named_demo, actor="vera",
            source_name="alfa", source_field="cod_art", origin="rule", batch_id=real_batch,
        )
        demo_decision = _decision(
            conn, demo_mapping, decision="confirm", company_id=flagged, actor="finta",
            source_name="alfa", source_field="cod_art", origin="rule", batch_id=demo_batch,
        )
        _file_decision(
            conn, real_batch, company_id=named_demo, actor="vera", decision="correct",
            proposed_type="order", decided_at="2024-02-01T00:00:00Z",
        )
        _file_decision(
            conn, demo_batch, company_id=flagged, actor="finta", decision="wrong",
            correct_type="product", proposed_type="customer", decided_at="2024-02-01T00:00:00Z",
        )
        conn.commit()
        before_labels = {item["decision_id"] for item in _human_mapping_cases(conn)}
        before_types = file_type_metric(conn)
        conn.execute("UPDATE app.companies SET name = 'Demo interna' WHERE id = %s", (flagged,))
        conn.execute("UPDATE app.companies SET name = 'Officina' WHERE id = %s", (named_demo,))
        conn.commit()
        after_labels = {item["decision_id"] for item in _human_mapping_cases(conn)}
        after_types = file_type_metric(conn)
        flags = {
            row["id"]: row["is_demo"]
            for row in conn.execute(
                "SELECT id::text AS id, name, is_demo FROM app.companies"
            ).fetchall()
        }
    assert real_decision in before_labels
    assert demo_decision not in before_labels
    assert before_labels == after_labels
    assert before_types["correct"] == 1
    assert before_types["wrong"] == 0
    assert before_types == after_types
    assert flags[flagged] is True
    assert flags[named_demo] is False


def _cli_json(argv: list[str]) -> dict:
    from contextlib import redirect_stdout
    from io import StringIO

    buffer = StringIO()
    with redirect_stdout(buffer):
        main(argv)
    return json.loads(buffer.getvalue().strip().splitlines()[-1])


_NOTICE_CHECK = r"""
const { companyUploadNotice } = require("./apps/web/escape.js");
const hostile = '<img src=x onerror="alert(1)">';
const text = companyUploadNotice(hostile);
if (text !== "Il tuo utente non è ancora collegato a un'azienda: chiedi all'amministratore") {
  console.error(text);
  process.exit(1);
}
if (text.includes("<") || text.includes(hostile)) process.exit(2);
console.log("ok");
"""

_DECISION_BUTTONS = r"""
const { canWriteDecisions, mappingRow } = require("./apps/web/escape.js");
if (canWriteDecisions(true) !== true) process.exit(1);
if (canWriteDecisions(false) !== false) process.exit(2);
if (canWriteDecisions(undefined) !== false) process.exit(3);
if (canWriteDecisions("true") !== false) process.exit(4);
const item = {
  id: "1",
  status: "proposed",
  origin: "learned",
  decision_id: "d1",
  source_name: "ordini",
  source_field: "cod",
  target_type: "Order",
  target_property: "number",
  origin_text: "Da una correzione",
  band: "forte",
  band_note: "nota",
};
const hidden = mappingRow(item, false);
if (hidden.includes("<button") || hidden.includes("Conferma") || hidden.includes("Rifiuta") || hidden.includes("Annulla")) {
  console.error(hidden);
  process.exit(5);
}
if (!hidden.includes("ordini") || !hidden.includes("cod")) process.exit(6);
const shown = mappingRow(item, true);
if (!shown.includes(">Conferma<") || !shown.includes(">Rifiuta<") || !shown.includes("Annulla correzione")) process.exit(7);
if (!mappingRow(item).includes(">Conferma<")) process.exit(8);
console.log("ok");
"""
