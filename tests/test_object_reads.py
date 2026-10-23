"""Letture nuove: oggetti per azienda, residuo, aperto/chiuso, maschera.

La pagina non inventa «riservato» e non deduce aperto/chiuso dallo stato.
Il residuo sottrae solo note di credito e decisioni umane ancora valide.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from services.db import connect
from services.agent.payment_measure import score_cases
from services.disposition import ORDER_DECIDABLE, PAYMENT_DECIDABLE, card_disposition
from services.pii import MASK, RESERVED, ROLE_HIDDEN, note_clear_personal_view
from tests.test_flow import decide, give_company, load_demo, login
from tests.test_order_to_cash import SYNTHETIC, _assign, _status, _upload

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _objects(client: TestClient, type_name: str) -> list[dict]:
    response = client.get("/api/objects", params={"type": type_name})
    assert response.status_code == 200, response.text
    return response.json()["objects"]


def _by_key(client: TestClient, type_name: str) -> dict[str, dict]:
    return {item["business_key"]: item for item in _objects(client, type_name)}


def _field(obj: dict, name: str) -> dict:
    return next(item for item in obj["fields"] if item["name"] == name)


def _remaining(client: TestClient, number: str) -> int | float:
    return _by_key(client, "Invoice")[number]["remaining"]


def test_card_disposition_is_the_same_rule_for_every_queue():
    """Open only when that queue can still accept a decision today."""
    for status in ("detected", "proposed", "modified"):
        assert card_disposition(status, ORDER_DECIDABLE) == "open"
    for status in ("approved", "rejected", "revoked", "executed", "dry_run_ok"):
        assert card_disposition(status, ORDER_DECIDABLE) == "closed"
    assert card_disposition("proposed", PAYMENT_DECIDABLE) == "open"
    for status in ("confirmed", "corrected", "rejected", "revoked"):
        assert card_disposition(status, PAYMENT_DECIDABLE) == "closed"
    # The same status goes through the same function for both queues.
    assert card_disposition("proposed", ORDER_DECIDABLE) == card_disposition("proposed", PAYMENT_DECIDABLE)


def test_unknown_type_is_absent_and_a_company_without_one_sees_nothing(client: TestClient):
    login(client, "operator")
    empty = client.get("/api/objects", params={"type": "Invoice"})
    assert empty.status_code == 200, empty.text
    assert empty.json()["objects"] == []
    for type_name in ("Foo", "invoice", ""):
        missing = client.get("/api/objects", params={"type": type_name})
        assert missing.status_code == 404, missing.text
        assert missing.json()["detail"] == "Tipo assente"
    assert client.get("/api/objects/00000000-0000-0000-0000-000000000001").status_code == 404

    give_company("mapper", "Tipi noti")
    login(client, "mapper")
    for type_name in ("Foo", "invoice", ""):
        missing = client.get("/api/objects", params={"type": type_name})
        assert missing.status_code == 404
        assert missing.json()["detail"] == "Tipo assente"
    for type_name in ("Order", "Invoice", "Payment"):
        assert client.get("/api/objects", params={"type": type_name}).status_code == 200


def test_two_payments_balance_mask_and_the_other_companys_number(client: TestClient):
    company_a = give_company("mapper", "Alfa letture")
    _assign("approver", company_a)
    login(client, "mapper")
    _upload(client, "fatturapa", "fatture_prova.xml", (SYNTHETIC / "fatture_prova.xml").read_bytes())
    _upload(client, "bank", "incassi_prova.csv", (SYNTHETIC / "incassi_prova.csv").read_bytes())

    invoices = _by_key(client, "Invoice")
    assert set(invoices) == {"FT-TEST-100", "FT-TEST-200", "FT-TEST-301", "FT-TEST-302"}
    assert "history" not in invoices["FT-TEST-100"]
    assert invoices["FT-TEST-100"]["remaining"] == 122
    assert invoices["FT-TEST-200"]["remaining"] == 90
    assert invoices["FT-TEST-301"]["remaining"] == 80
    amount = _field(invoices["FT-TEST-100"], "amount")
    assert amount["value"] == 122
    assert amount["masked"] is False
    assert amount["value"] != RESERVED
    for prop in ("customer_name", "customer_vat"):
        field = _field(invoices["FT-TEST-100"], prop)
        assert field["value"] == RESERVED
        assert field["masked"] is True

    detail = client.get(f"/api/objects/{invoices['FT-TEST-100']['id']}")
    assert detail.status_code == 200, detail.text
    assert any("emessa" in item["label"] for item in detail.json()["history"])
    invoice = client.get(f"/api/invoices/{invoices['FT-TEST-100']['id']}").json()
    assert invoice["remaining"] == 122
    assert invoice["status"] == "issued"
    assert any("emessa" in item["label"] for item in invoice["history"])

    payment = _by_key(client, "Payment")["INC-100"]
    for prop in ("iban", "causale", "counterparty_name", "counterparty_vat"):
        field = _field(payment, prop)
        assert field["value"] == RESERVED
        assert field["masked"] is True
        assert field["value"] != MASK
    paid = _field(payment, "amount")
    assert paid["value"] == 122
    assert paid["masked"] is False
    payment_detail = client.get(f"/api/payments/{payment['id']}").json()
    assert payment_detail["history"]
    assert "Cliente Prova Alfa" not in str(payment)
    assert "IT00TEST0000000000000000000" not in str(payment)

    login(client, "approver")
    approver_payment = _by_key(client, "Payment")["INC-100"]
    assert _field(approver_payment, "iban")["value"] == RESERVED
    assert _field(approver_payment, "amount")["value"] == 122
    inbox = client.get("/api/inbox").json()["recommendations"]
    matches = [item for item in inbox if item["kind"] == "payment_match"]
    full = next(item for item in matches if item["invoice"]["number"] == "FT-TEST-100")
    assert full["disposition"] == "open"
    assert full["payment"] == {
        "number": "INC-100",
        "paid_at": "2026-04-02",
        "amount": 122,
        "direction": "incoming",
        "label": "Incasso",
    }
    assert full["invoice"]["number"] == "FT-TEST-100"
    assert full["invoice"]["amount"] == 122
    assert full["invoice"]["due_at"] == "2026-04-01"
    assert full["allocation"]["amount"] == 122
    assert full["allocation"]["remaining"] == 122

    login(client, "mapper")
    mapper_full = next(
        item
        for item in client.get("/api/inbox").json()["recommendations"]
        if item.get("kind") == "payment_match" and item["invoice"]["number"] == "FT-TEST-100"
    )
    assert mapper_full["allocated_amount"] == 122
    assert mapper_full["allocation"]["amount"] == 122
    assert mapper_full["allocation"]["remaining"] == 122

    login(client, "approver")
    split = next(item for item in matches if item["invoice"]["number"] == "FT-TEST-301")
    confirmed = client.post(
        f"/api/payment-proposals/{split['id']}/confirm",
        json={"decision": "confirm", "evidence_hash": split["evidence_hash"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert _status(split["invoice_id"]) == "issued"
    assert _remaining(client, "FT-TEST-301") == 0
    closed = next(
        item
        for item in client.get("/api/inbox").json()["recommendations"]
        if item["id"] == split["id"]
    )
    assert closed["disposition"] == "closed"
    revoked = client.post(
        f"/api/payment-decisions/{confirmed.json()['id']}/revoke",
        json={"reason": "annullo di prova"},
    )
    assert revoked.status_code == 200, revoked.text
    assert _status(split["invoice_id"]) == "issued"
    assert _remaining(client, "FT-TEST-301") == 80
    reopened = next(
        item
        for item in client.get("/api/inbox").json()["recommendations"]
        if item["id"] == split["id"]
    )
    assert reopened["disposition"] == "open"
    assert reopened["status"] == "proposed"
    again_decision = client.post(
        f"/api/payment-proposals/{split['id']}/confirm",
        json={"decision": "confirm", "evidence_hash": split["evidence_hash"]},
    )
    assert again_decision.status_code == 200, again_decision.text
    decided_again = next(
        item
        for item in client.get("/api/inbox").json()["recommendations"]
        if item["id"] == split["id"]
    )
    assert decided_again["disposition"] == "closed"

    with connect() as conn:
        partial_payment = conn.execute(
            """
            SELECT id::text FROM app.entities
            WHERE type = 'Payment' AND business_key = 'INC-200' AND company_id = %s
            """,
            (company_a,),
        ).fetchone()["id"]
    first = client.post(
        f"/api/payments/{partial_payment}/correct",
        json={
            "invoice_number": "FT-TEST-200",
            "allocated_amount": 40,
            "reason": "primo acconto sulla stessa fattura",
        },
    )
    assert first.status_code == 200, first.text
    assert _remaining(client, "FT-TEST-200") == 50
    assert _status(invoices["FT-TEST-200"]["id"]) == "issued"
    second = client.post(
        f"/api/payment-proposals/{full['id']}/correct",
        json={
            "decision": "correct",
            "evidence_hash": full["evidence_hash"],
            "invoice_number": "FT-TEST-200",
            "allocated_amount": 20,
            "reason": "secondo incasso sulla stessa fattura",
        },
    )
    assert second.status_code == 200, second.text
    assert _remaining(client, "FT-TEST-200") == 30
    assert _remaining(client, "FT-TEST-100") == 122
    assert _status(invoices["FT-TEST-200"]["id"]) == "issued"
    drop_second = client.post(
        f"/api/payment-decisions/{second.json()['id']}/revoke",
        json={"reason": "annullo il secondo incasso"},
    )
    assert drop_second.status_code == 200, drop_second.text
    assert _remaining(client, "FT-TEST-200") == 50
    drop_first = client.post(
        f"/api/payment-decisions/{first.json()['id']}/revoke",
        json={"reason": "annullo il primo incasso"},
    )
    assert drop_first.status_code == 200, drop_first.text
    assert _remaining(client, "FT-TEST-200") == 90
    assert _status(invoices["FT-TEST-200"]["id"]) == "issued"

    company_b = give_company("admin", "Beta letture")
    login(client, "admin")
    _upload(client, "fatturapa", "fatture_prova.xml", (SYNTHETIC / "fatture_prova.xml").read_bytes())
    foreign = _by_key(client, "Invoice")
    assert set(foreign) == set(invoices)
    assert foreign["FT-TEST-100"]["id"] != invoices["FT-TEST-100"]["id"]
    assert foreign["FT-TEST-200"]["remaining"] == 90
    assert client.get(f"/api/objects/{invoices['FT-TEST-100']['id']}").status_code == 404
    with connect() as conn:
        twins = conn.execute(
            """
            SELECT company_id::text AS company_id, id::text
            FROM app.entities
            WHERE type = 'Invoice' AND business_key = 'FT-TEST-100' AND valid_to IS NULL
            """
        ).fetchall()
    assert {row["company_id"] for row in twins} == {company_a, company_b}
    assert len({row["id"] for row in twins}) == 2

    login(client, "approver")
    assert client.get(f"/api/objects/{foreign['FT-TEST-100']['id']}").status_code == 404
    assert _remaining(client, "FT-TEST-100") == 122
    crossed = client.post(
        f"/api/payments/{partial_payment}/correct",
        json={
            "invoice_id": foreign["FT-TEST-100"]["id"],
            "allocated_amount": 10,
            "reason": "fattura dell'altra azienda",
        },
    )
    assert crossed.status_code == 404
    assert _remaining(client, "FT-TEST-100") == 122
    both = client.post(
        f"/api/payments/{partial_payment}/correct",
        json={
            "invoice_id": invoices["FT-TEST-200"]["id"],
            "invoice_number": "FT-TEST-200",
            "allocated_amount": 10,
            "reason": "numero e id insieme",
        },
    )
    assert both.status_code == 422
    neither = client.post(
        f"/api/payments/{partial_payment}/correct",
        json={"allocated_amount": 10, "reason": "manca il numero"},
    )
    assert neither.status_code == 422
    own = client.post(
        f"/api/payments/{partial_payment}/correct",
        json={
            "invoice_number": "FT-TEST-200",
            "allocated_amount": 15,
            "reason": "solo la fattura di questa azienda",
        },
    )
    assert own.status_code == 200, own.text
    assert _remaining(client, "FT-TEST-200") == 75
    login(client, "admin")
    assert _remaining(client, "FT-TEST-200") == 90

    with connect() as conn:
        before = conn.execute("SELECT count(*) AS n FROM app.audit_log").fetchone()["n"]
        note_clear_personal_view(
            conn,
            actor="approver",
            entity_id=invoices["FT-TEST-100"]["id"],
            properties=["customer_name"],
        )
        conn.commit()
        after = conn.execute("SELECT count(*) AS n FROM app.audit_log").fetchone()["n"]
    assert before == after
    hook = (ROOT / "services" / "pii.py").read_text(encoding="utf-8")
    assert "def note_clear_personal_view" in hook
    assert "TODO" in hook


def test_order_cards_reopen_and_demo_numbers_stay(client: TestClient):
    company = give_company("mapper", "Demo oggetti")
    _assign("approver", company)
    login(client, "mapper")
    load_demo()
    found = _by_key(client, "Order")["1024"]
    assert _field(found, "shortage_qty")["value"] == 80
    assert _field(found, "amount")["value"] == 60000
    assert _field(found, "amount")["masked"] is False
    assert _field(found, "margin")["value"] == ROLE_HIDDEN
    assert _field(found, "margin")["masked"] is True

    login(client, "approver")
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
    exceptions = [item for item in inbox if item["kind"] == "order_exception"]
    assert len(exceptions) == 1
    assert exceptions[0]["disposition"] == "open"
    assert "80" in exceptions[0]["problem"]

    card = next(
        item
        for item in client.get("/api/inbox").json()["recommendations"]
        if item["kind"] == "order_exception"
    )
    assert card["disposition"] == "open"
    approved = decide(client, card)
    assert approved.status_code == 200, approved.text
    closed = next(
        item
        for item in client.get("/api/inbox").json()["recommendations"]
        if item["id"] == card["id"]
    )
    assert closed["disposition"] == "closed"
    revoked = client.post(
        f"/api/decisions/{closed['history'][0]['id']}/revoke",
        json={"reason": "annullo di prova"},
    )
    assert revoked.status_code == 200, revoked.text
    reopened = next(
        item
        for item in client.get("/api/inbox").json()["recommendations"]
        if item["id"] == card["id"]
    )
    assert reopened["disposition"] == "closed"
    assert reopened["status"] == "revoked"
    open_orders = [
        item
        for item in client.get("/api/inbox").json()["recommendations"]
        if item["kind"] == "order_exception" and item["disposition"] == "open"
    ]
    assert open_orders == []
    refused = decide(client, reopened)
    assert refused.status_code == 409

    give_company("admin", "Altra azienda")
    login(client, "admin")
    foreign = client.get("/api/objects", params={"type": "Order"}).json()["objects"]
    assert all(item["business_key"] != "1024" for item in foreign)
    still = client.get("/api/orders").json()["orders"]
    assert any(item["business_key"] == "1024" and item["shortage_qty"] == 80 for item in still)
    login(client, "approver")
    again = client.get("/api/orders").json()["orders"]
    seen = next(item for item in again if item["business_key"] == "1024")
    assert seen["shortage_qty"] == 80
    assert seen["margin"] == 0.6667


def test_a_manual_split_lowers_both_balances_and_counts_two_cases(client: TestClient):
    company = give_company("mapper", "Split manuale")
    _assign("approver", company)
    login(client, "mapper")
    _upload(client, "fatturapa", "fatture_prova.xml", (SYNTHETIC / "fatture_prova.xml").read_bytes())
    _upload(client, "bank", "incassi_prova.csv", (SYNTHETIC / "incassi_prova.csv").read_bytes())
    login(client, "approver")
    assert _remaining(client, "FT-TEST-301") == 80
    assert _remaining(client, "FT-TEST-302") == 20
    with connect() as conn:
        payment_id = conn.execute(
            """
            SELECT id::text FROM app.entities
            WHERE type = 'Payment' AND business_key = 'INC-200' AND company_id = %s
            """,
            (company,),
        ).fetchone()["id"]
    first = client.post(
        f"/api/payments/{payment_id}/correct",
        json={
            "invoice_number": "FT-TEST-301",
            "allocated_amount": 25,
            "reason": "prima parte dello stesso incasso",
        },
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/api/payments/{payment_id}/correct",
        json={
            "invoice_number": "FT-TEST-302",
            "allocated_amount": 15,
            "reason": "seconda parte dello stesso incasso",
        },
    )
    assert second.status_code == 200, second.text
    assert _remaining(client, "FT-TEST-301") == 55
    assert _remaining(client, "FT-TEST-302") == 5
    detail = client.get(f"/api/objects/{_by_key(client, 'Invoice')['FT-TEST-301']['id']}").json()
    decision = next(item for item in detail["history"] if item.get("decision") == "correct")
    assert decision["label"] == "Corretta"
    assert decision["amount"] == 25
    assert "confirm" not in decision["label"]
    assert _status(_by_key(client, "Invoice")["FT-TEST-301"]["id"]) == "issued"

    payment = {"id": "pay", "company_id": company, "source_name": "alfa", "label_origin": "rule"}
    shared = {
        "proposal_id": None,
        "payment_entity_id": "pay",
        "company_id": company,
        "decision": "correct",
        "source_name": "alfa",
        "revokes_decision_id": None,
    }
    report = score_cases(
        [],
        [
            {**shared, "id": "d1", "invoice_entity_id": "inv-a", "allocated_amount": "25.00"},
            {**shared, "id": "d2", "invoice_entity_id": "inv-b", "allocated_amount": "15.00"},
        ],
        [payment],
    )
    assert report["human_cases"] == 2
    assert report["false_negative"] == 2
    revoked = {
        **shared,
        "id": "rev",
        "decision": "revoked",
        "invoice_entity_id": None,
        "allocated_amount": None,
        "revokes_decision_id": "d1",
    }
    after = score_cases(
        [],
        [
            {**shared, "id": "d1", "invoice_entity_id": "inv-a", "allocated_amount": "25.00"},
            {**shared, "id": "d2", "invoice_entity_id": "inv-b", "allocated_amount": "15.00"},
            revoked,
        ],
        [payment],
    )
    assert after["human_cases"] == 1
    assert after["revocations"] == 1

    cancelled = client.post(
        f"/api/payment-decisions/{first.json()['id']}/revoke",
        json={"reason": "prima parte annullata"},
    )
    assert cancelled.status_code == 200, cancelled.text
    again = client.get(f"/api/objects/{_by_key(client, 'Invoice')['FT-TEST-301']['id']}").json()
    decisions = [item for item in again["history"] if item.get("kind") == "decision"]
    struck = next(item for item in decisions if item["id"] == first.json()["id"])
    undo = next(item for item in decisions if item["decision"] == "revoked")
    assert struck["revoked"] is True
    assert undo["revoked"] is False
    assert undo["revokes_decision_id"] == first.json()["id"]
    other = client.get(f"/api/objects/{_by_key(client, 'Invoice')['FT-TEST-302']['id']}").json()
    kept = next(item for item in other["history"] if item.get("id") == second.json()["id"])
    assert kept["revoked"] is False


_FREDDY_INVOICE = """<?xml version="1.0" encoding="UTF-8"?>
<!-- label_origin: synthetic -->
<FatturaElettronica>
  <FatturaElettronicaHeader>
    <CessionarioCommittente>
      <DatiAnagrafici>
        <Anagrafica><Denominazione>Cliente Prova Alfa</Denominazione></Anagrafica>
        <IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>00000000001</IdCodice></IdFiscaleIVA>
      </DatiAnagrafici>
    </CessionarioCommittente>
  </FatturaElettronicaHeader>
  <FatturaElettronicaBody>
    <DatiGenerali>
      <DatiGeneraliDocumento>
        <TipoDocumento>TD01</TipoDocumento>
        <Divisa>EUR</Divisa>
        <Data>2026-10-01</Data>
        <Numero>FT-2026-0187</Numero>
        <ImportoTotaleDocumento>940.00</ImportoTotaleDocumento>
      </DatiGeneraliDocumento>
    </DatiGenerali>
    <DatiBeniServizi>
      <DettaglioLinee>
        <NumeroLinea>1</NumeroLinea>
        <Descrizione>Voce di prova</Descrizione>
        <Quantita>1.00</Quantita>
        <PrezzoUnitario>940.00</PrezzoUnitario>
        <PrezzoTotale>940.00</PrezzoTotale>
        <AliquotaIVA>22.00</AliquotaIVA>
      </DettaglioLinee>
    </DatiBeniServizi>
  </FatturaElettronicaBody>
