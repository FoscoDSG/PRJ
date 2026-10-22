import os
import subprocess
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from apps.api.main import app
from ontology.loader import load_merged
from services.actions.executor import execute
from services.db import connect
from services.mapping.corrections import company_id_for_user
from services.errors import AppError
from services.pii import ROLE_HIDDEN
from services.pipeline import load_fixtures, seed_src


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, role: str) -> None:
    password = os.environ[f"IEOL_SEED_{role.upper()}_PASSWORD"]
    response = client.post("/api/login", json={"username": role, "password": password})
    assert response.status_code == 200, response.text


def load_demo() -> None:
    model = load_merged()
    with connect() as conn:
        company_id = company_id_for_user(conn, "mapper")
        load_fixtures(conn, actor="mapper", role="mapper", model=model, company_id=company_id)
        conn.commit()


def ensure_company(username: str, name: str | None = None) -> str:
    """Give a seeded user a company once, so an existing decision test can pass the gate."""
    with connect() as conn:
        row = conn.execute(
            "SELECT company_id::text AS company_id FROM app.users WHERE username = %s",
            (username,),
        ).fetchone()
    if row and row["company_id"]:
        return row["company_id"]
    return give_company(username, name or f"Azienda {username}")


def give_company(username: str = "mapper", name: str = "Azienda test") -> str:
    """Create a company and assign it. API uploads need this link."""
    import json
    from contextlib import redirect_stdout
    from io import StringIO

    from apps.cli import main

    created = StringIO()
    with redirect_stdout(created):
        main(["company", "create", "--name", name])
    company_id = json.loads(created.getvalue().strip().splitlines()[-1])["company_id"]
    with redirect_stdout(StringIO()):
        main(["company", "assign", "--username", username, "--company-id", company_id])
    return company_id


def decide(client: TestClient, item: dict, decision: str = "approve", **extra):
    ensure_company("approver", "Approvazioni")
    body = {"decision": decision, "evidence_hash": item["evidence_hash"], **extra}
    return client.post(f"/api/recommendations/{item['id']}/{decision}", json=body)


def test_mapping_review_identity_and_agent_does_not_execute(client: TestClient):
    login(client, "mapper")
    load_demo()
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
    assert order["margin"] == ROLE_HIDDEN
    detail = client.get(f"/api/orders/{order['id']}").json()
    assert detail["stock_formula"] == "disponibile = giacenza − impegnato"
    assert detail["lines"][0]["stock"]["available"] == 420

    inbox = client.get("/api/inbox").json()["recommendations"]
    assert len(inbox) == 1
    assert inbox[0]["requires_cfo"] is True
    assert "80" in inbox[0]["problem"] or any(item["value"] == 80 for item in inbox[0]["evidence"])


def test_cfo_approval_dry_run_idempotent_execute_and_masking(client: TestClient):
    login(client, "mapper")
    load_demo()
    recommendation = client.get("/api/inbox").json()["recommendations"][0]
    denied = decide(client, recommendation)
    assert denied.status_code == 403

    login(client, "operator")
    denied_operator = decide(client, recommendation)
    assert denied_operator.status_code == 403
    assert "approvatore" in denied_operator.json()["detail"]

    login(client, "approver")
    order = next(item for item in client.get("/api/orders").json()["orders"] if item["business_key"] == "1024")
    assert order["margin"] == 0.6667
    customer = client.get(f"/api/customers/{order['customer_id']}").json()
    assert customer["company_name"] == "••••"
    vat = next(fact["value"] for fact in customer["facts"] if fact["property"] == "vat")
    assert vat == "••••"
    with connect() as conn:
        stored_vat = conn.execute(
            """
            SELECT value_cleartext #>> '{}' AS clear
            FROM app.facts
            WHERE entity_id = %s AND property = 'vat' AND valid_to IS NULL
            """,
            (customer["id"],),
        ).fetchone()
    assert stored_vat["clear"] == "IT12345678901"

    approved = decide(client, recommendation)
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
    load_demo()
    recommendation = client.get("/api/inbox").json()["recommendations"][0]
    login(client, "approver")
    modified = decide(client, recommendation, "modify", quantity=40)
    assert modified.status_code == 200, modified.text
    approved = decide(client, recommendation)
    purchase = next(item for item in approved.json()["dry_run"] if item["action"] == "createPurchaseOrder")
    assert purchase["quantity"] == 40


