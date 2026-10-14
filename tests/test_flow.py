from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from apps.api.main import app
from services.db import connect
from services.pipeline import load_fixtures, seed_src
from ontology.loader import load_merged


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, role: str) -> None:
    response = client.post("/api/login", json={"username": role, "password": role})
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

    with connect() as conn:
        customers = conn.execute("SELECT count(*) AS n FROM app.entities WHERE type = 'Customer'").fetchone()
        assert customers["n"] == 2
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
    assert "CFO" in denied_operator.json()["detail"]

    login(client, "approver")
    order = next(item for item in client.get("/api/orders").json()["orders"] if item["business_key"] == "1024")
    assert order["margin"] == 0.6667
    customer = client.get(f"/api/customers/{order['customer_id']}").json()
    assert customer["company_name"] == "Acme Srl"
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
