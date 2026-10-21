"""Ordine-incasso: dati di prova, regole, isolamento e misura.

I file in fixtures/synthetic dichiarano label_origin synthetic.
I casi della misura costruiti in memoria sono etichette di test, non fatture vere.
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app

from apps.cli import main
from ontology.loader import load_merged
from services.agent.payment_match import (
    RULE,
    InvoiceView,
    MatchPolicy,
    PaymentView,
    load_policy,
    select_matches,
)
from services.agent.payment_measure import (
    MIN_MEASURED_CASES,
    MIN_MEASURED_SOURCES,
    RECALL_LABEL,
    UNMATCHED_LABEL,
    UNMEASURED,
    measure_payment_matches,
    score_cases,
)
from services.cash.documents import write_payment, write_supplier
from services.companies import USER_WITHOUT_COMPANY
from services.db import app_connect, connect
from services.graph.projection import project_graph
from services.pii import MASK
from tests.test_flow import decide, give_company, load_demo, login

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "fixtures" / "synthetic"
IBAN = "IT00TEST0000000000000000000"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _assign(username: str, company_id: str) -> None:
    from contextlib import redirect_stdout
    from io import StringIO

    with redirect_stdout(StringIO()):
        main(["company", "assign", "--username", username, "--company-id", company_id])


def _upload(client: TestClient, name: str, filename: str, content: bytes) -> dict:
    response = client.post(
        f"/api/ingest/{name}",
        files={"file": (filename, content, "application/octet-stream")},
        data={"source_name": filename.rsplit(".", 1)[0], "source_class": "external"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _status(entity_id: str) -> str:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT value #>> '{}' AS status
            FROM app.facts
            WHERE entity_id = %s AND property = 'status' AND valid_to IS NULL
            """,
            (entity_id,),
        ).fetchone()
    return row["status"] if row else ""


def _process(catalog: dict, process_id: str) -> dict:
    return next(item for item in catalog["processes"] if item["id"] == process_id)


def test_rule_proposes_only_a_complete_coincidence():
    invoice = InvoiceView("inv", "FT-TEST-100", Decimal("122.00"), vat="IT00000000001", name="Cliente Prova Alfa")
    other = InvoiceView("inv2", "FT-TEST-301", Decimal("80.00"), vat="IT00000000001", name="Cliente Prova Alfa")
    small = InvoiceView("inv3", "FT-TEST-302", Decimal("20.00"), vat="IT00000000001", name="Cliente Prova Alfa")
    full = PaymentView(
        "pay",
        "INC-100",
        Decimal("122.00"),
        vat="IT00000000001",
        name="Cliente Prova Alfa",
        text="Saldo FT-TEST-100",
        source_name="incassi-prova",
        label_origin="synthetic",
    )
    partial = PaymentView(
        "pay-part",
        "INC-200",
        Decimal("40.00"),
        vat="IT00000000001",
        text="Acconto FT-TEST-200",
        source_name="incassi-prova",
        label_origin="synthetic",
    )
    both = PaymentView(
        "pay-both",
        "INC-301",
        Decimal("100.00"),
        vat="IT00000000001",
        text="Saldo FT-TEST-301 e FT-TEST-302",
        source_name="incassi-prova",
        label_origin="synthetic",
    )
    subset = PaymentView(
        "pay-sub",
        "INC-SUB",
        Decimal("80.00"),
        vat="IT00000000001",
        text="FT-TEST-301 FT-TEST-302",
        source_name="incassi-prova",
        label_origin="synthetic",
    )
    amount_only = PaymentView(
        "pay-amt",
        "INC-AMT",
        Decimal("122.00"),
        vat="IT00000000001",
        text="senza numero",
        source_name="incassi-prova",
        label_origin="synthetic",
    )
    strict = select_matches([full, partial, both, subset, amount_only], [invoice, other, small])
    by_payment = {}
    for item in strict.proposals:
        by_payment.setdefault(item.payment_id, []).append(item)
    assert list(by_payment) == ["pay", "pay-both"]
    assert by_payment["pay"][0].score == 1.0
    assert by_payment["pay"][0].criteria == {"amount": True, "customer": True, "reference": True}
    assert {item.invoice_number for item in by_payment["pay-both"]} == {"FT-TEST-301", "FT-TEST-302"}
    assert "pay-part" in strict.unmatched_payment_ids
    assert "pay-sub" in strict.unmatched_payment_ids
    assert "pay-amt" in strict.unmatched_payment_ids
    relaxed = select_matches(
        [partial],
        [InvoiceView("inv-p", "FT-TEST-200", Decimal("90.00"), vat="IT00000000001")],
        MatchPolicy(require_all_criteria=False, threshold=0.6),
    )
    assert len(relaxed.proposals) == 1
    assert relaxed.proposals[0].allocated_amount == Decimal("40.00")
    assert relaxed.proposals[0].criteria["amount"] is False
    assert load_policy().require_all_criteria is True
    assert load_policy().threshold == 1.0
    assert load_policy().rule == RULE


