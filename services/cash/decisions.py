"""Human decisions on a payment match. The rule does not close the invoice.

Decisions are append-only. ``revoked`` is a new row. A user without a
company cannot decide. Another company's proposal is a 404.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Json

from ontology.loader import Ontology
from services.agent.payment_match import money
from services.audit import record
from services.cash.catalog import CASH_PROCESS_ID, STEP_CONFIRMED
from services.cash.documents import confirm_allocation_status
from services.cash.events import record_process_event
from services.companies import USER_WITHOUT_COMPANY
from services.ontology_view import payment_label
from services.errors import AppError
from services.mapping.corrections import company_id_for_user

_OPEN = "issued"


def confirm_proposal(
    conn: psycopg.Connection,
    ontology: Ontology,
    proposal_id: str,
    *,
    actor: str,
    role: str,
    evidence_hash: str,
) -> dict[str, Any]:
    company_id = _company(conn, actor)
    proposal = _proposal(conn, proposal_id, company_id)
    _fresh(proposal, evidence_hash)
    _refuse_if_decided(conn, proposal_id)
    decision_id = _insert(
        conn,
        proposal,
        decision="confirm",
        actor=actor,
        role=role,
        company_id=company_id,
        invoice_id=proposal["invoice_entity_id"],
        allocated_amount=proposal["allocated_amount"],
        failed_criteria=[],
        reason="",
    )
    confirm_allocation_status(conn, ontology, proposal["allocation_entity_id"], source=proposal["source_name"])
    _event(conn, ontology, proposal, "match_confirmed", "Abbinamento confermato da una persona")
    _audit(conn, actor, role, decision_id, "confirm", proposal_id)
    _assert_invoice_open(conn, proposal["invoice_entity_id"])
    return {"id": decision_id, "decision": "confirm", "failed_criteria": []}


def correct_proposal(
    conn: psycopg.Connection,
    ontology: Ontology,
    proposal_id: str,
    *,
    actor: str,
    role: str,
    evidence_hash: str,
    allocated_amount: Decimal | int | float | str,
    reason: str,
    invoice_id: str | None = None,
    invoice_number: str | None = None,
) -> dict[str, Any]:
    company_id = _company(conn, actor)
    proposal = _proposal(conn, proposal_id, company_id)
    _fresh(proposal, evidence_hash)
    _refuse_if_decided(conn, proposal_id)
    invoice = _resolve_invoice(conn, company_id, invoice_id, invoice_number)
    amount = money(allocated_amount)
    if amount <= 0:
        raise AppError(400, "L'importo assegnato deve essere positivo")
    failed = _failed(conn, proposal, invoice, amount)
    decision_id = _insert(
        conn,
        proposal,
        decision="correct",
        actor=actor,
        role=role,
        company_id=company_id,
        invoice_id=invoice["id"],
        allocated_amount=amount,
        failed_criteria=failed,
        reason=reason,
    )
    _event(conn, ontology, proposal, "match_corrected", "Abbinamento corretto da una persona")
    _audit(conn, actor, role, decision_id, "correct", proposal_id)
    _assert_invoice_open(conn, proposal["invoice_entity_id"])
    _assert_invoice_open(conn, invoice["id"])
    return {"id": decision_id, "decision": "correct", "failed_criteria": failed}


def correct_unmatched(
    conn: psycopg.Connection,
    ontology: Ontology,
    payment_id: str,
    *,
    actor: str,
    role: str,
    allocated_amount: Decimal | int | float | str,
    reason: str,
    invoice_id: str | None = None,
    invoice_number: str | None = None,
) -> dict[str, Any]:
    """A person matches a payment the rule left open. The invoice stays open."""
    company_id = _company(conn, actor)
    payment = _payment(conn, payment_id, company_id)
    invoice = _resolve_invoice(conn, company_id, invoice_id, invoice_number)
    amount = money(allocated_amount)
    if amount <= 0:
        raise AppError(400, "L'importo assegnato deve essere positivo")
    decision_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.payment_decisions (
          id, proposal_id, payment_entity_id, company_id, decision, actor, actor_role,
          invoice_entity_id, allocated_amount, failed_criteria, reason, source_name
        )
        VALUES (%s, NULL, %s, %s, 'correct', %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            decision_id,
            payment["id"],
            company_id,
            actor,
            role,
            invoice["id"],
            amount,
            Json(["amount", "customer", "reference"]),
            reason,
            payment["source_name"],
        ),
    )
    record_process_event(
        conn,
        ontology,
        company_id=company_id,
        subject_id=payment["id"],
        event_type="match_corrected",
        summary=f"{payment_label(payment.get('direction'))} abbinato da una persona",
        occurred_at=payment["paid_at"] or "correzione",
        process_id=CASH_PROCESS_ID,
        step_id=STEP_CONFIRMED,
        source=payment["source_name"] or "incasso",
        source_class="manual",
    )
    _audit(conn, actor, role, decision_id, "correct", payment_id)
    _assert_invoice_open(conn, invoice["id"])
    return {
        "id": decision_id,
        "decision": "correct",
        "failed_criteria": ["amount", "customer", "reference"],
    }


def reject_proposal(
    conn: psycopg.Connection,
    ontology: Ontology,
    proposal_id: str,
    *,
    actor: str,
    role: str,
    evidence_hash: str,
    reason: str,
) -> dict[str, Any]:
    company_id = _company(conn, actor)
    proposal = _proposal(conn, proposal_id, company_id)
    _fresh(proposal, evidence_hash)
    _refuse_if_decided(conn, proposal_id)
    decision_id = _insert(
        conn,
        proposal,
        decision="reject",
        actor=actor,
        role=role,
        company_id=company_id,
        invoice_id=None,
        allocated_amount=None,
        failed_criteria=["amount", "customer", "reference"],
        reason=reason,
    )
    _event(conn, ontology, proposal, "match_rejected", "Abbinamento rifiutato da una persona")
    _audit(conn, actor, role, decision_id, "reject", proposal_id)
    _assert_invoice_open(conn, proposal["invoice_entity_id"])
    return {"id": decision_id, "decision": "reject", "failed_criteria": ["amount", "customer", "reference"]}


def revoke_payment_decision(
    conn: psycopg.Connection,
    decision_id: str,
    *,
    actor: str,
    role: str,
    reason: str,
) -> dict[str, Any]:
    company_id = _company(conn, actor)
    row = conn.execute(
        """
        SELECT id::text, company_id::text AS company_id, decision, proposal_id::text
        FROM app.payment_decisions
        WHERE id = %s::uuid
        """,
        (decision_id,),
    ).fetchone()
    if row is None or row["company_id"] != company_id:
        raise AppError(404, "Decisione assente")
    if row["decision"] == "revoked":
        raise AppError(409, "La revoca non si revoca")
    new_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.payment_decisions (
          id, proposal_id, payment_entity_id, company_id, decision, actor, actor_role,
          reason, revokes_decision_id, source_name
        )
        SELECT %s, proposal_id, payment_entity_id, company_id, 'revoked', %s, %s,
               %s, id, source_name
        FROM app.payment_decisions
        WHERE id = %s::uuid
        """,
        (new_id, actor, role, reason, decision_id),
    )
    _audit(conn, actor, role, new_id, "revoked", decision_id)
    return {"id": new_id, "decision": "revoked", "revokes": decision_id}


