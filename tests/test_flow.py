import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from apps.api.main import app
from ontology.loader import load_merged
from services.actions.executor import execute
from services.db import connect
from services.errors import AppError
from services.pipeline import load_fixtures, seed_src


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, role: str) -> None:
    password = os.environ[f"IEOL_SEED_{role.upper()}_PASSWORD"]
    response = client.post("/api/login", json={"username": role, "password": password})
    assert response.status_code == 200, response.text


def test_mapping_review_identity_and_agent_does_not_execute(client: TestClient):
    login(client, "mapper")
    loaded = client.post("/api/demo/load")
    assert loaded.status_code == 200, loaded.text
    mappings = client.get("/api/mappings").json()["mappings"]
    by_field = {(item["source_name"], item["source_field"]): item for item in mappings}
    assert by_field[("ordini", "ord_head")]["confidence"] >= 0.75
    assert by_field[("ordini", "ord_head")]["status"] == "confirmed"
    assert by_field[("articoli", "cod_art")]["target_type"] == "Product"
    assert by_field[("ambiguo", "doc_01")]["status"] == "proposed"
    assert by_field[("ambiguo", "doc_01")]["confidence"] < 0.75
    assert by_field[("anagrafica", "piva")]["samples"] == ["••••", "••••", "••••"]

    with connect() as conn:
        customers = conn.execute(
            "SELECT business_key FROM app.entities WHERE type = 'Customer'"
        ).fetchall()
        assert len(customers) == 2
        assert all(not row["business_key"].startswith("IT") for row in customers)
        edges = conn.execute(
            """
            SELECT rel, count(*) AS n FROM app.graph_edges
            WHERE valid_to IS NULL AND rel = ANY(%s)
            GROUP BY rel
            """,
            (["places", "contains", "suppliedBy"],),
        ).fetchall()
        found = {row["rel"]: row["n"] for row in edges}
        assert found["places"] >= 1
        assert found["contains"] >= 1
        assert found["suppliedBy"] >= 1
        sandbox = conn.execute("SELECT count(*) AS n FROM app.sandbox_documents").fetchone()
        assert sandbox["n"] == 0

    orders = client.get("/api/orders").json()["orders"]
    order = next(item for item in orders if item["business_key"] == "1024")
    assert order["shortage_qty"] == 80
    assert order["margin"] == "••••"
    detail = client.get(f"/api/orders/{order['id']}").json()
    assert detail["stock_formula"] == "disponibile = giacenza − impegnato"
    assert detail["lines"][0]["stock"]["available"] == 420

    inbox = client.get("/api/inbox").json()["recommendations"]
    assert len(inbox) == 1
    assert inbox[0]["requires_cfo"] is True
    assert "80" in inbox[0]["problem"] or any(item["value"] == 80 for item in inbox[0]["evidence"])


def test_cfo_approval_dry_run_idempotent_execute_and_masking(client: TestClient):
    login(client, "mapper")
    assert client.post("/api/demo/load").status_code == 200
    recommendation = client.get("/api/inbox").json()["recommendations"][0]
    denied = client.post(f"/api/recommendations/{recommendation['id']}/approve")
    assert denied.status_code == 403

    login(client, "operator")
    denied_operator = client.post(f"/api/recommendations/{recommendation['id']}/approve")
    assert denied_operator.status_code == 403
    assert "approvatore" in denied_operator.json()["detail"]

    login(client, "approver")
    order = next(item for item in client.get("/api/orders").json()["orders"] if item["business_key"] == "1024")
    assert order["margin"] == 0.6667
    customer = client.get(f"/api/customers/{order['customer_id']}").json()
    assert customer["company_name"] == "ACME SRL"
    vat = next(fact["value"] for fact in customer["facts"] if fact["property"] == "vat")
    assert vat == "IT12345678901"

    approved = client.post(f"/api/recommendations/{recommendation['id']}/approve")
    assert approved.status_code == 200, approved.text
    payloads = approved.json()["dry_run"]
    purchase = next(item for item in payloads if item["action"] == "createPurchaseOrder")
    assert purchase["supplier"] == "SUP-B"
    assert purchase["quantity"] == 80
    assert purchase["product"] == "ART-X"

    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.sandbox_documents").fetchone()["n"] == 0

    first = client.post(f"/api/recommendations/{recommendation['id']}/execute")
    assert first.status_code == 200, first.text
    second = client.post(f"/api/recommendations/{recommendation['id']}/execute")
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    with connect() as conn:
        documents = conn.execute(
            "SELECT id::text, status FROM app.sandbox_documents WHERE kind = 'purchase_order'"
        ).fetchall()
        assert len(documents) == 1
        edges = conn.execute(
            """
            SELECT count(*) AS n FROM app.graph_edges
            WHERE rel = 'triggered' AND valid_to IS NULL
            """
        ).fetchone()
        assert edges["n"] == 1
    compensated = client.post(f"/api/sandbox/{documents[0]['id']}/compensate")
    assert compensated.status_code == 200
    assert compensated.json()["status"] == "compensated"