def test_measure_stays_provisional_until_cases_and_source_names():
    assert MIN_MEASURED_CASES == 30
    assert MIN_MEASURED_SOURCES == 3
    assert UNMEASURED == "non misurata"

    def case(index: int, source: str, company: str, **overrides):
        proposal = {
            "id": f"p{index}",
            "company_id": company,
            "payment_entity_id": f"pay{index}",
            "invoice_entity_id": f"inv{index}",
            "source_name": source,
            "label_origin": "rule",
            "allocated_amount": "10.00",
        }
        decision = {
            "id": f"d{index}",
            "proposal_id": f"p{index}",
            "payment_entity_id": f"pay{index}",
            "company_id": company,
            "decision": "confirm",
            "invoice_entity_id": f"inv{index}",
            "allocated_amount": "10.00",
            "source_name": source,
            "revokes_decision_id": None,
        }
        payment = {
            "id": f"pay{index}",
            "company_id": company,
            "source_name": source,
            "label_origin": "rule",
        }
        proposal.update(overrides.get("proposal") or {})
        decision.update(overrides.get("decision") or {})
        return proposal, decision, payment

    short_sources = [case(i, "alfa" if i % 2 == 0 else "beta", "c1") for i in range(30)]
    report = score_cases(*_unzip(short_sources))
    assert report["label"] == UNMEASURED
    assert report["provisional"] is True
    assert report["measured"] is False
    assert report["human_cases"] == 30
    assert report["source_count"] == 2

    short_cases = [case(i, ("alfa", "beta", "gamma")[i % 3], "c1") for i in range(29)]
    report = score_cases(*_unzip(short_cases))
    assert report["human_cases"] == 29
    assert report["source_count"] == 3
    assert report["label"] == UNMEASURED

    shared = []
    for index in range(30):
        source = ("alfa", "beta", "gamma")[index % 3]
        company = "c1" if index % 2 == 0 else "c2"
        shared.append(case(index, source, company))
    report = score_cases(*_unzip(shared))
    assert report["source_count"] == 3
    assert report["source_names"] == ["alfa", "beta", "gamma"]
    assert report["label"] == "misurata"
    assert report["provisional"] is False
    assert report["precision"] == 1
    assert report["recall"] == 1
    assert report["rule"] == RULE
    assert "probabil" not in report["score_kind"] or "non probabil" in report["score_kind"]

    same_name = [case(index, "stesso", "c1" if index % 2 == 0 else "c2") for index in range(30)]
    report = score_cases(*_unzip(same_name))
    assert report["source_count"] == 1
    assert report["label"] == UNMEASURED

    blank = case(0, "", "c1", decision={"source_name": ""}, proposal={"source_name": ""})
    report = score_cases(*_unzip([blank]))
    assert report["missing_source"] == 1
    assert report["source_count"] == 0

    wrong_amount = case(
        1,
        "alfa",
        "c1",
        decision={"decision": "correct", "allocated_amount": "4.00"},
    )
    same_amount = case(2, "alfa", "c1", decision={"decision": "correct"})
    report = score_cases(*_unzip([wrong_amount, same_amount]))
    assert report["true_positive"] == 1
    assert report["false_positive"] == 1
    assert report["false_negative"] == 1

    opened = case(3, "alfa", "c1")
    later = case(3, "alfa", "c1", decision={"id": "d3b", "decision": "correct", "allocated_amount": "4.00"})
    report = score_cases(*_unzip([opened, later]))
    assert report["true_positive"] == 0
    assert report["false_positive"] == 1

    confirmed = case(4, "alfa", "c1")
    revoked = {
        "id": "rev1",
        "proposal_id": "p4",
        "payment_entity_id": "pay4",
        "company_id": "c1",
        "decision": "revoked",
        "invoice_entity_id": None,
        "allocated_amount": None,
        "source_name": "alfa",
        "revokes_decision_id": "d4",
    }
    proposals, decisions, payments = _unzip([confirmed])
    report = score_cases(proposals, decisions + [revoked], payments)
    assert report["human_cases"] == 0
    assert report["revocations"] == 1
    assert report["true_positive"] == 0

    synthetic = case(
        5,
        "prova",
        "c1",
        proposal={"label_origin": "synthetic"},
        decision={"source_name": "prova"},
    )
    synthetic_payment = {
        "id": "pay5",
        "company_id": "c1",
        "source_name": "prova",
        "label_origin": "synthetic",
    }
    proposals, decisions, _payments = _unzip([synthetic])
    synthetic_revoke = {
        "id": "rev-syn",
        "proposal_id": "p5",
        "payment_entity_id": "pay5",
        "decision": "revoked",
        "source_name": "prova",
        "revokes_decision_id": "d5",
    }
    report = score_cases(proposals, decisions + [synthetic_revoke], [synthetic_payment])
    assert report["human_cases"] == 0
    assert report["revocations"] == 0
    assert report["payments"] == 0
    assert report["label"] == UNMEASURED

    lonely = {
        "id": "pay-open",
        "company_id": "c1",
        "source_name": "alfa",
        "label_origin": "rule",
    }
    report = score_cases([], [], [lonely])
    assert report["unmatched_share"] == 1
    assert report["unmatched_payments"] == 1
    assert report["recall_label"] == RECALL_LABEL
    assert report["unmatched_label"] == UNMATCHED_LABEL

    reviewed = case(6, "alfa", "c1")
    proposals, decisions, payments = _unzip([reviewed])
    payments.append(
        {
            "id": "pay-open-2",
            "company_id": "c1",
            "source_name": "alfa",
            "label_origin": "rule",
        }
    )
    report = score_cases(proposals, decisions, payments)
    assert report["recall"] == 1
    assert report["unmatched_share"] == 0.5
    assert report["recall_label"] == RECALL_LABEL
    assert report["unmatched_label"] == UNMATCHED_LABEL
    empty = score_cases([], [], [])
    assert empty["recall"] is None
    assert empty["unmatched_share"] is None
    assert empty["recall_label"] == RECALL_LABEL
    assert empty["unmatched_label"] == UNMATCHED_LABEL