def _company(conn, actor: str) -> str:
    company_id = company_id_for_user(conn, actor)
    if not company_id:
        raise AppError(403, USER_WITHOUT_COMPANY)
    return company_id


def _proposal(conn, proposal_id: str, company_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id::text, company_id::text AS company_id, payment_entity_id::text,
               invoice_entity_id::text, allocation_entity_id::text, source_name,
               allocated_amount, evidence_hash, label_origin, criteria
        FROM app.payment_proposals
        WHERE id = %s::uuid
        """,
        (proposal_id,),
    ).fetchone()
    if row is None or row["company_id"] != company_id:
        raise AppError(404, "Proposta assente")
    return dict(row)


def _resolve_invoice(
    conn,
    company_id: str,
    invoice_id: str | None,
    invoice_number: str | None,
) -> dict[str, Any]:
    """An id, or the invoice number inside this company. Never the other company."""
    number = (invoice_number or "").strip()
    if number and invoice_id:
        raise AppError(422, "Indica il numero della fattura")
    if number:
        rows = conn.execute(
            """
            SELECT id::text, company_id::text AS company_id, business_key
            FROM app.entities
            WHERE type = 'Invoice' AND valid_to IS NULL
              AND company_id = %s::uuid
              AND business_key = %s
            """,
            (company_id, number),
        ).fetchall()
        if len(rows) != 1:
            raise AppError(404, "Fattura assente")
        return dict(rows[0])
    if not invoice_id:
        raise AppError(422, "Indica il numero della fattura")
    return _invoice(conn, invoice_id, company_id)


def _invoice(conn, invoice_id: str, company_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id::text, company_id::text AS company_id, business_key
        FROM app.entities
        WHERE id = %s::uuid AND type = 'Invoice' AND valid_to IS NULL
        """,
        (invoice_id,),
    ).fetchone()
    if row is None or row["company_id"] != company_id:
        raise AppError(404, "Fattura assente")
    return dict(row)


