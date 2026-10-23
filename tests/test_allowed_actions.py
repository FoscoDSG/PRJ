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
    page_actions,
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

_PAGE_PROBES = (
    ("upload", "/api/ingest/document"),
    ("confirm_confident", "/api/mappings/confirm-confident"),
    ("materialize", "/api/materialize"),
)


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
        me = client.get("/api/me").json()
        assert me["allowed_actions"] == page_actions(role, company=True)
        for name, path in _PAGE_PROBES:
            offered = name in me["allowed_actions"]
            if name == "upload":
                response = client.post(
                    path,
                    files={"file": ("ordine.txt", b"non e un ordine", "text/plain")},
                )
            else:
                response = client.post(path)
            assert (response.status_code != 403) is offered, (
                role,
                name,
                response.status_code,
                response.text,
            )

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
        conn.execute(
            "UPDATE app.quarantine_documents SET created_at = '2020-01-02T00:00:00Z' WHERE id = %s",
            (open_id,),
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
    with connect() as conn:
        stored = conn.execute(
            "SELECT created_at, decided_at FROM app.quarantine_documents WHERE id = %s",
            (open_id,),
        ).fetchone()
    assert stored["created_at"].year == 2020
    assert stored["decided_at"] is not None
    assert stored["decided_at"] != stored["created_at"]
    from services.revocation import rome_stamp

    assert shown["history"][0]["decided_at"] == rome_stamp(stored["decided_at"])
    assert "2020" not in shown["history"][0]["decided_at"]

    login(client, "approver")
    blocked = client.post(f"/api/quarantine/{other_id}/reject", json={"reason": REASON})
    assert blocked.status_code == 403

    script = r"""
const { quarantineCard } = require("./apps/web/escape.js");
const sample = JSON.parse(require("fs").readFileSync(0, "utf8"));
const card = quarantineCard({
  id: "q-reason",
  order_key: "9",
  reasons: [],
  status: "rejected",
  reason: sample.reason,
  history: [{ decision: "rejected", actor: "mapper", decided_at: sample.decided_at }],
}, true, "mapper");
if (card.includes(sample.reason) || card.includes("Motivo:")) process.exit(1);
if (!card.includes('data-quarantine-reason="q-reason"')) process.exit(2);
if (!card.includes("Rifiutato da mapper il " + sample.decided_at)) process.exit(3);
if (card.includes("Rifiutata")) process.exit(4);
const app = require("fs").readFileSync("./apps/web/app.js", "utf8");
if (!app.includes('slot.textContent = "Motivo: " + String(item.reason).trim()')) process.exit(5);
process.stdout.write("ok\n");
"""
    rendered = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        input=json.dumps({"reason": "<img src=x onerror=alert(1)>", "decided_at": shown["history"][0]["decided_at"]}),
        text=True,
        capture_output=True,
    )
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
    assert "DECIDED_AT" in folded
    assert "DEFAULT" not in folded

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
            """
            SELECT status, reason, decided_at, created_at
            FROM app.quarantine_documents WHERE id = %s
            """,
            (old_id,),
        ).fetchone()
        assert stored["status"] == "rejected"
        assert stored["reason"] is None
        assert stored["decided_at"] is None
        assert stored["created_at"] is not None
        from services.revocation import state_histories

        history = state_histories(
            conn,
            subject_table="app.quarantine_documents",
            revocation_table="app.quarantine_decisions",
            subject_ids=[old_id],
            decision_for_status={"released": "released", "rejected": "rejected"},
            when_column="decided_at",
        )[old_id][0]
        assert history["decided_at"] is None
        assert history["actor"] == "mapper"
        rendered = subprocess.run(
            ["node", "-e", _UNDATED_REJECTION],
            cwd=ROOT,
            input=json.dumps(history),
            text=True,
            capture_output=True,
        )
        assert rendered.returncode == 0, rendered.stderr
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
            "SELECT status, reason, decided_at FROM app.quarantine_documents WHERE id = %s",
            (old_id,),
        ).fetchone()
        assert kept == {"status": "rejected", "reason": None, "decided_at": None}
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