</FatturaElettronica>
"""

_FREDDY_PAYMENT = """# label_origin: synthetic
data,importo,iban,causale,riferimento,cliente,partita_iva,direzione,incasso
2026-10-17,940.00,IT00TEST0000000000000000000,Saldo FT-2026-0187,FT-2026-0187,Cliente Prova Alfa,IT00000000001,incoming,INC-2026-0413
"""


def test_only_the_confirmation_is_revoked_and_the_revocation_points_at_it(client: TestClient):
    company = give_company("mapper", "Storia annullata")
    _assign("approver", company)
    login(client, "mapper")
    _upload(client, "fatturapa", "fattura_0187.xml", _FREDDY_INVOICE.encode())
    uploaded = _upload(client, "bank", "incasso_0413.csv", _FREDDY_PAYMENT.encode())
    assert uploaded["proposals"], uploaded
    login(client, "approver")
    inbox = client.get("/api/inbox").json()["recommendations"]
    card = next(item for item in inbox if item.get("kind") == "payment_match")
    confirmed = client.post(
        f"/api/payment-proposals/{card['id']}/confirm",
        json={"decision": "confirm", "evidence_hash": card["evidence_hash"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    decision_id = confirmed.json()["id"]
    revoked = client.post(
        f"/api/payment-decisions/{decision_id}/revoke",
        json={"reason": "importo da correggere"},
    )
    assert revoked.status_code == 200, revoked.text

    def decisions(type_name: str, key: str) -> list[dict]:
        detail = client.get(f"/api/objects/{_by_key(client, type_name)[key]['id']}").json()
        return [item for item in detail["history"] if item.get("kind") == "decision"]

    for rows in (
        decisions("Invoice", "FT-2026-0187"),
        decisions("Payment", "INC-2026-0413"),
    ):
        closed = [item for item in rows if item["revoked"]]
        assert [item["decision"] for item in closed] == ["confirm"]
        assert closed[0]["id"] == decision_id
        undo = next(item for item in rows if item["decision"] == "revoked")
        assert undo["revoked"] is False
        assert undo["revokes_decision_id"] == decision_id


def _figures(payload) -> dict[str, set]:
    found: dict[str, set] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            if "property" in node and "value" in node:
                found.setdefault(str(node["property"]), set()).add(node["value"])
            for key, value in node.items():
                if key in {"amount", "allocated_amount", "remaining", "margin", "cost", "unit_cost", "total_cost"} and not isinstance(value, (dict, list)):
                    found.setdefault(key, set()).add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def test_operator_sees_amounts_and_not_margin_or_cost(client: TestClient):
    company = give_company("mapper", "Visibilità importi")
    _assign("operator", company)
    _assign("approver", company)
    _assign("admin", company)
    login(client, "mapper")
    load_demo()

    def bundle() -> dict:
        orders = client.get("/api/orders").json()["orders"]
        order = next(item for item in orders if item["business_key"] == "1024")
        detail = client.get(f"/api/orders/{order['id']}").json()
        objects = client.get("/api/objects", params={"type": "Order"}).json()["objects"]
        own = next(item for item in objects if item["business_key"] == "1024")
        products = client.get("/api/objects", params={"type": "Product"}).json()["objects"]
        product = next(item for item in products if item["business_key"] == "ART-X")
        inbox = client.get("/api/inbox").json()["recommendations"]
        card = next(item for item in inbox if item["kind"] == "order_exception")
        return {"order": order, "detail": detail, "object": own, "product": product, "card": card}

    login(client, "operator")
    operator = bundle()
    figures = _figures(operator)
    assert 60000 in figures["amount"]
    assert MASK not in figures["amount"]
    assert figures["margin"] == {ROLE_HIDDEN}
    assert figures["cost"] == {ROLE_HIDDEN}
    assert _field(operator["object"], "amount")["value"] == operator["order"]["amount"] == 60000
    assert _field(operator["object"], "margin")["value"] == operator["order"]["margin"] == ROLE_HIDDEN
    assert _field(operator["product"], "cost")["value"] == ROLE_HIDDEN
    cost_evidence = next(item for item in operator["card"]["evidence"] if item["property"] == "cost")
    amount_evidence = next(item for item in operator["card"]["evidence"] if item["property"] == "amount")
    assert cost_evidence["value"] == ROLE_HIDDEN
    assert amount_evidence["value"] == 60000
    vat = next(item for item in operator["card"]["evidence"] if item["property"] == "vat")
    assert vat["value"] == MASK

    for role in ("approver", "admin"):
        login(client, role)
        seen = bundle()
        figures = _figures(seen)
        assert MASK not in figures["amount"]
        assert MASK not in figures["margin"]
        assert MASK not in figures["cost"]
        assert seen["order"]["margin"] == 0.6667
        assert seen["order"]["amount"] == 60000
        assert _field(seen["object"], "margin")["value"] == 0.6667
        assert _field(seen["product"], "cost")["value"] == 40
        assert next(item["value"] for item in seen["card"]["evidence"] if item["property"] == "cost") == 40
        assert next(item["value"] for item in seen["card"]["evidence"] if item["property"] == "amount") == 60000


def test_pages_show_reserved_only_when_the_server_sends_it():
    for path in WEB.iterdir():
        if path.suffix in {".js", ".html", ".css"}:
            assert "riservato" not in path.read_text(encoding="utf-8")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'disposition !== "open"' in app or 'disposition === "closed"' in app
    assert "Già decise" in app
    assert "invoice_number:" in app
    page_js = (WEB / "escape.js").read_text(encoding="utf-8")
    assert "Residuo attuale" in page_js
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    assert "max-width: 859px" in css
    assert "overflow-x: clip" not in css
    assert css.rfind("minmax(min(100%, 260px)") < css.rfind("@media (max-width: 700px)")
    assert "grid-template-columns: 1fr" in css
    page = (WEB / "index.html").read_text(encoding="utf-8")
    assert "IEOL" in page
    assert "Oggi" in page
    assert "Oggetti" in page
    assert "Dati" in page


_OBJECT_FILTERS = r"""
const fs = require("fs");
const vm = require("vm");
const { matchesIncasso, matchesPagamento } = require("./apps/web/escape.js");

