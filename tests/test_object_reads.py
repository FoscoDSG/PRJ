"""Letture nuove: oggetti per azienda, residuo, aperto/chiuso, maschera.

La pagina non inventa «riservato» e non deduce aperto/chiuso dallo stato.
Il residuo sottrae solo note di credito e decisioni umane ancora valide.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from services.db import connect
from services.disposition import card_disposition
from services.pii import MASK, RESERVED, note_clear_personal_view
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
    sequences = [
        [],
        [{"decision": "approve"}],
        [{"decision": "reject"}],
        [{"decision": "modify"}],
        [{"decision": "confirm"}],
        [{"decision": "correct"}],
        [{"decision": "approve"}, {"decision": "revoked"}],
        [{"decision": "confirm"}, {"decision": "revoked"}],
        [{"decision": "correct"}, {"decision": "revoked"}],
        [{"decision": "reject"}, {"decision": "revoked"}, {"decision": "approve"}],
    ]
    for decisions in sequences:
        last = decisions[-1]["decision"] if decisions else ""
        expected = "open" if last in ("", "revoked") else "closed"
        assert card_disposition(decisions) == expected
        assert card_disposition(list(decisions)) == expected


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
    assert full["payment"] == {"number": "INC-100", "paid_at": "2026-04-02", "amount": 122}
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
    assert mapper_full["allocated_amount"] == MASK
    assert mapper_full["allocation"]["amount"] == 122

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
    assert _field(found, "margin")["value"] == 0.6667
    assert _field(found, "margin")["masked"] is False

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
    assert reopened["disposition"] == "open"

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


def test_pages_show_reserved_only_when_the_server_sends_it():
    for path in WEB.iterdir():
        if path.suffix in {".js", ".html", ".css"}:
            assert "riservato" not in path.read_text(encoding="utf-8")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'disposition !== "open"' in app
    assert "invoice_number:" in app
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    assert "max-width: 859px" in css
    assert "overflow-x: clip" in css
    assert "grid-template-columns: 1fr" in css
    page = (WEB / "index.html").read_text(encoding="utf-8")
    assert "IEOL" in page
    assert "Oggi" in page
    assert "Oggetti" in page
    assert "Dati" in page
