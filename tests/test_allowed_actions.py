"""The page shows a button only when the server lists it.

Each role is checked against the real route: a listed action is not a 403,
and an action the role cannot take is a 403. The same list is what the card
draws. A quarantine refusal needs a motive. A refusal already stored without
one stays valid because the new check is NOT VALID.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from ontology.loader import load_merged
from services.allowed_actions import (
    customer_actions,
    drift_actions,
    file_actions,
    identity_actions,
    mapping_actions,
    payment_actions,
    quarantine_actions,
    recommendation_actions,
)
from services.cash.balance import remaining_balance
from services.cash.documents import write_invoice, write_payment
from services.db import connect
from services.errors import AppError
from tests.test_flow import give_company, login
from tests.test_order_to_cash import IBAN, _assign

ROOT = Path(__file__).resolve().parents[1]
FAKE = "00000000-0000-0000-0000-000000000099"
HASH = "ab" * 32
REASON = "motivo valido"
ROLES = ("admin", "mapper", "approver", "operator")
MOTIVE = "Scrivi il motivo del rifiuto (almeno 5 caratteri)."

_OPEN_QUARANTINE = {
    "id": "q1",
    "order_key": "1",
    "source_name": "mail",
    "reasons": [],
    "status": "open",
    "history": [],
}
_DECIDED_QUARANTINE = {
    "id": "q1",
    "order_key": "1",
    "source_name": "mail",
    "reasons": [],
    "status": "released",
    "history": [{"id": "h1", "decision": "released", "revoked": False}],
}
_OPEN_DRIFT = {"id": "d1", "source_name": "ordini", "diff": {}, "status": "open", "history": []}
_DECIDED_DRIFT = {
    "id": "d1",
    "source_name": "ordini",
    "diff": {},
    "status": "acknowledged",
    "history": [{"id": "h1", "decision": "acknowledged", "revoked": False}],
}
_OPEN_IDENTITY = {
    "id": "r1",
    "entity_type": "Customer",
    "reason": "Nomi vicini",
    "score": 0.7,
    "status": "open",
    "history": [],
}
_DECIDED_IDENTITY = {
    "id": "r1",
    "entity_type": "Customer",
    "reason": "Nomi vicini",
    "score": 0.7,
    "status": "confirmed",
    "history": [{"id": "h1", "decision": "confirmed", "revoked": False}],
}
_PROPOSED_MAPPING = {
    "id": "m1",
    "status": "proposed",
    "origin": "learned",
    "decision_id": "d1",
    "source_name": "ordini",
    "source_field": "cod",
    "target_type": "Order",
    "target_property": "number",
    "history": [{"id": "h1", "decision": "confirm", "revoked": False}],
}
_OPEN_RECOMMENDATION = {
    "id": "i1",
    "status": "proposed",
    "risk": "low",
    "order_key": "1024",
    "problem": "manca",
    "evidence_hash": "abc",
    "actions": [],
    "evidence": [],
    "history": [],
}
_RUNNABLE = {
    "id": "i1",
    "status": "dry_run_ok",
    "risk": "low",
    "order_key": "1024",
    "problem": "manca",
    "evidence_hash": "abc",
    "actions": [],
    "evidence": [],
    "history": [],
}
_REVOCABLE_RECOMMENDATION = {
    "id": "i1",
    "status": "approved",
    "risk": "low",
    "order_key": "1024",
    "problem": "manca",
    "evidence_hash": "abc",
    "actions": [],
    "evidence": [],
    "history": [{"id": "h1", "decision": "approve", "revoked": False}],
}
_OPEN_PAYMENT = {
    "id": "p1",
    "kind": "payment_match",
    "status": "proposed",
    "risk": "low",
    "problem": "abbina",
    "confidence": 1,
    "confidence_method": "rule",
    "evidence_hash": "abc",
    "payment": {"number": "INC-1", "amount": 10},
    "invoice": {"number": "FT-1", "amount": 10},
    "allocation": {"amount": 10, "remaining": 0},
    "criteria": {},
    "history": [],
}
_DECIDED_PAYMENT = {
    **_OPEN_PAYMENT,
    "status": "confirmed",
    "history": [{"id": "h1", "decision": "confirm", "revoked": False}],
}
_FILE = {"file_id": FAKE, "source_name": "ordini", "choices": []}
_CUSTOMER = {
    "id": FAKE,
    "company_name": "ACME",
    "business_key": "C1",
    "orders": [],
    "edges": [],
    "facts": [],
}


def _probe(kind, fn, name, sample, path, marker, body):
    return {
        "kind": kind,
        "fn": fn,
        "name": name,
        "sample": sample,
        "path": path,
        "marker": marker,
        "body": body,
    }


PROBES = [
    _probe("quarantine", quarantine_actions, "release", _OPEN_QUARANTINE, f"/api/quarantine/{FAKE}/release", ">Rilascia<", None),
    _probe("quarantine", quarantine_actions, "reject", _OPEN_QUARANTINE, f"/api/quarantine/{FAKE}/reject", "data-reject-quarantine", {"reason": REASON}),
    _probe("quarantine", quarantine_actions, "revoke", _DECIDED_QUARANTINE, f"/api/quarantine/{FAKE}/revoke", "data-revoke-quarantine", {"reason": REASON}),
    _probe("drift", drift_actions, "acknowledge", _OPEN_DRIFT, f"/api/drift/{FAKE}/acknowledge", ">Segna visto<", None),
    _probe("drift", drift_actions, "revoke", _DECIDED_DRIFT, f"/api/drift/{FAKE}/revoke", "data-revoke-drift", {"reason": REASON}),
    _probe("identity", identity_actions, "confirm", _OPEN_IDENTITY, f"/api/identity-reviews/{FAKE}/confirm", ">Conferma fusione<", None),
    _probe("identity", identity_actions, "reject", _OPEN_IDENTITY, f"/api/identity-reviews/{FAKE}/reject", ">Tieni separati<", None),
    _probe("identity", identity_actions, "revoke", _DECIDED_IDENTITY, f"/api/identity-reviews/{FAKE}/revoke", "data-revoke-identity", {"reason": REASON}),
    _probe("mapping", mapping_actions, "confirm", _PROPOSED_MAPPING, f"/api/mappings/{FAKE}/confirm", 'data-confirm="', None),
    _probe("mapping", mapping_actions, "reject", _PROPOSED_MAPPING, f"/api/mappings/{FAKE}/reject", 'data-reject="', None),
    _probe("mapping", mapping_actions, "revoke", _PROPOSED_MAPPING, f"/api/mapping-decisions/{FAKE}/revoke", "Annulla correzione", {"reason": REASON}),
    _probe("recommendation", recommendation_actions, "approve", _OPEN_RECOMMENDATION, f"/api/recommendations/{FAKE}/approve", 'data-act="approve"', {"decision": "approve", "evidence_hash": HASH}),
    _probe("recommendation", recommendation_actions, "modify", _OPEN_RECOMMENDATION, f"/api/recommendations/{FAKE}/modify", 'data-act="modify"', {"decision": "modify", "evidence_hash": HASH, "quantity": 1}),
    _probe("recommendation", recommendation_actions, "reject", _OPEN_RECOMMENDATION, f"/api/recommendations/{FAKE}/reject", 'data-act="reject"', {"decision": "reject", "evidence_hash": HASH, "reason": REASON}),
    _probe("recommendation", recommendation_actions, "execute", _RUNNABLE, f"/api/recommendations/{FAKE}/execute", 'data-act="execute"', None),
    _probe("recommendation", recommendation_actions, "revoke", _REVOCABLE_RECOMMENDATION, f"/api/decisions/{FAKE}/revoke", "data-revoke-inbox", {"reason": REASON}),
    _probe("payment", payment_actions, "confirm", _OPEN_PAYMENT, f"/api/payment-proposals/{FAKE}/confirm", 'data-act="confirm-match"', {"decision": "confirm", "evidence_hash": HASH}),
    _probe("payment", payment_actions, "correct", _OPEN_PAYMENT, f"/api/payment-proposals/{FAKE}/correct", 'data-act="correct-match"', {"decision": "correct", "evidence_hash": HASH, "invoice_number": "FT-1", "allocated_amount": 1, "reason": REASON}),
    _probe("payment", payment_actions, "reject", _OPEN_PAYMENT, f"/api/payment-proposals/{FAKE}/reject", 'data-act="reject-match"', {"decision": "reject", "evidence_hash": HASH, "reason": REASON}),
    _probe("payment", payment_actions, "revoke", _DECIDED_PAYMENT, f"/api/payment-decisions/{FAKE}/revoke", "data-revoke-payment", {"reason": REASON}),
    _probe("file", file_actions, "correct", _FILE, f"/api/files/{FAKE}/type", "data-type-ok", {"decision": "correct"}),
    _probe("file", file_actions, "wrong", _FILE, f"/api/files/{FAKE}/type", "data-type-wrong", {"decision": "wrong", "correct_type": "order"}),
    _probe("file", file_actions, "not_sure", _FILE, f"/api/files/{FAKE}/type", "data-type-wrong", {"decision": "not_sure"}),
    _probe("customer", customer_actions, "unmask", _CUSTOMER, "/api/pii/unmask", "unmask-pii", {"entity_id": FAKE, "reason": REASON}),
]

_RENDER = r"""
const fs = require("fs");
const {
  quarantineCard, driftCard, identityCard, mappingRow, inboxCard, customerDetail, fileActionBar,
} = require("./apps/web/escape.js");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
function render(kind, item) {
  if (kind === "quarantine") return quarantineCard(item, true, "operator");
  if (kind === "drift") return driftCard(item, true, "operator");
  if (kind === "identity") return identityCard(item, true, "operator");
  if (kind === "mapping") return mappingRow(item, true, "operator");
  if (kind === "recommendation" || kind === "payment") return inboxCard(item, "operator", true);
  if (kind === "file") return fileActionBar(item);
  if (kind === "customer") return customerDetail(item, "operator", true);
  throw new Error(kind);
}
for (const row of cases) {
  const html = render(row.kind, row.item);
  const shown = html.includes(row.marker);
  if (shown !== row.expect) {
    console.error(`${row.role} ${row.kind} ${row.name} shown=${shown} expect=${row.expect}`);
    process.exit(1);
  }
}
process.stdout.write("ok\n");
"""


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_each_role_sees_only_the_actions_the_server_allows(client: TestClient):
    quarantine_ids = {}
    with connect() as conn:
        for role in ROLES:
            company_id = give_company(role, f"Azioni {role}")
            quarantine_ids[role] = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO app.quarantine_documents (
                  id, source_name, order_key, reasons, status, company_id
                )
                VALUES (%s, 'mail', '1', '[]'::jsonb, 'open', %s)
                """,
                (quarantine_ids[role], company_id),
            )
        conn.commit()

    cases = []
    for role in ROLES:
        login(client, role)
        documents = client.get("/api/quarantine").json()["documents"]
        document = next(item for item in documents if item["id"] == quarantine_ids[role])
        assert document["allowed_actions"] == quarantine_actions(role, document, company=True)
        for probe in PROBES:
            offered = probe["name"] in probe["fn"](role, probe["sample"], company=True)
            if probe["body"] is None:
                response = client.post(probe["path"])
            else:
                response = client.post(probe["path"], json=probe["body"])
            assert (response.status_code != 403) is offered, (
                role,
                probe["kind"],
                probe["name"],
                response.status_code,
                response.text,
            )
            item = dict(probe["sample"])
            item["allowed_actions"] = probe["fn"](role, probe["sample"], company=True)
            cases.append({
                "role": role,
                "kind": probe["kind"],
                "name": probe["name"],
                "item": item,
                "marker": probe["marker"],
                "expect": offered,
            })

    rendered = subprocess.run(
        ["node", "-e", _RENDER],
        cwd=ROOT,
        input=json.dumps(cases),
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"


def test_a_new_quarantine_refusal_needs_a_reason_and_the_card_keeps_it_out_of_html(client: TestClient):
    company_id = give_company("mapper", "Rifiuti")
    give_company("approver", "Chi guarda")
    open_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.quarantine_documents (id, source_name, order_key, reasons, status, company_id)
            VALUES (%s, 'mail', '9', '[]'::jsonb, 'open', %s),
                   (%s, 'mail', '10', '[]'::jsonb, 'open', %s)
            """,
            (open_id, company_id, other_id, company_id),
        )
        conn.commit()

    login(client, "mapper")
    for body in ({}, {"reason": "  ab  "}, {"reason": "    "}):
        refused = client.post(f"/api/quarantine/{open_id}/reject", json=body)
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"] == MOTIVE
    still = client.get("/api/quarantine").json()["documents"]
    assert next(item["status"] for item in still if item["id"] == open_id) == "open"

    accepted = client.post(f"/api/quarantine/{open_id}/reject", json={"reason": "  abcde  "})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["reason"] == "abcde"
    shown = next(item for item in client.get("/api/quarantine").json()["documents"] if item["id"] == open_id)
    assert shown["status"] == "rejected"
    assert shown["reason"] == "abcde"
    assert shown["history"][0]["actor"] == "mapper"
    assert shown["history"][0]["decision"] == "rejected"

    login(client, "approver")
    blocked = client.post(f"/api/quarantine/{other_id}/reject", json={"reason": REASON})
    assert blocked.status_code == 403

    script = r"""
const { quarantineCard } = require("./apps/web/escape.js");
const card = quarantineCard({
  id: "q-reason",
  order_key: "9",
  reasons: [],
  status: "rejected",
  reason: "<img src=x onerror=alert(1)>",
  history: [{ decision: "rejected", actor: "mapper", decided_at: "2026-10-21T18:00:00" }],
}, true, "mapper");
if (card.includes("<img") || card.includes("onerror")) process.exit(1);
if (!card.includes('data-quarantine-reason="q-reason"')) process.exit(2);
if (!card.includes("Rifiutato da mapper il 21/10/2026")) process.exit(3);
const app = require("fs").readFileSync("./apps/web/app.js", "utf8");
if (!app.includes('slot.textContent = item.reason == null ? "" : String(item.reason)')) process.exit(4);
process.stdout.write("ok\n");
"""
    rendered = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True)
    assert rendered.returncode == 0, rendered.stderr


def _sql_without_comments(path: Path) -> str:
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if "--" in line:
            line = line.split("--", 1)[0]
        kept.append(line)
    return "\n".join(kept)


def test_old_rejected_document_stays_valid_and_a_new_one_needs_a_reason():
    path = ROOT / "db" / "migrations" / "0021_quarantine_reject_reason.sql"
    body = _sql_without_comments(path)
    folded = body.upper()
    assert "UPDATE" not in folded
    assert "NOT VALID" in folded
    assert "VALIDATE CONSTRAINT" not in folded

    company_id = give_company("mapper", "Rifiuti vecchi")
    old_id = str(uuid.uuid4())
    open_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            "ALTER TABLE app.quarantine_documents DROP CONSTRAINT quarantine_documents_reject_reason_check"
        )
        conn.execute("ALTER TABLE app.quarantine_documents DROP COLUMN reason")
        conn.execute(
            """
            INSERT INTO app.quarantine_documents (
              id, source_name, order_key, reasons, status, company_id, actor
            )
            VALUES (%s, 'vecchio', '1', '[]'::jsonb, 'rejected', %s, 'mapper')
            """,
            (old_id, company_id),
        )
        conn.execute(path.read_text(encoding="utf-8"))
        stored = conn.execute(
            "SELECT status, reason FROM app.quarantine_documents WHERE id = %s",
            (old_id,),
        ).fetchone()
        assert stored == {"status": "rejected", "reason": None}
        validated = conn.execute(
            """
            SELECT convalidated
            FROM pg_constraint
            WHERE conname = 'quarantine_documents_reject_reason_check'
            """
        ).fetchone()
        assert validated["convalidated"] is False

        def rejected(reason: str | None) -> None:
            conn.execute(
                """
                INSERT INTO app.quarantine_documents (
                  id, source_name, order_key, reasons, status, company_id, actor, reason
                )
                VALUES (%s, 'nuovo', '2', '[]'::jsonb, 'rejected', %s, 'mapper', %s)
                """,
                (str(uuid.uuid4()), company_id, reason),
            )

        for reason in (None, "no", "  abcd  "):
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    rejected(reason)

        conn.execute(
            """
            INSERT INTO app.quarantine_documents (id, source_name, order_key, reasons, status)
            VALUES (%s, 'aperto', '3', '[]'::jsonb, 'open')
            """,
            (open_id,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE app.quarantine_documents
                    SET status = 'rejected', company_id = %s, actor = 'mapper', reason = 'no'
                    WHERE id = %s
                    """,
                    (company_id, open_id),
                )
        conn.execute(
            """
            UPDATE app.quarantine_documents
            SET status = 'rejected', company_id = %s, actor = 'mapper', reason = '  abcde  '
            WHERE id = %s
            """,
            (company_id, open_id),
        )
        kept = conn.execute(
            "SELECT status, reason FROM app.quarantine_documents WHERE id = %s",
            (old_id,),
        ).fetchone()
        assert kept == {"status": "rejected", "reason": None}
        fresh = conn.execute(
            "SELECT status, reason FROM app.quarantine_documents WHERE id = %s",
            (open_id,),
        ).fetchone()
        assert fresh["status"] == "rejected"
        assert fresh["reason"] == "  abcde  "
        conn.rollback()


