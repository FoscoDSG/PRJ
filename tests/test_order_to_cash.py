"""Ordine-incasso: dati di prova, regole, isolamento e misura.

I file in fixtures/synthetic dichiarano label_origin synthetic.
I casi della misura costruiti in memoria sono etichette di test, non fatture vere.
"""

from __future__ import annotations

from decimal import Decimal
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
    UNMEASURED,
    measure_payment_matches,
    score_cases,
)
from services.cash.documents import write_supplier
from services.companies import NO_COMPANY_UPLOAD
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
    assert any(item["package"] == "customer_a" for item in catalog["extensions"])
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
    assert any(fact["property"] == "iban" and fact["value"] == MASK for fact in payment["facts"])
    assert any(fact["property"] == "causale" and fact["value"] == MASK for fact in payment["facts"])
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
    assert response.json()["detail"] == NO_COMPANY_UPLOAD
    ontology = client.get("/api/ontology")
    assert ontology.status_code == 200
    assert ontology.json()["company_id"] is None