def _unzip(rows):
    proposals, decisions, payments = [], [], []
    for proposal, decision, payment in rows:
        proposals.append(proposal)
        decisions.append(decision)
        payments.append(payment)
    return proposals, decisions, payments


def test_demo_numbers_and_order_process_stay(client: TestClient):
    login(client, "mapper")
    load_demo()
    login(client, "approver")
    catalog = client.get("/api/ontology").json()
    order_process = _process(catalog, "order_exception")
    shortage = next(step for step in order_process["steps"] if step["id"] == "shortage")
    orders = client.get("/api/orders").json()["orders"]
    order = next(item for item in orders if item["business_key"] == "1024")
    other = next(item for item in orders if item["business_key"] == "1025")
    assert order["shortage_qty"] == 80
    assert order["margin"] == 0.6667
    assert other["amount"] == 1000
    detail = client.get(f"/api/orders/{order['id']}").json()
    line = next(item for item in detail["lines"] if item["product"] == "ART-X")
    assert line["stock"]["available"] == 420
    inbox = client.get("/api/inbox").json()["recommendations"]
    assert len(inbox) == 1
    assert inbox[0]["requires_cfo"] is True
    assert "80" in inbox[0]["problem"]
    assert inbox[0]["kind"] == "order_exception"
    assert inbox[0]["process"] == {
        "id": order_process["id"],
        "label": order_process["label"],
        "package": order_process["package"],
    }
    assert inbox[0]["step"] == {"id": shortage["id"], "label": shortage["label"]}
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.entities WHERE type = 'Invoice'").fetchone()["n"] == 0
        keys = conn.execute(
            "SELECT business_key FROM app.entities WHERE type = 'Customer' AND valid_to IS NULL"
        ).fetchall()
        assert keys
        assert all(not row["business_key"].startswith("IT") for row in keys)
        assert conn.execute(
            "SELECT count(*) AS n FROM app.graph_edges WHERE rel = 'sameOrganization' AND valid_to IS NULL"
        ).fetchone()["n"] == 0
    approved = decide(client, inbox[0])
    assert approved.status_code == 200, approved.text
    executed = client.post(f"/api/recommendations/{inbox[0]['id']}/execute")
    assert executed.status_code == 200, executed.text
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM app.sandbox_documents WHERE kind = 'purchase_order'"
        ).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT count(*) AS n FROM app.graph_edges WHERE rel = 'triggered' AND valid_to IS NULL"
        ).fetchone()["n"] == 1