def test_outgoing_allocation_does_not_match_a_sales_invoice_or_reduce_the_balance(client: TestClient):
    company_id = give_company("mapper", "Incassi")
    _assign("approver", company_id)
    model = load_merged()

    def payment(conn, source_key: str, direction: str) -> str:
        return write_payment(
            conn,
            model,
            company_id=company_id,
            source_key=source_key,
            direction=direction,
            paid_at="2026-04-01",
            amount=Decimal("40.00"),
            iban=IBAN,
            causale="prova",
            counterparty_name="Cliente Prova",
            counterparty_vat="IT00000000001",
            reference="FT-1",
            source="banca",
            source_class="external",
            label_origin="rule",
        )

    def allocate(conn, payment_id: str, invoice_id: str, amount: str) -> None:
        conn.execute(
            """
            INSERT INTO app.payment_decisions (
              id, proposal_id, payment_entity_id, company_id, decision,
              actor, actor_role, invoice_entity_id, allocated_amount,
              failed_criteria, reason, source_name
            )
            VALUES (
              %s, NULL, %s, %s, 'correct',
              'approver', 'approver', %s, %s,
              '[]'::jsonb, 'assegno gia salvato', 'banca'
            )
            """,
            (str(uuid.uuid4()), payment_id, company_id, invoice_id, Decimal(amount)),
        )

    with connect() as conn:
        invoice_id = write_invoice(
            conn,
            model,
            company_id=company_id,
            number="FT-1",
            document_type="TD01",
            issued_at="2026-04-01",
            amount=Decimal("100.00"),
            currency="EUR",
            customer_name="Cliente Prova",
            customer_vat="IT00000000001",
            order_number=None,
            due_dates=["2026-04-30"],
            lines=[],
            source="fatture",
            source_class="system_of_record",
            label_origin="rule",
        )
        incoming_id = payment(conn, "INC-IN", "incoming")
        outgoing_id = payment(conn, "INC-OUT", "outgoing")
        blank_id = payment(conn, "INC-BLANK", "incoming")
        conn.execute(
            """
            UPDATE app.facts
            SET valid_to = now()
            WHERE entity_id = %s::uuid AND property = 'direction' AND valid_to IS NULL
            """,
            (blank_id,),
        )
        allocate(conn, outgoing_id, invoice_id, "40.00")
        allocate(conn, incoming_id, invoice_id, "25.00")
        allocate(conn, blank_id, invoice_id, "10.00")
        assert remaining_balance(conn, company_id, invoice_id) == Decimal("65.00")
        before = conn.execute("SELECT count(*) AS n FROM app.payment_decisions").fetchone()["n"]
        conn.commit()

    login(client, "approver")
    refused = client.post(
        f"/api/payments/{outgoing_id}/correct",
        json={"invoice_number": "FT-1", "allocated_amount": 10, "reason": REASON},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == "Un pagamento in uscita non si abbina a una fattura di vendita"
    with connect() as conn:
        after = conn.execute("SELECT count(*) AS n FROM app.payment_decisions").fetchone()["n"]
    assert after == before

    objects = client.get("/api/objects", params={"type": "Payment"}).json()["objects"]
    by_key = {item["business_key"]: item for item in objects}
    assert by_key["INC-IN"]["incoming"] is True
    assert by_key["INC-OUT"]["incoming"] is False
    assert by_key["INC-BLANK"]["incoming"] is True
    invoice = client.get(f"/api/objects/{invoice_id}").json()
    assert invoice["remaining"] == 65

    page = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
    assert "matchesIncasso(" in page
    rendered = subprocess.run(
        ["node", "-e", _INCASSO],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"


_INCASSO = r"""
const { matchesIncasso } = require("./apps/web/escape.js");
if (!matchesIncasso({ incoming: true, direction: "outgoing" })) process.exit(1);
if (matchesIncasso({ incoming: false, direction: "" })) process.exit(2);
if (matchesIncasso({ direction: "incoming" })) process.exit(3);
if (matchesIncasso(null) || matchesIncasso({})) process.exit(4);
process.stdout.write("ok\n");
"""


def test_reject_document_still_hides_another_companys_row_before_the_reason():
    from services.pipeline import reject_document

    company_id = give_company("mapper", "Altrui")
    other = give_company("approver", "Altra")
    quarantine_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.quarantine_documents (id, source_name, order_key, reasons, status, company_id)
            VALUES (%s, 'mail', '4', '[]'::jsonb, 'open', %s)
            """,
            (quarantine_id, company_id),
        )
        with pytest.raises(AppError) as caught:
            reject_document(
                conn,
                quarantine_id,
                "mapper",
                actor="approver",
                company_id=other,
            )
        assert caught.value.status == 404
        conn.rollback()