def test_reload_is_idempotent_and_drift_is_reviewed(client: TestClient, tmp_path: Path):
    give_company("mapper", "Ricarica")
    login(client, "mapper")
    first = client.post(
        "/api/ingest/csv",
        files={"file": ("anagrafica.csv", Path("fixtures/anagrafica.csv").read_bytes(), "text/csv")},
        data={"source_name": "anagrafica", "source_class": "system_of_record"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["skipped"] is False
    second = client.post(
        "/api/ingest/csv",
        files={"file": ("anagrafica.csv", Path("fixtures/anagrafica.csv").read_bytes(), "text/csv")},
        data={"source_name": "anagrafica", "source_class": "system_of_record"},
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
        data={"source_name": "anagrafica", "source_class": "system_of_record"},
    )
    assert third.status_code == 200, third.text
    assert third.json()["drift"]["diff"]["added"] == ["codice_legacy"]
    mappings = client.get("/api/mappings").json()
    assert any(item["source_field"] == "codice_legacy" and item["status"] == "proposed" for item in mappings["mappings"])
    assert mappings["drift"][0]["status"] == "open"


def test_excel_and_sql_connectors(client: TestClient, tmp_path: Path):
    give_company("mapper", "Connettori")
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
        data={"source_name": "articoli_xl", "source_class": "system_of_record"},
    )
    assert excel.status_code == 200, excel.text
    assert any(item["source_field"] == "cod_art" for item in excel.json()["mappings"])

    model = load_merged()
    with connect() as conn:
        seed_src(conn)
        conn.commit()
    sql = client.post(
        "/api/ingest/sql",
        json={"table": "src.clienti", "source_name": "clienti_sql", "source_class": "system_of_record"},
    )
    assert sql.status_code == 200, sql.text
    assert any(item["source_field"] == "anag_cli" for item in sql.json()["mappings"])
    repeated = client.post(
        "/api/ingest/sql",
        json={"table": "src.clienti", "source_name": "clienti_sql", "source_class": "system_of_record"},
    )
    assert repeated.json()["skipped"] is True
    del model


def test_evidence_expiry_and_audit_is_append_only(client: TestClient):
    login(client, "mapper")
    load_demo()
    give_company("admin", "Amministrazione IEOL")
    login(client, "admin")
    expired = client.post("/api/stock/ART-X", json={"on_hand": 500})
    assert expired.status_code == 200, expired.text
    with connect() as conn:
        adjusted = conn.execute(
            """
            SELECT f.source_class, f.confidence IS NULL AS missing, f.confidence_method, f.confidence_rule
            FROM app.facts f
            JOIN app.entities e ON e.id = f.entity_id
            WHERE f.property = 'on_hand' AND f.valid_to IS NULL
              AND e.business_key = 'ART-X|WH1'
            """
        ).fetchone()
    assert adjusted["source_class"] == "manual"
    assert adjusted["missing"] is True
    assert adjusted["confidence_method"] == "human"
    assert adjusted["confidence_rule"] is None
    inbox = client.get("/api/inbox").json()["recommendations"]
    assert inbox[0]["status"] == "expired"
    login(client, "approver")
    denied = decide(client, inbox[0])
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
    load_demo()
    login(client, "approver")
    item = client.get("/api/inbox").json()["recommendations"][0]
    assert item["status"] == "proposed"
    assert "giacenza" in item["preview"]["formula"]
    assert any(payload["action"] == "createPurchaseOrder" for payload in item["preview"]["payloads"])


def test_similar_names_stay_apart_until_review(client: TestClient, tmp_path: Path):
    login(client, "mapper")
    load_demo()
    give_company("mapper", "Nomi vicini")
    path = tmp_path / "rossi.csv"
    path.write_text(
        "ANAG_CLI,RAGIONE_SOCIALE,PIVA\nR1,Mario Rossi Srl,IT11111111111\nR2,M. Rossi SRL,IT22222222222\n",
        encoding="utf-8",
    )
    uploaded = client.post(
        "/api/ingest/csv",
        files={"file": ("rossi.csv", path.read_bytes(), "text/csv")},
        data={"source_name": "rossi", "source_class": "external"},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert client.post("/api/mappings/confirm-confident").status_code == 200
    assert client.post("/api/materialize").status_code == 200
    reviews = client.get("/api/identity-reviews").json()["reviews"]
    assert any(
        item["status"] == "open" and item["reason"] == "Nomi vicini senza identità comune"
        for item in reviews
    )
    assert all("rossi" not in (item["reason"] or "").lower() for item in reviews)
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
    load_demo()
    from services.db import app_connect

    for statement in (
        "UPDATE app.audit_log SET actor = 'x'",
        "TRUNCATE app.audit_log",
        "DROP TABLE app.users",
    ):
        with app_connect() as conn:
            conn.execute("RESET ROLE")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


def test_migrations_apply_twice():
    from services.migrate import migrate

    with connect() as conn:
        again = migrate(conn)
        count = conn.execute("SELECT count(*) AS n FROM public.schema_migrations").fetchone()
    assert again == []
    assert count["n"] >= 2


def test_facts_keep_optional_confidence_and_unknown_provenance(client: TestClient):
    login(client, "mapper")
    load_demo()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT source_class, confidence_method, confidence_rule,
                   confidence IS NULL AS missing,
                   count(*) AS n
            FROM app.facts
            WHERE valid_to IS NULL
            GROUP BY 1, 2, 3, 4
            """
        ).fetchall()
        prices = {
            row["product_source_key"]: int(row["price"])
            for row in conn.execute(
                "SELECT product_source_key, price FROM app.price_list ORDER BY product_source_key"
            ).fetchall()
        }
    assert prices == {"ART-X": 120, "ART-Y": 200}
    assert rows
    assert all(row["source_class"] != "manual" for row in rows)
    assert any(row["missing"] for row in rows)
    assert all(row["confidence_method"] == "unknown" for row in rows if row["missing"])
    assert all(row["confidence_method"] == "rule" for row in rows if not row["missing"])
    assert any(not row["missing"] and row["source_class"] == "system_of_record" for row in rows)

    order = next(item for item in client.get("/api/orders").json()["orders"] if item["business_key"] == "1024")
    detail = client.get(f"/api/orders/{order['id']}").json()
    shortage = next(fact for fact in detail["facts"] if fact["property"] == "shortage_qty")
    status = next(fact for fact in detail["facts"] if fact["property"] == "status")
    assert shortage["confidence"] is None
    assert shortage["confidence_method"] == "unknown"
    assert shortage["confidence_rule"] is None
    assert shortage["source_class"] == "system_of_record"
    assert shortage["origin"] == "calculated"
    assert status["confidence_method"] == "rule"
    assert status["confidence_rule"] == "mapping.header_similarity@v1"
    assert status["source_class"] == "system_of_record"
    assert status["confidence"] not in (None, 1)
    assert all(
        row["confidence_rule"] == "mapping.header_similarity@v1"
        for row in rows
        if row["confidence_method"] == "rule"
    )
    assert all(row["confidence_rule"] is None for row in rows if row["confidence_method"] != "rule")

    rendered = subprocess.run(
        ["node", "-e", _FACT_LABEL_SCRIPT],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"


_FACT_LABEL_SCRIPT = r"""
const { orderDetail, customerDetail, recommendationConfidence, showFixtureButton, inboxCard } = require("./apps/web/escape.js");
function linesOf(html, property) {
  const item = html.split("<li>").find((part) => part.includes(`<code>${property}</code>`));
  if (!item) return [];
  return [...item.matchAll(/<p\b[^>]*>([^<]*)<\/p>/g)].map((match) => match[1]);
}
const unknown = orderDetail({
  business_key: "1024",
  stock_formula: "disponibile",
  facts: [{ property: "status", value: "open", source_class: "unknown", confidence: null, confidence_method: "unknown" }],
  lines: [],
  edges: [],
});
const unknownLines = linesOf(unknown, "status");
if (unknownLines[0] !== "provenienza sconosciuta" || unknownLines[1] !== "confidenza non disponibile") {
  console.error(unknown);
  process.exit(2);
}
if (unknown.includes("%") || unknown.includes("manuale")) {
  console.error(unknown);
  process.exit(3);
}
const calculated = orderDetail({
  business_key: "1024",
  stock_formula: "disponibile",
  facts: [{ property: "shortage_qty", value: 80, source_class: "operational", confidence: null, confidence_method: "unknown" }],
  lines: [],
  edges: [],
});
const calculatedLines = linesOf(calculated, "shortage_qty");
if (calculatedLines[0] !== "operativa" || calculatedLines[1] !== "confidenza non disponibile") {
  console.error(calculated);
  process.exit(4);
}
if (calculated.includes("%") || calculated.includes("manuale") || calculated.includes("provenienza sconosciuta")) {
  console.error(calculated);
  process.exit(5);
}
for (const legacy of ["erp", "crm", "ai", "calculated"]) {
  const retired = orderDetail({
    business_key: "1024",
    stock_formula: "disponibile",
    facts: [{ property: "status", value: "open", source_class: legacy, confidence: null, confidence_method: "unknown" }],
    lines: [],
    edges: [],
  });
  if (linesOf(retired, "status")[0] !== "provenienza sconosciuta") {
    console.error(legacy, retired);
    process.exit(16);
  }
}
const scored = orderDetail({
  business_key: "1024",
  stock_formula: "disponibile",
  facts: [{
    property: "status",
    value: "open",
    source_class: "unknown",
    confidence: 0.93,
    confidence_method: "rule",
    confidence_rule: "mapping.header_similarity@v1",
  }],
  lines: [],
  edges: [],
});
const scoredLines = linesOf(scored, "status");
if (scoredLines[0] !== "provenienza sconosciuta") {
  console.error(scored);
  process.exit(6);
}
if (scoredLines[1] !== "0,93 · punteggio di regola, non calibrato · mapping.header_similarity@v1") {
  console.error(scoredLines[1]);
  process.exit(7);
}
if (scored.includes("%") || scored.includes("manuale") || scored.includes("confidenza non disponibile")) {
  console.error(scored);
  process.exit(8);
}
const known = customerDetail({
  company_name: "ACME",
  business_key: "C1",
  orders: [],
  edges: [],
  facts: [{ property: "note", value: "bozza", source_class: "manual", confidence: null, confidence_method: "human" }],
});
const knownLines = linesOf(known, "note");
if (knownLines[0] !== "manuale" || knownLines[1] !== "rettifica manuale") {
  console.error(known);
  process.exit(9);
}
if (known.includes("%") || known.includes("provenienza sconosciuta") || known.includes("confidenza non disponibile")) {
  console.error(known);
  process.exit(10);
}
if (recommendationConfidence(null) !== "confidenza non disponibile" || recommendationConfidence(0.86) !== "0,86") {
  process.exit(11);
}
if (String(recommendationConfidence(0.86)).includes("%")) {
  process.exit(12);
}
const card = inboxCard({
  id: "1",
  status: "proposed",
  risk: "high",
  order_key: "1024",
  problem: "Ordine 1024",
  confidence: null,
  evidence_hash: "abc",
  evidence: [{ property: "status", value: "open", source_class: "unknown" }],
  actions: [],
  requires_cfo: false,
  preview: { formula: "disponibile = giacenza − impegnato", payloads: [{ action: "createPurchaseOrder" }] },
}, "approver");
if (!card.includes("Anteprima di approvazione") || !card.includes("Cosa verrà approvato")) {
  console.error(card);
  process.exit(13);
}
if (!card.includes("provenienza sconosciuta") || !card.includes("confidenza non disponibile") || card.includes("%")) {
  console.error(card);
  process.exit(14);
}
if (!showFixtureButton("admin", true) || showFixtureButton("mapper", true) || showFixtureButton("admin", false)) {
  process.exit(15);
}
const scoredCard = inboxCard({
  id: "2",
  status: "proposed",
  risk: "high",
  order_key: "1024",
  problem: "Ordine 1024",
  confidence: 0.93,
  confidence_method: "rule",
  confidence_rule: "order-agent.baseline@v1",
  evidence_hash: "abc",
  evidence: [],
  actions: [{ type: "createPurchaseOrder", params: { quantity: 1 } }],
  requires_cfo: false,
  preview: { formula: "disponibile", payloads: [] },
}, "approver");
if (!scoredCard.includes("0,93 · punteggio di regola, non calibrato · order-agent.baseline@v1")) {
  console.error(scoredCard);
  process.exit(17);
}
if (scoredCard.includes("%") || scoredCard.includes('data-act="approve" disabled')) {
  console.error(scoredCard);
  process.exit(18);
}
if (!scoredCard.includes("data-reason") || !scoredCard.includes("required") || !scoredCard.includes('minlength="5"')) {
  console.error(scoredCard);
  process.exit(19);
}
const blockedCard = inboxCard({
  id: "3",
  status: "proposed",
  risk: "high",
  order_key: "1024",
  problem: "Ordine 1024",
  confidence: null,
  confidence_method: "unknown",
  evidence_hash: "abc",
  evidence: [],
  actions: [],
  requires_cfo: false,
  blocked_reason: "provenienza sconosciuta",
  preview: { formula: "disponibile", payloads: [] },
}, "approver");
if (!blockedCard.includes('data-act="approve" disabled') || !blockedCard.includes("provenienza sconosciuta")) {
  console.error(blockedCard);
  process.exit(20);
}
if (!blockedCard.includes("confidenza non disponibile")) {
  console.error(blockedCard);
  process.exit(21);
}
process.stdout.write("ok\n");
"""


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
    give_company("mapper", "Limite byte")
    login(client, "operator")
    denied = client.post("/api/materialize")
    assert denied.status_code == 403
    login(client, "mapper")
    monkeypatch.setattr("apps.api.main.MAX_UPLOAD_BYTES", 8)
    oversized = client.post(
        "/api/ingest/csv",
        files={"file": ("big.csv", b"0123456789", "text/csv")},
        data={"source_name": "big", "source_class": "manual"},
    )
    assert oversized.status_code == 413


def test_email_and_pdf_orders_enter_the_same_model(client: TestClient, tmp_path: Path):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, StreamObject

    give_company("mapper", "Documenti")
    login(client, "mapper")
    load_demo()
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
    assert created["customer_name"] == "••••"
    with connect() as conn:
        stored_name = conn.execute(
            """
            SELECT value_cleartext #>> '{}' AS clear
            FROM app.facts
            WHERE entity_id = %s AND property = 'company_name' AND valid_to IS NULL
            """,
            (created["customer_id"],),
        ).fetchone()
    assert stored_name["clear"] == "Nuova Srl"

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
    give_company("mapper", "Sql rifiutato")
    login(client, "mapper")
    denied = client.post(
        "/api/ingest/sql",
        json={"table": "app.users", "source_name": "users", "source_class": "system_of_record"},
    )
    assert denied.status_code == 400


def test_operator_inbox_hides_confidential_evidence(client: TestClient):
    login(client, "mapper")
    load_demo()
    login(client, "operator")
    evidence = client.get("/api/inbox").json()["recommendations"][0]["evidence"]
    amount = next(item for item in evidence if item["property"] == "amount")
    cost = next(item for item in evidence if item["property"] == "cost")
    vat = next(item for item in evidence if item["property"] == "vat")
    assert amount["value"] == 60000
    assert cost["value"] == ROLE_HIDDEN
    assert vat["value"] == "••••"
    login(client, "approver")
    visible = client.get("/api/inbox").json()["recommendations"][0]["evidence"]
    assert next(item for item in visible if item["property"] == "amount")["value"] == 60000
    samples = client.get("/api/mappings").json()["mappings"]
    piva = next(item for item in samples if item["source_field"] == "piva")
    assert piva["samples"]
    assert all(sample == "••••" for sample in piva["samples"])
    assert "IT12345678901" not in str(samples)


def test_executed_order_is_not_proposed_again(client: TestClient):
    login(client, "mapper")
    load_demo()
    recommendation = client.get("/api/inbox").json()["recommendations"][0]
    login(client, "approver")
    assert decide(client, recommendation).status_code == 200
    assert client.post(f"/api/recommendations/{recommendation['id']}/execute").status_code == 200
    login(client, "mapper")
    give_company("mapper", "Riesecuzione")
    again = client.post("/api/agent/run")
    assert again.status_code == 200, again.text
    assert again.json()["recommendations"] == []
    with connect() as conn:
        count = conn.execute("SELECT count(*) AS n FROM app.sandbox_documents WHERE kind = 'purchase_order'").fetchone()
        assert count["n"] == 1


def test_execute_keeps_expired_status(client: TestClient):
    login(client, "mapper")
    load_demo()
    recommendation = client.get("/api/inbox").json()["recommendations"][0]
    login(client, "approver")
    assert decide(client, recommendation).status_code == 200
    with connect() as conn:
        conn.execute(
            """
            UPDATE app.facts
            SET value = '999'::jsonb
            WHERE property = 'on_hand' AND valid_to IS NULL
            """
        )
        conn.commit()
    denied = client.post(f"/api/recommendations/{recommendation['id']}/execute")
    assert denied.status_code == 409
    status = client.get("/api/inbox").json()["recommendations"][0]["status"]
    assert status == "expired"


def test_failed_execution_is_audited(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    login(client, "mapper")
    load_demo()
    recommendation = client.get("/api/inbox").json()["recommendations"][0]
    login(client, "approver")
    assert decide(client, recommendation).status_code == 200

    def boom(*_args, **_kwargs):
        raise RuntimeError("sandbox down")

    monkeypatch.setattr("services.actions.executor._sandbox", boom)
    with connect() as conn:
        with pytest.raises(AppError, match="Esecuzione fallita"):
            execute(conn, load_merged(), recommendation["id"], actor="approver", role="approver")
    with connect() as conn:
        status = conn.execute(
            "SELECT status FROM app.recommendations WHERE id = %s",
            (recommendation["id"],),
        ).fetchone()
        assert status["status"] == "failed"
        outcome = conn.execute(
            """
            SELECT 1 FROM app.audit_log
            WHERE action = 'outcome' AND entity_ref = %s
            """,
            (recommendation["id"],),
        ).fetchone()
        assert outcome is not None