def test_modify_changes_dry_run_quantity(client: TestClient):
    login(client, "mapper")
    client.post("/api/demo/load")
    recommendation = client.get("/api/inbox").json()["recommendations"][0]
    login(client, "approver")
    modified = client.post(
        f"/api/recommendations/{recommendation['id']}/modify",
        json={"quantity": 40},
    )
    assert modified.status_code == 200, modified.text
    approved = client.post(f"/api/recommendations/{recommendation['id']}/approve")
    purchase = next(item for item in approved.json()["dry_run"] if item["action"] == "createPurchaseOrder")
    assert purchase["quantity"] == 40


def test_reload_is_idempotent_and_drift_is_reviewed(client: TestClient, tmp_path: Path):
    login(client, "mapper")
    first = client.post(
        "/api/ingest/csv",
        files={"file": ("anagrafica.csv", Path("fixtures/anagrafica.csv").read_bytes(), "text/csv")},
        data={"source_name": "anagrafica"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["skipped"] is False
    second = client.post(
        "/api/ingest/csv",
        files={"file": ("anagrafica.csv", Path("fixtures/anagrafica.csv").read_bytes(), "text/csv")},
        data={"source_name": "anagrafica"},
    )
    assert second.json()["skipped"] is True

    drifted = tmp_path / "anagrafica.csv"
    drifted.write_text(
        "ANAG_CLI,RAGIONE_SOCIALE,PIVA,EMAIL,CODICE_LEGACY\nC001,Acme Srl,IT12345678901,ordini@acme.example,L1\n",
        encoding="utf-8",
    )
    third = client.post(
        "/api/ingest/csv",
        files={"file": ("anagrafica.csv", drifted.read_bytes(), "text/csv")},
        data={"source_name": "anagrafica"},
    )
    assert third.status_code == 200, third.text
    assert third.json()["drift"]["diff"]["added"] == ["codice_legacy"]
    mappings = client.get("/api/mappings").json()
    assert any(item["source_field"] == "codice_legacy" and item["status"] == "proposed" for item in mappings["mappings"])
    assert mappings["drift"][0]["status"] == "open"


def test_excel_and_sql_connectors(client: TestClient, tmp_path: Path):
    login(client, "mapper")
    workbook_path = tmp_path / "articoli.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["COD_ART", "DESCRIZIONE", "COSTO"])
    sheet.append(["ART-X", "Componente X", 40])
    workbook.save(workbook_path)
    excel = client.post(
        "/api/ingest/excel",
        files={"file": ("articoli.xlsx", workbook_path.read_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"source_name": "articoli_xl"},
    )
    assert excel.status_code == 200, excel.text
    assert any(item["source_field"] == "cod_art" for item in excel.json()["mappings"])

    model = load_merged()
    with connect() as conn:
        seed_src(conn)
        conn.commit()
    sql = client.post("/api/ingest/sql", json={"table": "src.clienti", "source_name": "clienti_sql"})
    assert sql.status_code == 200, sql.text
    assert any(item["source_field"] == "anag_cli" for item in sql.json()["mappings"])
    repeated = client.post("/api/ingest/sql", json={"table": "src.clienti", "source_name": "clienti_sql"})
    assert repeated.json()["skipped"] is True
    del model


def test_evidence_expiry_and_audit_is_append_only(client: TestClient):
    login(client, "mapper")
    client.post("/api/demo/load")
    expired = client.post("/api/stock/ART-X", json={"on_hand": 500})
    assert expired.status_code == 200, expired.text
    inbox = client.get("/api/inbox").json()["recommendations"]
    assert inbox[0]["status"] == "expired"
    login(client, "approver")
    denied = client.post(f"/api/recommendations/{inbox[0]['id']}/approve")
    assert denied.status_code == 409

    with connect() as conn:
        with pytest.raises(Exception, match="append-only"):
            conn.execute("UPDATE app.audit_log SET actor = 'tamper'")
            conn.commit()


def test_role_name_is_not_a_password(client: TestClient):
    denied = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert denied.status_code == 401


def test_demo_load_is_closed_without_the_flag(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    login(client, "mapper")
    monkeypatch.setenv("IEOL_DEMO_MODE", "0")
    denied = client.post("/api/demo/load")
    assert denied.status_code == 403


def test_inbox_previews_the_payload_before_approval(client: TestClient):
    login(client, "mapper")
    client.post("/api/demo/load")
    login(client, "approver")
    item = client.get("/api/inbox").json()["recommendations"][0]
    assert item["status"] == "proposed"
    assert "giacenza" in item["preview"]["formula"]
    assert any(payload["action"] == "createPurchaseOrder" for payload in item["preview"]["payloads"])


def test_similar_names_stay_apart_until_review(client: TestClient, tmp_path: Path):
    login(client, "mapper")
    client.post("/api/demo/load")
    path = tmp_path / "rossi.csv"
    path.write_text(
        "ANAG_CLI,RAGIONE_SOCIALE,PIVA\nR1,Mario Rossi Srl,IT11111111111\nR2,M. Rossi SRL,IT22222222222\n",
        encoding="utf-8",
    )
    uploaded = client.post(
        "/api/ingest/csv",
        files={"file": ("rossi.csv", path.read_bytes(), "text/csv")},
        data={"source_name": "rossi"},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert client.post("/api/mappings/confirm-confident").status_code == 200
    assert client.post("/api/materialize").status_code == 200
    reviews = client.get("/api/identity-reviews").json()["reviews"]
    assert any(item["status"] == "open" and "rossi" in item["reason"] for item in reviews)
    with connect() as conn:
        count = conn.execute(
            """
            SELECT count(*) AS n FROM app.entities
            WHERE type = 'Customer' AND business_key IN (
              SELECT business_key FROM app.entities WHERE type = 'Customer'
            )
            AND business_key LIKE 'id:%'
            AND id IN (
              SELECT entity_id FROM app.identity_aliases WHERE alias IN ('R1', 'R2')
            )
            """
        ).fetchone()
        assert count["n"] == 2


def test_app_role_cannot_rewrite_audit_or_drop(client: TestClient):
    login(client, "mapper")
    client.post("/api/demo/load")
    with connect() as conn:
        conn.execute("SET ROLE ieol_app")
        with pytest.raises(Exception):
            conn.execute("UPDATE app.audit_log SET actor = 'x'")
        conn.rollback()
    with connect() as conn:
        conn.execute("SET ROLE ieol_app")
        with pytest.raises(Exception):
            conn.execute("TRUNCATE app.audit_log")
        conn.rollback()
    with connect() as conn:
        conn.execute("SET ROLE ieol_app")
        with pytest.raises(Exception):
            conn.execute("DROP TABLE app.users")
        conn.rollback()


def test_migrations_apply_twice():
    from services.migrate import migrate

    with connect() as conn:
        again = migrate(conn)
        count = conn.execute("SELECT count(*) AS n FROM public.schema_migrations").fetchone()
    assert again == []
    assert count["n"] >= 2


def test_facts_always_have_confidence(client: TestClient):
    login(client, "mapper")
    client.post("/api/demo/load")
    with connect() as conn:
        missing = conn.execute("SELECT count(*) AS n FROM app.facts WHERE confidence IS NULL").fetchone()
    assert missing["n"] == 0


def test_baseline_matches_labeled_cases():
    from services.agent.evaluate import evaluate

    report = evaluate()
    assert report["correct_action_rate"] == 1
    assert report["overrides_excluded"] is True


def test_direct_fixture_pipeline_records_detected_state():
    model = load_merged()
    with connect() as conn:
        summary = load_fixtures(conn, actor="mapper", role="mapper", model=model)
        conn.commit()
        assert summary["recommendations"]
        events = conn.execute(
            """
            SELECT to_status FROM app.workflow_events
            WHERE recommendation_id = %s
            ORDER BY at
            """,
            (summary["recommendations"][0],),
        ).fetchall()
        assert [row["to_status"] for row in events[:2]] == ["detected", "proposed"]


def test_operator_cannot_rebuild_or_upload_without_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    login(client, "operator")
    denied = client.post("/api/materialize")
    assert denied.status_code == 403
    login(client, "mapper")
    monkeypatch.setattr("apps.api.main.MAX_UPLOAD_BYTES", 8)
    oversized = client.post(
        "/api/ingest/csv",
        files={"file": ("big.csv", b"0123456789", "text/csv")},
        data={"source_name": "big"},
    )
    assert oversized.status_code == 413


def test_email_and_pdf_orders_enter_the_same_model(client: TestClient, tmp_path: Path):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, StreamObject

    login(client, "mapper")
    assert client.post("/api/demo/load").status_code == 200
    email = client.post(
        "/api/ingest/document",
        files={"file": ("ordine_email.txt", Path("fixtures/ordine_email.txt").read_bytes(), "text/plain")},
    )
    assert email.status_code == 200, email.text
    assert email.json()["order_key"] == "1099"
    assert email.json()["status"] == "open"
    assert client.get("/api/orders").json()["orders"]
    assert all(item["business_key"] != "1099" for item in client.get("/api/orders").json()["orders"])
    released = client.post(f"/api/quarantine/{email.json()['id']}/release")
    assert released.status_code == 200, released.text
    created = next(item for item in client.get("/api/orders").json()["orders"] if item["business_key"] == "1099")
    assert created["shortage_qty"] == 1000
    assert created["customer_name"] == "Nuova Srl"

    text = Path("fixtures/ordine_email.txt").read_text(encoding="utf-8").replace("1099", "1100").replace("C009", "C010")
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=500)
    page = writer.pages[0]
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    lines = ["BT /F1 10 Tf 40 460 Td"]
    for line in text.splitlines():
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        lines.append(f"({escaped}) Tj T*")
    lines.append("ET")
    stream = StreamObject()
    stream.set_data("\n".join(lines).encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Resources")] = resources
    pdf_path = tmp_path / "ordine.pdf"
    with pdf_path.open("wb") as handle:
        writer.write(handle)
    pdf = client.post(
        "/api/ingest/document",
        files={"file": ("ordine.pdf", pdf_path.read_bytes(), "application/pdf")},
    )
    assert pdf.status_code == 200, pdf.text
    assert pdf.json()["source_kind"] == "pdf"
    assert pdf.json()["order_key"] == "1100"
    assert pdf.json()["status"] == "open"


def test_sql_connector_rejects_application_tables(client: TestClient):
    login(client, "mapper")
    denied = client.post("/api/ingest/sql", json={"table": "app.users", "source_name": "users"})
    assert denied.status_code == 400


def test_operator_inbox_hides_confidential_evidence(client: TestClient):
    login(client, "mapper")
    assert client.post("/api/demo/load").status_code == 200
    login(client, "operator")
    evidence = client.get("/api/inbox").json()["recommendations"][0]["evidence"]
    amount = next(item for item in evidence if item["property"] == "amount")
    vat = next(item for item in evidence if item["property"] == "vat")
    assert amount["value"] == "••••"
    assert vat["value"] == "••••"
    login(client, "approver")
    visible = client.get("/api/inbox").json()["recommendations"][0]["evidence"]
    assert next(item for item in visible if item["property"] == "amount")["value"] == 60000
    samples = client.get("/api/mappings").json()["mappings"]
    piva = next(item for item in samples if item["source_field"] == "piva")
    assert "IT12345678901" in piva["samples"]


def test_executed_order_is_not_proposed_again(client: TestClient):
    login(client, "mapper")
    client.post("/api/demo/load")
    recommendation = client.get("/api/inbox").json()["recommendations"][0]
    login(client, "approver")
    assert client.post(f"/api/recommendations/{recommendation['id']}/approve").status_code == 200
    assert client.post(f"/api/recommendations/{recommendation['id']}/execute").status_code == 200
    login(client, "mapper")
    again = client.post("/api/agent/run")
    assert again.status_code == 200, again.text
    assert again.json()["recommendations"] == []
    with connect() as conn:
        count = conn.execute("SELECT count(*) AS n FROM app.sandbox_documents WHERE kind = 'purchase_order'").fetchone()
        assert count["n"] == 1


def test_execute_keeps_expired_status(client: TestClient):
    login(client, "mapper")
    client.post("/api/demo/load")
    recommendation = client.get("/api/inbox").json()["recommendations"][0]["id"]
    login(client, "approver")
    assert client.post(f"/api/recommendations/{recommendation}/approve").status_code == 200
    with connect() as conn:
        conn.execute(
            """
            UPDATE app.facts
            SET value = '999'::jsonb
            WHERE property = 'on_hand' AND valid_to IS NULL
            """
        )
        conn.commit()
    denied = client.post(f"/api/recommendations/{recommendation}/execute")
    assert denied.status_code == 409
    status = client.get("/api/inbox").json()["recommendations"][0]["status"]
    assert status == "expired"


def test_failed_execution_is_audited(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    login(client, "mapper")
    client.post("/api/demo/load")
    recommendation = client.get("/api/inbox").json()["recommendations"][0]["id"]
    login(client, "approver")
    assert client.post(f"/api/recommendations/{recommendation}/approve").status_code == 200

    def boom(*_args, **_kwargs):
        raise RuntimeError("sandbox down")

    monkeypatch.setattr("services.actions.executor._sandbox", boom)
    with connect() as conn:
        with pytest.raises(AppError, match="Esecuzione fallita"):
            execute(conn, load_merged(), recommendation, actor="approver", role="approver")
    with connect() as conn:
        status = conn.execute(
            "SELECT status FROM app.recommendations WHERE id = %s",
            (recommendation,),
        ).fetchone()
        assert status["status"] == "failed"
        outcome = conn.execute(
            """
            SELECT 1 FROM app.audit_log
            WHERE action = 'outcome' AND entity_ref = %s
            """,
            (recommendation,),
        ).fetchone()
        assert outcome is not None