if (!matchesPagamento({ incoming: false, direction: "incoming" })) process.exit(1);
if (matchesPagamento({ incoming: true, direction: "outgoing" })) process.exit(1);
if (matchesPagamento({ direction: "outgoing" }) || matchesPagamento(null) || matchesPagamento({})) process.exit(1);
if (matchesIncasso({ incoming: false }) || matchesPagamento({ incoming: true })) process.exit(1);

const orders = [
  { id: "o1", type: "Order", business_key: "ORD-1", incoming: false },
  { id: "o2", type: "Order", business_key: "ORD-2", incoming: true },
];
const invoices = [{ id: "i1", type: "Invoice", business_key: "FT-1", incoming: false }];
const payments = [
  { id: "in", type: "Payment", business_key: "INC-1", incoming: true, direction: "outgoing" },
  { id: "out", type: "Payment", business_key: "PAG-1", incoming: false, direction: "incoming" },
  { id: "blank", type: "Payment", business_key: "OLD-1", direction: "outgoing" },
];
const ontology = {
  types: [
    { name: "Order", label: "Ordine" },
    { name: "Invoice", label: "Fattura" },
    { name: "Payment", label: "Incasso" },
    { name: "Product", label: "Prodotto" },
  ],
};

function makeElement(tag) {
  return {
    tagName: tag || "div",
    hidden: false,
    textContent: "",
    className: "",
    value: "",
    type: "",
    disabled: false,
    childNodes: [],
    dataset: {},
    style: {},
    classList: { toggle() {}, add() {}, remove() {} },
    _listeners: {},
    addEventListener(type, fn) { this._listeners[type] = fn; },
    append(...nodes) { this.childNodes.push(...nodes); },
    appendChild(node) { this.childNodes.push(node); return node; },
    replaceChildren(...nodes) { this.childNodes = nodes; },
    setAttribute(name, value) { this[name] = value; },
    removeAttribute() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
}
const cache = new Map();
function query(selector) {
  const key = String(selector);
  if (!cache.has(key)) cache.set(key, makeElement(key));
  return cache.get(key);
}
const document = {
  querySelector: query,
  querySelectorAll() { return []; },
  createElement(tag) { return makeElement(tag); },
  createTextNode(text) { return { nodeType: 3, textContent: String(text), childNodes: [] }; },
};
const requests = [];
const context = {
  console,
  document,
  fetch: async (url) => {
    const path = String(url);
    requests.push(path);
    if (path.includes("/api/me")) return { ok: false, status: 401, json: async () => ({}) };
    if (path.includes("/api/ontology")) return { ok: true, json: async () => ontology };
    if (path.includes("/api/objects")) {
      const type = new URL(path, "http://local").searchParams.get("type");
      const rows = type === "Order" ? orders : type === "Invoice" ? invoices : type === "Payment" ? payments : [];
      return { ok: true, json: async () => ({ objects: rows }) };
    }
    return { ok: true, json: async () => ({}) };
  },
  FormData: class {},
  setTimeout,
  clearTimeout,
  URL,
};
context.globalThis = context;
context.ieolEscape = require("./apps/web/escape.js");
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync("./apps/web/app.js", "utf8"), context);