def test_synthetic_cash_is_proposed_isolated_and_not_measured(client: TestClient):
    company_a = give_company("mapper", "Alfa prova")
    _assign("approver", company_a)
    login(client, "mapper")
    invoices = _upload(client, "fatturapa", "fatture_prova.xml", (SYNTHETIC / "fatture_prova.xml").read_bytes())
    assert len(invoices["invoices"]) == 4
    assert len(invoices["credit_notes"]) == 1
    payments = _upload(client, "bank", "incassi_prova.csv", (SYNTHETIC / "incassi_prova.csv").read_bytes())
    assert len(payments["payments"]) == 4
    assert len(payments["proposals"]) == 3
    refused = client.post(
        "/api/ingest/fatturapa",
        files={"file": ("nuda.xml", b"<FatturaElettronica></FatturaElettronica>", "application/xml")},
        data={"source_name": "nuda", "source_class": "external"},
    )
    assert refused.status_code == 422
    assert "synthetic" in refused.json()["detail"]

    login(client, "operator")
    hidden = client.get(f"/api/invoices/{invoices['invoices'][0]}")
    assert hidden.status_code == 404
    catalog = client.get("/api/ontology").json()
    assert catalog["company_id"] is None
    assert catalog["extensions"] == []
    sales = next(item for item in catalog["packages"] if item["name"] == "sales")
    assert sales["depends_on"] == ["manufacturing"]
    blocked = client.post(
        "/api/payment-proposals/00000000-0000-0000-0000-000000000001/confirm",
        json={"decision": "confirm", "evidence_hash": "a" * 64},
    )
    assert blocked.status_code == 403

    login(client, "approver")
    naked = client.post(
        "/api/payment-proposals/00000000-0000-0000-0000-000000000001/confirm",
        json={"decision": "confirm", "evidence_hash": "a" * 64},
    )
    # approver belongs to Alfa, so a missing id is 404, not the no-company message
    assert naked.status_code == 404
    catalog = client.get("/api/ontology").json()
    cash = _process(catalog, "order_to_cash")
    proposed_step = next(step for step in cash["steps"] if step["id"] == "match_proposed")
    inbox = client.get("/api/inbox").json()["recommendations"]
    matches = [item for item in inbox if item["kind"] == "payment_match"]
    assert len(matches) == 3
    for item in matches:
        assert item["process"] == {
            "id": cash["id"],
            "label": cash["label"],
            "package": cash["package"],
        }
        assert item["step"] == {"id": proposed_step["id"], "label": proposed_step["label"]}
        assert item["confidence_rule"] == RULE
        assert item["criteria"] == {"amount": True, "customer": True, "reference": True}
        assert item["confidence_method"] == "rule"
    full = next(item for item in matches if "FT-TEST-100" in item["problem"])
    invoice = client.get(f"/api/invoices/{full['invoice_id']}").json()
    assert invoice["status"] == "issued"
    assert invoice["process"]["id"] == "order_to_cash"
    assert invoice["step"]["id"] == "invoiced"
    assert any(fact["property"] == "amount" and fact["value"] == 122 for fact in invoice["facts"])
    assert any(fact["property"] == "due_at" and fact["value"] == "2026-04-01" for fact in invoice["facts"])
    assert len(invoice["lines"]) == 1
    payment = client.get(f"/api/payments/{full['payment_id']}").json()
    _assert_personal_cash_masked(invoice, payment)
    login(client, "mapper")
    mapper_invoice = client.get(f"/api/invoices/{full['invoice_id']}").json()
    mapper_payment = client.get(f"/api/payments/{full['payment_id']}").json()
    mapper_inbox = client.get("/api/inbox").json()
    _assert_personal_cash_masked(mapper_invoice, mapper_payment)
    hidden_text = json.dumps(
        {
            "approver_invoice": invoice,
            "approver_payment": payment,
            "mapper_invoice": mapper_invoice,
            "mapper_payment": mapper_payment,
            "inbox": mapper_inbox,
        },
        default=str,
    )
    assert IBAN not in hidden_text
    assert "Cliente Prova Alfa" not in hidden_text
    assert "Saldo FT-TEST-100" not in hidden_text
    login(client, "approver")
    with connect() as conn:
        stored = conn.execute(
            """
            SELECT value_cleartext #>> '{}' AS clear
            FROM app.facts
            WHERE entity_id = %s AND property = 'iban' AND valid_to IS NULL
            """,
            (full["payment_id"],),
        ).fetchone()
    assert stored["clear"] == IBAN
    with app_connect() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT value_cleartext FROM app.facts")
        conn.rollback()

    confirmed = client.post(
        f"/api/payment-proposals/{full['id']}/confirm",
        json={"decision": "confirm", "evidence_hash": full["evidence_hash"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert _status(full["invoice_id"]) == "issued"
    again = client.post(
        f"/api/payment-proposals/{full['id']}/confirm",
        json={"decision": "confirm", "evidence_hash": full["evidence_hash"]},
    )
    assert again.status_code == 409
    revoked = client.post(
        f"/api/payment-decisions/{confirmed.json()['id']}/revoke",
        json={"reason": "annullo di prova"},
    )
    assert revoked.status_code == 200, revoked.text

    split = next(item for item in matches if "FT-TEST-301" in item["problem"])
    corrected = client.post(
        f"/api/payment-proposals/{split['id']}/correct",
        json={
            "decision": "correct",
            "evidence_hash": split["evidence_hash"],
            "invoice_id": split["invoice_id"],
            "allocated_amount": 10,
            "reason": "importo diverso",
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["failed_criteria"] == ["amount"]
    assert _status(split["invoice_id"]) == "issued"

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id::text, business_key FROM app.entities
            WHERE type = 'Invoice' AND company_id = %s AND valid_to IS NULL
            """,
            (company_a,),
        ).fetchall()
        partial_payment = conn.execute(
            """
            SELECT id::text FROM app.entities
            WHERE type = 'Payment' AND business_key = 'INC-200' AND company_id = %s
            """,
            (company_a,),
        ).fetchone()["id"]
    numbers = {row["business_key"]: row["id"] for row in rows}
    assert partial_payment in payments["unmatched"]
    assert len(payments["unmatched"]) == 1
    assigned = client.post(
        f"/api/payments/{partial_payment}/correct",
        json={
            "invoice_id": numbers["FT-TEST-200"],
            "allocated_amount": 40,
            "reason": "acconto deciso da una persona",
        },
    )
    assert assigned.status_code == 200, assigned.text
    assert _status(numbers["FT-TEST-200"]) == "issued"
    assert "amount" in assigned.json()["failed_criteria"]

    company_b = give_company("admin", "Beta prova")
    login(client, "admin")
    other = _upload(client, "fatturapa", "fatture_prova.xml", (SYNTHETIC / "fatture_prova.xml").read_bytes())
    assert client.get(f"/api/invoices/{invoices['invoices'][0]}").status_code == 404
    login(client, "mapper")
    assert client.get(f"/api/invoices/{other['invoices'][0]}").status_code == 404
    own = client.get("/api/inbox").json()["recommendations"]
    mapper_ids = {item["invoice_id"] for item in own if item.get("kind") == "payment_match"}
    assert other["invoices"][0] not in mapper_ids
    login(client, "admin")
    admin_inbox = client.get("/api/inbox").json()["recommendations"]
    assert all(item.get("invoice_id") != invoices["invoices"][0] for item in admin_inbox)

    with connect() as conn:
        model = load_merged()
        write_supplier(
            conn,
            model,
            company_id=company_a,
            name="Fornitore Prova Alfa",
            vat="IT00000000001",
            source="fatture_prova",
            source_class="external",
        )
        write_supplier(
            conn,
            model,
            company_id=company_b,
            name="Fornitore Prova Beta",
            vat="IT00000000001",
            source="fatture_prova",
            source_class="external",
        )
        project_graph(conn)
        edges = conn.execute(
            """
            SELECT c.company_id::text AS customer_company, s.company_id::text AS supplier_company,
                   c.business_key
            FROM app.graph_edges g
            JOIN app.entities c ON c.id = g.from_id
            JOIN app.entities s ON s.id = g.to_id
            WHERE g.rel = 'sameOrganization' AND g.valid_to IS NULL
            """
        ).fetchall()
        conn.commit()
    assert len(edges) == 2
    assert {row["customer_company"] for row in edges} == {company_a, company_b}
    assert all(row["customer_company"] == row["supplier_company"] for row in edges)
    assert all(row["business_key"].startswith("id:") for row in edges)
    assert all("00000000001" not in row["business_key"] for row in edges)

    login(client, "approver")
    measured = client.get("/api/payment-match/measure").json()
    assert measured["label"] == UNMEASURED
    assert measured["provisional"] is True
    assert measured["human_cases"] == 0
    assert measured["revocations"] == 0
    assert measured["scope"] == "company"
    assert measured["decides_rule"] is False
    assert measured["recall_label"] == RECALL_LABEL
    assert measured["unmatched_label"] == UNMATCHED_LABEL
    with connect() as conn:
        fresh = measure_payment_matches(conn)
    assert fresh["label"] == UNMEASURED


def test_approver_without_a_company_cannot_decide(client: TestClient):
    login(client, "approver")
    response = client.post(
        "/api/payment-proposals/00000000-0000-0000-0000-000000000001/confirm",
        json={"decision": "confirm", "evidence_hash": "a" * 64},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == USER_WITHOUT_COMPANY
    ontology = client.get("/api/ontology")
    assert ontology.status_code == 200
    body = ontology.json()
    assert body["company_id"] is None
    assert body["extensions"] == []


def _create_company(name: str, *, demo: bool = False, extension: str | None = None) -> str:
    args = ["company", "create-demo" if demo else "create", "--name", name]
    if extension:
        args.extend(["--extension", extension])
    created = StringIO()
    with redirect_stdout(created):
        main(args)
    return json.loads(created.getvalue().strip().splitlines()[-1])["company_id"]


def _code(path: Path) -> str:
    script = path.read_text(encoding="utf-8")
    return "\n".join(line.split("--", 1)[0] for line in script.splitlines())


def test_ontology_names_transitions_and_one_company_extension(client: TestClient):
    login(client, "operator")
    catalog = client.get("/api/ontology").json()
    assert catalog["extensions"] == []
    assert "sales@1.0.0" in catalog["version"]
    invoice = next(item for item in catalog["types"] if item["name"] == "Invoice")
    assert invoice["label"] == "Fattura"
    amount = next(field for field in invoice["fields"] if field["name"] == "amount")
    assert amount["label"] == "Importo fattura"
    payment = next(item for item in catalog["types"] if item["name"] == "Payment")
    assert payment["label"] == "Incasso"
    causale = next(field for field in payment["fields"] if field["name"] == "causale")
    assert causale["label"] == "Causale"
    assert causale["classification"] == "confidential"
    for type_name, field_name in (
        ("Payment", "iban"),
        ("Payment", "causale"),
        ("Payment", "counterparty_name"),
        ("Invoice", "customer_name"),
    ):
        field = next(
            item
            for item in next(entry for entry in catalog["types"] if entry["name"] == type_name)["fields"]
            if item["name"] == field_name
        )
        assert field["classification"] == "confidential"
    for type_def in catalog["types"]:
        assert type_def["label"]
        for field in type_def["fields"]:
            assert field["label"]
    cash = _process(catalog, "order_to_cash")
    step_ids = [step["id"] for step in cash["steps"]]
    assert step_ids[0] == "order_placed"
    assert step_ids[1] == "fulfilled"
    assert cash["transitions"] == [
        {"from": step_ids[index], "to": step_ids[index + 1]} for index in range(len(step_ids) - 1)
    ]
    assert _process(catalog, "order_exception")["transitions"] == []
    assert {
        "process": "order_to_cash",
        "from": "order_placed",
        "to": "fulfilled",
    } in catalog["step_transitions"]

    extended = _create_company("Con commessa", extension="customer_a")
    plain = _create_company("Senza commessa")
    _assign("operator", extended)
    _assign("mapper", plain)
    login(client, "operator")
    own = client.get("/api/ontology").json()
    assert own["extensions"] == [{"package": "customer_a", "version": "1.0.0", "types": ["Order"]}]
    assert own["company_id"] == extended
    login(client, "mapper")
    other = client.get("/api/ontology").json()
    assert other["extensions"] == []
    assert other["company_id"] == plain


def test_measure_is_scoped_and_skips_demo_companies(client: TestClient):
    real = give_company("mapper", "Reale")
    other = _create_company("Altra")
    demo = _create_company("Dimostrazione", demo=True)
    _assign("approver", demo)
    model = load_merged()
    with connect() as conn:
        payments = {}
        for company_id, source in (
            (real, "fonte-reale"),
            (other, "fonte-altra"),
            (demo, "fonte-demo"),
        ):
            payments[source] = write_payment(
                conn,
                model,
                company_id=company_id,
                source_key=source,
                direction="incoming",
                paid_at="2026-04-01",
                amount=Decimal("10.00"),
                iban=IBAN,
                causale="prova",
                counterparty_name="Cliente Prova",
                counterparty_vat="IT00000000001",
                reference="FT-1",
                source=source,
                source_class="external",
                label_origin="rule",
            )
        for source, company_id in (
            ("fonte-reale", real),
            ("fonte-altra", other),
            ("fonte-demo", demo),
        ):
            conn.execute(
                """
                INSERT INTO app.payment_decisions (
                  id, proposal_id, payment_entity_id, company_id, decision,
                  actor, actor_role, failed_criteria, reason, source_name
                )
                VALUES (
                  %s, NULL, %s, %s, 'correct',
                  'approver', 'approver', '["amount","customer","reference"]'::jsonb,
                  'controllo di prova', %s
                )
                """,
                (str(uuid.uuid4()), payments[source], company_id, source),
            )
        conn.commit()

    login(client, "admin")
    admin = client.get("/api/payment-match/measure").json()
    admin_text = json.dumps(admin)
    assert admin["scope"] == "all"
    assert admin["decides_rule"] is True
    assert admin["source_names"] == ["fonte-altra", "fonte-reale"]
    assert admin["human_cases"] == 2
    assert admin["payments"] == 2
    assert admin["unmatched_payments"] == 2
    assert admin["unmatched_share"] == 1
    assert "fonte-demo" not in admin_text

    login(client, "mapper")
    own = client.get("/api/payment-match/measure").json()
    own_text = json.dumps(own)
    assert own["scope"] == "company"
    assert own["decides_rule"] is False
    assert own["source_names"] == ["fonte-reale"]
    assert own["human_cases"] == 1
    assert own["payments"] == 1
    assert own["unmatched_share"] == 1
    assert "fonte-altra" not in own_text
    assert "fonte-demo" not in own_text

    login(client, "approver")
    hidden = client.get("/api/payment-match/measure").json()
    hidden_text = json.dumps(hidden)
    assert hidden["scope"] == "company"
    assert hidden["decides_rule"] is False
    assert hidden["human_cases"] == 0
    assert hidden["payments"] == 0
    assert hidden["unmatched_share"] is None
    assert hidden["source_names"] == []
    assert "fonte-reale" not in hidden_text
    assert "fonte-altra" not in hidden_text
    assert "fonte-demo" not in hidden_text

    login(client, "operator")
    empty = client.get("/api/payment-match/measure").json()
    assert empty["scope"] == "company"
    assert empty["decides_rule"] is False
    assert empty["human_cases"] == 0
    assert empty["payments"] == 0
    assert empty["source_names"] == []
    assert empty["recall_label"] == RECALL_LABEL
    assert empty["unmatched_label"] == UNMATCHED_LABEL


def test_migration_0017_does_not_rewrite_inbox_rows(client: TestClient):
    code = _code(ROOT / "db/migrations/0017_order_to_cash.sql")
    assert re.search(r"\bupdate\s+app\.recommendations\b", code, re.I) is None
    extension = _code(ROOT / "db/migrations/0018_company_extension.sql")
    assert re.search(r"\bupdate\b", extension, re.I) is None
    checks = _code(ROOT / "db/migrations/0019_payment_company_checks.sql")
    assert re.search(r"\bupdate\b", checks, re.I) is None
    give_company("mapper", "Lettura")
    login(client, "mapper")
    load_demo()
    with connect() as conn:
        conn.execute("UPDATE app.recommendations SET process_id = NULL, step_id = NULL")
        conn.commit()
    catalog = client.get("/api/ontology").json()
    order_process = _process(catalog, "order_exception")
    shortage = next(step for step in order_process["steps"] if step["id"] == "shortage")
    inbox = client.get("/api/inbox").json()["recommendations"]
    assert len(inbox) == 1
    assert "80" in inbox[0]["problem"]
    assert inbox[0]["process"] == {
        "id": order_process["id"],
        "label": order_process["label"],
        "package": order_process["package"],
    }
    assert inbox[0]["step"] == {"id": shortage["id"], "label": shortage["label"]}


def _assert_personal_cash_masked(invoice: dict, payment: dict) -> None:
    by_invoice = {fact["property"]: fact for fact in invoice["facts"]}
    by_payment = {fact["property"]: fact for fact in payment["facts"]}
    expected = {
        "customer_name": by_invoice,
        "iban": by_payment,
        "causale": by_payment,
        "counterparty_name": by_payment,
    }
    for name, facts in expected.items():
        fact = facts[name]
        assert fact["classification"] == "confidential"
        assert fact["value"] == MASK
        assert fact.get("masked") is True


def _entity(conn, type_name: str, key: str, company_id: str) -> str:
    entity_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.entities (id, type, business_key, company_id)
        VALUES (%s, %s, %s, %s)
        """,
        (entity_id, type_name, key, company_id),
    )
    return entity_id


def _insert_proposal(conn, company_id: str, payment_id: str, invoice_id: str, allocation_id: str) -> str:
    proposal_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.payment_proposals (
          id, company_id, payment_entity_id, invoice_entity_id, allocation_entity_id,
          match_group, source_name, label_origin, criteria, score, confidence_method,
          confidence_rule, allocated_amount, evidence, evidence_hash, process_id, step_id
        )
        VALUES (
          %s, %s, %s, %s, %s,
          %s, 'fonte-vincolo', 'rule', '{}'::jsonb, 1, 'rule',
          %s, 10, '[]'::jsonb, %s, 'order_to_cash', 'match_proposed'
        )
        """,
        (
            proposal_id,
            company_id,
            payment_id,
            invoice_id,
            allocation_id,
            str(uuid.uuid4()),
            RULE,
            "a" * 64,
        ),
    )
    return proposal_id


def _insert_decision(
    conn,
    *,
    company_id: str,
    proposal_id: str,
    payment_id: str,
    invoice_id: str | None,
    decision: str,
    reason: str,
    revokes: str | None = None,
) -> str:
    decision_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.payment_decisions (
          id, proposal_id, payment_entity_id, company_id, decision, actor, actor_role,
          invoice_entity_id, failed_criteria, reason, source_name, revokes_decision_id
        )
        VALUES (%s, %s, %s, %s, %s, 'approver', 'approver', %s, '[]'::jsonb, %s, 'fonte-vincolo', %s)
        """,
        (decision_id, proposal_id, payment_id, company_id, decision, invoice_id, reason, revokes),
    )
    return decision_id


def test_payment_company_and_reason_are_enforced_in_the_database():
    company_a = _create_company("Vincolo Alfa")
    company_b = _create_company("Vincolo Beta")
    with connect() as conn:
        payment_a = _entity(conn, "Payment", "INC-A", company_a)
        invoice_a = _entity(conn, "Invoice", "FT-A", company_a)
        allocation_a = _entity(conn, "PaymentAllocation", "ALL-A", company_a)
        payment_b = _entity(conn, "Payment", "INC-B", company_b)
        invoice_b = _entity(conn, "Invoice", "FT-B", company_b)
        allocation_b = _entity(conn, "PaymentAllocation", "ALL-B", company_b)
        conn.commit()
        for payment_id, invoice_id, allocation_id in (
            (payment_b, invoice_a, allocation_a),
            (payment_a, invoice_b, allocation_a),
            (payment_a, invoice_a, allocation_b),
        ):
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                _insert_proposal(conn, company_a, payment_id, invoice_id, allocation_id)
            conn.rollback()
        proposal_id = _insert_proposal(conn, company_a, payment_a, invoice_a, allocation_a)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert_decision(
                conn,
                company_id=company_a,
                proposal_id=proposal_id,
                payment_id=payment_a,
                invoice_id=invoice_b,
                decision="correct",
                reason="motivo valido",
            )
        conn.rollback()
        proposal_id = _insert_proposal(conn, company_a, payment_a, invoice_a, allocation_a)
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_decision(
                conn,
                company_id=company_a,
                proposal_id=proposal_id,
                payment_id=payment_a,
                invoice_id=invoice_a,
                decision="reject",
                reason="no",
            )
        conn.rollback()
        proposal_id = _insert_proposal(conn, company_a, payment_a, invoice_a, allocation_a)
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_decision(
                conn,
                company_id=company_a,
                proposal_id=proposal_id,
                payment_id=payment_a,
                invoice_id=None,
                decision="correct",
                reason="    a",
            )
        conn.rollback()
        proposal_id = _insert_proposal(conn, company_a, payment_a, invoice_a, allocation_a)
        confirmed = _insert_decision(
            conn,
            company_id=company_a,
            proposal_id=proposal_id,
            payment_id=payment_a,
            invoice_id=invoice_a,
            decision="confirm",
            reason="",
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_decision(
                conn,
                company_id=company_a,
                proposal_id=proposal_id,
                payment_id=payment_a,
                invoice_id=None,
                decision="revoked",
                reason="x",
                revokes=confirmed,
            )
        conn.rollback()
        proposal_id = _insert_proposal(conn, company_a, payment_a, invoice_a, allocation_a)
        confirmed = _insert_decision(
            conn,
            company_id=company_a,
            proposal_id=proposal_id,
            payment_id=payment_a,
            invoice_id=invoice_a,
            decision="confirm",
            reason="",
        )
        _insert_decision(
            conn,
            company_id=company_a,
            proposal_id=proposal_id,
            payment_id=payment_a,
            invoice_id=invoice_a,
            decision="correct",
            reason="abcde",
        )
        conn.execute("SAVEPOINT one_revocation")
        try:
            conn.execute("ALTER TABLE app.payment_decisions DISABLE TRIGGER payment_decisions_company")
            _insert_decision(
                conn,
                company_id=company_a,
                proposal_id=proposal_id,
                payment_id=payment_a,
                invoice_id=None,
                decision="revoked",
                reason="annullo di prova",
                revokes=confirmed,
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                _insert_decision(
                    conn,
                    company_id=company_a,
                    proposal_id=proposal_id,
                    payment_id=payment_a,
                    invoice_id=None,
                    decision="revoked",
                    reason="secondo annullo",
                    revokes=confirmed,
                )
        finally:
            conn.execute("ROLLBACK TO SAVEPOINT one_revocation")
        index = conn.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'app' AND indexname = 'payment_decisions_one_revocation_idx'
            """
        ).fetchone()
    assert index is not None
    assert "UNIQUE" in index["indexdef"].upper()