def _payment(conn, payment_id: str, company_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT e.id::text, e.company_id::text AS company_id,
               d.source_name,
               f.value #>> '{}' AS paid_at,
               dir.value #>> '{}' AS direction
        FROM app.entities e
        LEFT JOIN app.cash_documents d ON d.entity_id = e.id
        LEFT JOIN app.facts f
          ON f.entity_id = e.id AND f.property = 'paid_at' AND f.valid_to IS NULL
        LEFT JOIN app.facts dir
          ON dir.entity_id = e.id AND dir.property = 'direction' AND dir.valid_to IS NULL
        WHERE e.id = %s::uuid AND e.type = 'Payment' AND e.valid_to IS NULL
        """,
        (payment_id,),
    ).fetchone()
    if row is None or row["company_id"] != company_id:
        raise AppError(404, "Incasso assente")
    return dict(row)


def _fresh(proposal: dict[str, Any], evidence_hash: str) -> None:
    if evidence_hash != proposal["evidence_hash"]:
        raise AppError(409, "Le evidenze sono cambiate")


def _refuse_if_decided(conn, proposal_id: str) -> None:
    row = conn.execute(
        """
        SELECT d.decision
        FROM app.payment_decisions d
        WHERE d.proposal_id = %s::uuid
          AND d.decision <> 'revoked'
          AND NOT EXISTS (
            SELECT 1 FROM app.payment_decisions later
            WHERE later.revokes_decision_id = d.id
          )
        ORDER BY d.decided_at DESC, d.id DESC
        LIMIT 1
        """,
        (proposal_id,),
    ).fetchone()
    if row is not None:
        raise AppError(409, "La proposta ha già una decisione")


def _failed(conn, proposal: dict[str, Any], invoice: dict[str, Any], amount: Decimal) -> list[str]:
    failed = []
    if money(proposal["allocated_amount"]) != amount:
        failed.append("amount")
    if invoice["id"] != proposal["invoice_entity_id"]:
        failed.append("reference")
        if _vat_of(conn, invoice["id"]) != _vat_of(conn, proposal["invoice_entity_id"]):
            failed.append("customer")
    return failed


def _vat_of(conn, invoice_id: str) -> str:
    from services.pii import read_cleartext
    from services.text import stable

    rows = read_cleartext(
        conn,
        """
        SELECT value_cleartext
        FROM app.facts
        WHERE entity_id = %s AND property = 'customer_vat' AND valid_to IS NULL
        """,
        (invoice_id,),
        purpose="payment_correction",
        entity_ref=invoice_id,
    )
    if not rows:
        return ""
    return str(stable(rows[0]["value_cleartext"]) or "")


def _insert(
    conn,
    proposal: dict[str, Any],
    *,
    decision: str,
    actor: str,
    role: str,
    company_id: str,
    invoice_id: str | None,
    allocated_amount: Decimal | None,
    failed_criteria: list[str],
    reason: str,
) -> str:
    decision_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.payment_decisions (
          id, proposal_id, payment_entity_id, company_id, decision, actor, actor_role,
          invoice_entity_id, allocated_amount, failed_criteria, reason, source_name
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            decision_id,
            proposal["id"],
            proposal["payment_entity_id"],
            company_id,
            decision,
            actor,
            role,
            invoice_id,
            allocated_amount,
            Json(failed_criteria),
            reason,
            proposal["source_name"],
        ),
    )
    return decision_id


def _event(conn, ontology: Ontology, proposal: dict[str, Any], event_type: str, summary: str) -> None:
    record_process_event(
        conn,
        ontology,
        company_id=proposal["company_id"],
        subject_id=proposal["payment_entity_id"],
        event_type=event_type,
        summary=summary,
        occurred_at="decisione",
        process_id=CASH_PROCESS_ID,
        step_id=STEP_CONFIRMED,
        source=proposal["source_name"],
        source_class="manual",
    )


def _audit(conn, actor: str, role: str, decision_id: str, decision: str, entity_ref: str) -> None:
    record(
        conn,
        action="human_decision",
        actor=actor,
        role=role,
        entity_ref=entity_ref,
        payload={"decision": decision, "payment_decision_id": decision_id},
    )


def _assert_invoice_open(conn, invoice_id: str) -> None:
    row = conn.execute(
        """
        SELECT value #>> '{}' AS status
        FROM app.facts
        WHERE entity_id = %s AND property = 'status' AND valid_to IS NULL
        """,
        (invoice_id,),
    ).fetchone()
    status = (row["status"] if row else "") or ""
    if status.lower() in {"closed", "paid", "settled", "saldata", "chiusa"}:
        raise AppError(409, "La fattura non si chiude da sola")
    if status not in ("", _OPEN):
        raise AppError(409, "La fattura non si chiude da sola")