function fail(code, message) {
  console.error(message);
  process.exit(code);
}
function buttons() {
  return query("#object-types").childNodes.filter((node) => node.tagName === "button");
}
function listHtml() {
  return String(query("#order-list").innerHTML || "");
}
function keysIn(html) {
  return ["ORD-1", "ORD-2", "FT-1", "INC-1", "PAG-1", "OLD-1"].filter((key) => html.includes(key));
}
async function choose(label) {
  const button = buttons().find((item) => item.textContent === label);
  if (!button || typeof button._listeners.click !== "function") fail(2, "missing " + label);
  await button._listeners.click();
}

(async () => {
await new Promise((resolve) => setTimeout(resolve, 0));
await context.ieolPage.loadObjects();

const labels = buttons().map((button) => button.textContent);
if (labels.join("|") !== "Ordine|Fattura|Incasso|Pagamento") fail(3, labels.join("|"));
for (const button of buttons()) {
  if (button.className !== "ghost") fail(4, button.className);
  if (button.innerHTML != null) fail(4, "filter used innerHTML");
  if (button["aria-pressed"] !== (button.textContent === "Ordine" ? "true" : "false")) fail(4, button.textContent);
}
if (keysIn(listHtml()).join("|") !== "ORD-1|ORD-2") fail(5, listHtml());
if (!requests.at(-1).includes("type=Order")) fail(5, requests.at(-1));

await choose("Fattura");
if (buttons().find((button) => button["aria-pressed"] === "true").textContent !== "Fattura") fail(6, "fattura");
if (keysIn(listHtml()).join("|") !== "FT-1") fail(6, listHtml());
if (!requests.at(-1).includes("type=Invoice")) fail(6, requests.at(-1));

await choose("Incasso");
if (buttons().find((button) => button["aria-pressed"] === "true").textContent !== "Incasso") fail(7, "incasso");
if (keysIn(listHtml()).join("|") !== "INC-1") fail(7, listHtml());
if (!requests.at(-1).includes("type=Payment")) fail(7, requests.at(-1));

await choose("Pagamento");
if (buttons().find((button) => button["aria-pressed"] === "true").textContent !== "Pagamento") fail(8, "pagamento");
if (keysIn(listHtml()).join("|") !== "PAG-1") fail(8, listHtml());
if (!requests.at(-1).includes("type=Payment")) fail(8, requests.at(-1));

await choose("Ordine");
if (keysIn(listHtml()).join("|") !== "ORD-1|ORD-2") fail(9, listHtml());
if (!requests.at(-1).includes("type=Order")) fail(9, requests.at(-1));

const { objectsForFilter, objectPageFilters } = context.ieolPage;
const mixed = orders.concat(invoices, payments);
if (objectsForFilter(mixed, "Order") !== mixed) fail(10, "order filter changed the list");
if (objectsForFilter(mixed, "Invoice") !== mixed) fail(10, "invoice filter changed the list");
if (objectsForFilter(orders, "Order") !== orders) fail(10, "order rows changed");
if (objectsForFilter(invoices, "Invoice") !== invoices) fail(10, "invoice rows changed");
const incasso = objectsForFilter(payments, "Payment").map((object) => object.business_key);
const pagamento = objectsForFilter(payments, "Pagamento").map((object) => object.business_key);
if (incasso.join("|") !== "INC-1") fail(11, incasso.join("|"));
if (pagamento.join("|") !== "PAG-1") fail(11, pagamento.join("|"));
const pageTypes = ontology.types.filter((item) => item.name === "Order" || item.name === "Invoice" || item.name === "Payment");
const pageLabels = objectPageFilters(pageTypes).map((filter) => filter.label);
if (pageLabels.join("|") !== "Ordine|Fattura|Incasso|Pagamento") fail(12, pageLabels.join("|"));
process.stdout.write("ok\n");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""


def test_pagamento_filter_shows_only_outgoing_payments_and_leaves_the_others():
    page = (WEB / "app.js").read_text(encoding="utf-8")
    assert "matchesIncasso(" in page
    assert "matchesPagamento(" in page
    assert 'label: "Pagamento"' in page
    rendered = subprocess.run(
        ["node", "-e", _OBJECT_FILTERS],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"