_UNDATED_REJECTION = r"""
const { quarantineCard } = require("./apps/web/escape.js");
const entry = JSON.parse(require("fs").readFileSync(0, "utf8"));
const card = quarantineCard({
  id: "q-old",
  order_key: "1",
  reasons: [],
  status: "rejected",
  reason: null,
  history: [entry],
}, true, "mapper");
if (!card.includes("Rifiutato da mapper")) process.exit(1);
if (card.includes(" il ") || card.includes("Rifiutata") || card.includes("quarantine-reason")) process.exit(2);
if (card.includes("In attesa di conferma")) process.exit(3);
process.stdout.write("ok\n");
"""


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


def test_release_writes_the_decision_date(client: TestClient):
    company_id = give_company("mapper", "Rilasci")
    quarantine_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.quarantine_documents (
              id, source_name, order_key, reasons, status, company_id, created_at
            )
            VALUES (%s, 'mail', '8', '[]'::jsonb, 'open', %s, '2020-03-03T00:00:00Z')
            """,
            (quarantine_id, company_id),
        )
        conn.commit()
    login(client, "mapper")
    released = client.post(f"/api/quarantine/{quarantine_id}/release")
    assert released.status_code == 200, released.text
    shown = next(
        item for item in client.get("/api/quarantine").json()["documents"] if item["id"] == quarantine_id
    )
    assert shown["status"] == "released"
    assert shown["history"][0]["decision"] == "released"
    assert "2020" not in (shown["history"][0]["decided_at"] or "")
    with connect() as conn:
        stored = conn.execute(
            "SELECT decided_at FROM app.quarantine_documents WHERE id = %s",
            (quarantine_id,),
        ).fetchone()
    from services.revocation import rome_stamp

    assert stored["decided_at"] is not None
    assert shown["history"][0]["decided_at"] == rome_stamp(stored["decided_at"])


def test_revoking_a_rejection_leaves_the_document_rejected(client: TestClient):
    company_id = give_company("mapper", "Annulli")
    quarantine_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO app.quarantine_documents (id, source_name, order_key, reasons, status, company_id)
            VALUES (%s, 'mail', '7', '[]'::jsonb, 'open', %s)
            """,
            (quarantine_id, company_id),
        )
        conn.commit()
    login(client, "mapper")
    rejected = client.post(f"/api/quarantine/{quarantine_id}/reject", json={"reason": REASON})
    assert rejected.status_code == 200, rejected.text
    revoked = client.post(
        f"/api/quarantine/{quarantine_id}/revoke",
        json={"reason": "annullo il rifiuto"},
    )
    assert revoked.status_code == 200, revoked.text
    shown = next(
        item for item in client.get("/api/quarantine").json()["documents"] if item["id"] == quarantine_id
    )
    assert shown["status"] == "rejected"
    assert "release" not in shown["allowed_actions"]
    assert "reject" not in shown["allowed_actions"]
    assert shown["history"][0]["revoked"] is True
    rendered = subprocess.run(
        ["node", "-e", _REVOKED_REJECTION],
        cwd=ROOT,
        input=json.dumps(shown),
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"


def test_correct_stays_off_an_outgoing_payment():
    outgoing = dict(_OPEN_PAYMENT)
    outgoing["payment"] = {**_OPEN_PAYMENT["payment"], "direction": "outgoing"}
    outgoing["allowed_actions"] = payment_actions("approver", outgoing, company=True)
    incoming = dict(_OPEN_PAYMENT)
    incoming["allowed_actions"] = payment_actions("approver", _OPEN_PAYMENT, company=True)
    assert outgoing["allowed_actions"] == ["reject"]
    assert incoming["allowed_actions"] == ["confirm", "correct", "reject"]
    rendered = subprocess.run(
        ["node", "-e", _OUTGOING_CORRECT],
        cwd=ROOT,
        input=json.dumps({"outgoing": outgoing, "incoming": incoming}),
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"


def test_confirming_an_outgoing_payment_is_refused_and_old_rows_stay(client: TestClient):
    from services.agent.payment_measure import measure_payment_matches

    company_id = give_company("mapper", "Conferme in uscita")
    _assign("approver", company_id)
    model = load_merged()
    message = "Un pagamento in uscita non si abbina a una fattura di vendita"

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
            reference="FT-USCITA",
            source="banca",
            source_class="external",
            label_origin="rule",
        )

    def proposal(conn, payment_id: str, invoice_id: str) -> str:
        proposal_id = str(uuid.uuid4())
        allocation_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO app.entities (id, type, business_key, company_id)
            VALUES (%s, 'PaymentAllocation', %s, %s)
            """,
            (allocation_id, proposal_id, company_id),
        )
        conn.execute(
            """
            INSERT INTO app.payment_proposals (
              id, company_id, payment_entity_id, invoice_entity_id, allocation_entity_id,
              match_group, source_name, label_origin, criteria, score, confidence_method,
              confidence_rule, allocated_amount, evidence, evidence_hash, process_id, step_id
            )
            VALUES (
              %s, %s, %s, %s, %s,
              %s, 'banca', 'rule', '{}'::jsonb, 1, 'rule',
              'payment-match.baseline@v1', 40, '[]'::jsonb, %s, 'order_to_cash', 'match_proposed'
            )
            """,
            (
                proposal_id,
                company_id,
                payment_id,
                invoice_id,
                allocation_id,
                str(uuid.uuid4()),
                HASH,
            ),
        )
        return proposal_id

    with connect() as conn:
        invoice_id = write_invoice(
            conn,
            model,
            company_id=company_id,
            number="FT-USCITA",
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
        old_payment = payment(conn, "PAG-OLD", "outgoing")
        open_payment = payment(conn, "PAG-OPEN", "outgoing")
        incoming_payment = payment(conn, "PAG-IN", "incoming")
        old_proposal = proposal(conn, old_payment, invoice_id)
        open_proposal = proposal(conn, open_payment, invoice_id)
        old_decision = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO app.payment_decisions (
              id, proposal_id, payment_entity_id, company_id, decision,
              actor, actor_role, invoice_entity_id, allocated_amount,
              failed_criteria, reason, source_name
            )
            VALUES (
              %s, %s, %s, %s, 'confirm',
              'approver', 'approver', %s, 40,
              '[]'::jsonb, '', 'banca'
            )
            """,
            (old_decision, old_proposal, old_payment, company_id, invoice_id),
        )
        conn.execute(
            """
            INSERT INTO app.payment_decisions (
              id, proposal_id, payment_entity_id, company_id, decision,
              actor, actor_role, invoice_entity_id, allocated_amount,
              failed_criteria, reason, source_name
            )
            VALUES (
              %s, NULL, %s, %s, 'correct',
              'approver', 'approver', %s, 25,
              '[]'::jsonb, 'incasso gia salvato', 'banca'
            )
            """,
            (str(uuid.uuid4()), incoming_payment, company_id, invoice_id),
        )
        conn.commit()
        balance_before = remaining_balance(conn, company_id, invoice_id)
        measure_before = measure_payment_matches(conn, company_id=company_id)
        stored_before = conn.execute(
            """
            SELECT id::text, decision, invoice_entity_id::text, allocated_amount, decided_at
            FROM app.payment_decisions
            WHERE id = %s
            """,
            (old_decision,),
        ).fetchone()
        count_before = conn.execute("SELECT count(*) AS n FROM app.payment_decisions").fetchone()["n"]

    assert balance_before == Decimal("75.00")
    login(client, "approver")
    refused = client.post(
        f"/api/payment-proposals/{open_proposal}/confirm",
        json={"decision": "confirm", "evidence_hash": HASH},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == message

    with connect() as conn:
        assert remaining_balance(conn, company_id, invoice_id) == balance_before
        measure_after = measure_payment_matches(conn, company_id=company_id)
        stored_after = conn.execute(
            """
            SELECT id::text, decision, invoice_entity_id::text, allocated_amount, decided_at
            FROM app.payment_decisions
            WHERE id = %s
            """,
            (old_decision,),
        ).fetchone()
        count_after = conn.execute("SELECT count(*) AS n FROM app.payment_decisions").fetchone()["n"]
        open_decisions = conn.execute(
            "SELECT count(*) AS n FROM app.payment_decisions WHERE proposal_id = %s",
            (open_proposal,),
        ).fetchone()["n"]
    assert measure_after == measure_before
    assert stored_after == stored_before
    assert count_after == count_before
    assert open_decisions == 0


def test_the_page_uses_server_actions_for_its_buttons_and_file_types():
    assert page_actions("mapper", company=False) == []
    assert page_actions("approver", company=True) == []
    assert page_actions("admin", company=True) == ["upload", "confirm_confident", "materialize"]
    hostile = "<img src=x onerror=alert(1)>"
    choices = [{"code": "order", "label": hostile}, {"code": "not_sure", "label": "Non so"}]
    cases = []
    for role in ROLES:
        sample = {"file_id": "f1", "source_name": "ordini", "choices": choices}
        cases.append({
            "role": role,
            "allowed_actions": file_actions(role, sample, company=True),
            "choices": choices,
            "hostile": hostile,
        })
    cases.append({
        "role": "mapper-without-company",
        "allowed_actions": file_actions("mapper", {"file_id": "f1", "choices": choices}, company=False),
        "choices": choices,
        "hostile": hostile,
    })
    rendered = subprocess.run(
        ["node", "-e", _PAGE_DOM],
        cwd=ROOT,
        input=json.dumps(cases),
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"


def test_balance_uses_the_newest_payment_direction():
    company_id = give_company("mapper", "Direzioni")
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
            reference="FT-9",
            source="banca",
            source_class="external",
            label_origin="rule",
        )

    def later_direction(conn, payment_id: str, direction: str) -> None:
        current = conn.execute(
            """
            SELECT id FROM app.facts
            WHERE entity_id = %s::uuid AND property = 'direction' AND valid_to IS NULL
            """,
            (payment_id,),
        ).fetchone()
        conn.execute(
            "UPDATE app.facts SET valid_from = '2019-01-01T00:00:00Z' WHERE id = %s",
            (current["id"],),
        )
        conn.execute(
            """
            INSERT INTO app.facts (
              id, entity_id, property, value, classification, origin, source,
              confidence, valid_from, valid_to, source_class, confidence_method,
              confidence_rule, company_id
            )
            SELECT %s, entity_id, property, %s::jsonb, classification, origin, source,
                   confidence, '2024-06-01T00:00:00Z', NULL, source_class, confidence_method,
                   confidence_rule, company_id
            FROM app.facts WHERE id = %s
            """,
            (str(uuid.uuid4()), json.dumps(direction), current["id"]),
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
            number="FT-9",
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
        turned_out = payment(conn, "INC-OUT-NEW", "incoming")
        turned_in = payment(conn, "INC-IN-NEW", "outgoing")
        later_direction(conn, turned_out, "outgoing")
        later_direction(conn, turned_in, "incoming")
        allocate(conn, turned_out, invoice_id, "25.00")
        allocate(conn, turned_in, invoice_id, "10.00")
        assert remaining_balance(conn, company_id, invoice_id) == Decimal("90.00")
        conn.rollback()


_REVOKED_REJECTION = r"""
const { quarantineCard } = require("./apps/web/escape.js");
const item = JSON.parse(require("fs").readFileSync(0, "utf8"));
const card = quarantineCard(item, true, "mapper");
if (!card.includes("Il documento resta rifiutato e non torna in coda.")) process.exit(1);
if (card.includes(">Rilascia<") || card.includes(">Rifiuta<") || card.includes("data-reject-quarantine")) process.exit(2);
if (!card.includes("revoked-decision") || !card.includes("Rifiutato da ")) process.exit(3);
process.stdout.write("ok\n");
"""


_OUTGOING_CORRECT = r"""
const { inboxCard } = require("./apps/web/escape.js");
const sample = JSON.parse(require("fs").readFileSync(0, "utf8"));
const outgoing = inboxCard(sample.outgoing, "approver", true);
const incoming = inboxCard(sample.incoming, "approver", true);
if (outgoing.includes('data-act="correct-match"') || outgoing.includes(">Correggi<")) process.exit(1);
if (outgoing.includes('data-act="confirm-match"') || outgoing.includes(">Conferma<")) process.exit(2);
if (!outgoing.includes('data-act="reject-match"')) process.exit(3);
if (!incoming.includes('data-act="confirm-match"') || !incoming.includes('data-act="correct-match"')) process.exit(4);
process.stdout.write("ok\n");
"""


_PAGE_DOM = r"""
const fs = require("fs");
const vm = require("vm");
const ieolEscape = require("./apps/web/escape.js");
const cases = JSON.parse(fs.readFileSync(0, "utf8"));

function makeElement(tag) {
  const el = {
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
  return el;
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
const context = {
  console,
  document,
  fetch: async (url) => {
    if (String(url).includes("/api/me")) return { ok: false, status: 401, json: async () => ({}) };
    return {
      ok: true,
      json: async () => ({
        recommendations: [], documents: [], drift: [], mappings: [], files: [],
        events: [], total: 0, offset: 0, types: [], processes: [],
      }),
    };
  },
  FormData: class {},
  setTimeout,
  clearTimeout,
};
context.globalThis = context;
context.ieolEscape = ieolEscape;
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync("./apps/web/app.js", "utf8"), context);
const { fileTypeActions, enter } = context.ieolPage;

function walk(node, visit) {
  visit(node);
  for (const child of node.childNodes || []) walk(child, visit);
}

for (const row of cases) {
  const bar = fileTypeActions({
    file_id: "f1",
    choices: row.choices,
    allowed_actions: row.allowed_actions,
  });
  const buttons = [];
  const options = [];
  let htmlAssigned = false;
  walk(bar, (node) => {
    if (node.innerHTML != null) htmlAssigned = true;
    if (node.tagName === "button") buttons.push(node.textContent);
    if (node.tagName === "option") options.push(node.textContent);
  });
  const listed = row.allowed_actions;
  if (buttons.includes("Corretto") !== listed.includes("correct")) {
    console.error(row.role, "correct", buttons, listed);
    process.exit(1);
  }
  const wrongListed = listed.includes("wrong") || listed.includes("not_sure");
  if (buttons.includes("Non è questo tipo") !== wrongListed) {
    console.error(row.role, "wrong", buttons, listed);
    process.exit(2);
  }
  if (htmlAssigned) {
    console.error(row.role, "file type used innerHTML");
    process.exit(3);
  }
  if (wrongListed && !options.includes(row.hostile)) {
    console.error(row.role, "choice label was not textContent");
    process.exit(3);
  }
  if (!wrongListed && options.includes(row.hostile)) {
    console.error(row.role, "choice shown without an allowed action");
    process.exit(3);
  }
  if (wrongListed) {
    let picker = null;
    let wrong = null;
    walk(bar, (node) => {
      if (node.className === "file-type-choice") picker = node;
      if (node.textContent === "Non è questo tipo") wrong = node;
    });
    if (!picker || !picker.hidden || !wrong || !wrong._listeners.click) process.exit(4);
    wrong._listeners.click();
    if (picker.hidden) process.exit(5);
  }
}

function hidden(id) { return query(id).hidden; }
enter({ username: "approver", role: "approver", has_company: true, demo_mode: false, allowed_actions: [] });
if (!hidden("#upload-label") || !hidden("#confirm-high") || !hidden("#materialize")) process.exit(6);
enter({ username: "mapper", role: "mapper", has_company: true, demo_mode: false });
if (!hidden("#upload-label") || !hidden("#confirm-high") || !hidden("#materialize")) process.exit(7);
enter({
  username: "mapper",
  role: "mapper",
  has_company: true,
  demo_mode: false,
  allowed_actions: ["upload", "confirm_confident", "materialize"],
});
if (hidden("#upload-label") || hidden("#confirm-high") || hidden("#materialize")) process.exit(8);
enter({ username: "mapper", role: "mapper", has_company: true, demo_mode: false, allowed_actions: ["upload"] });
if (hidden("#upload-label") || !hidden("#confirm-high") || !hidden("#materialize")) process.exit(9);
process.stdout.write("ok\n");
"""
