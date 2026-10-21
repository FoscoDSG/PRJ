"""Write invoices, credit notes and payments for one company.

The invoice status written here is ``issued``. Nothing in this module sets
a closed or paid status.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import psycopg

from ontology.loader import Ontology
from services.agent.payment_match import (
    RULE,
    InvoiceView,
    PaymentView,
    money,
    select_matches,
)
from services.canonical.store import (
    _alias,
    _ensure_entity,
    _identity_key,
    _put_fact,
    _vat,
    _write_facts,
)
from services.cash.catalog import (
    CASH_PROCESS_ID,
    STEP_CONFIRMED,
    STEP_CREDITED,
    STEP_INVOICED,
    STEP_MATCH,
    STEP_PAYMENT,
)
from services.cash.events import record_process_event
from services.evidence import evidence_hmac
from services.graph.projection import project_graph
from services.pii import MASK, is_pii_property, read_cleartext
from services.sources import register_source
from services.text import stable

OPEN_INVOICE = "issued"
_PII_FACTS = (
    "causale",
    "iban",
    "customer_name",
    "customer_vat",
    "counterparty_name",
    "counterparty_vat",
    "vat",
    "company_name",
)


def remember_document(
    conn: psycopg.Connection,
    *,
    entity_id: str,
    company_id: str,
    source_name: str,
    label_origin: str,
) -> None:
    if label_origin not in ("rule", "synthetic"):
        raise ValueError("label_origin non ammesso")
    conn.execute(
        """
        INSERT INTO app.cash_documents (entity_id, company_id, source_name, label_origin)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (entity_id) DO NOTHING
        """,
        (entity_id, company_id, source_name, label_origin),
    )


def write_customer(
    conn: psycopg.Connection,
    ontology: Ontology,
    *,
    company_id: str,
    name: str,
    vat: str,
    source: str,
    source_class: str,
) -> str:
    normalized = _vat(vat) or ""
    business_key = _identity_key(normalized) or name
    entity_id = _ensure_entity(conn, "Customer", business_key, company_id)
    _alias(conn, "Customer", normalized or name, entity_id)
    _write_facts(
        conn,
        ontology,
        entity_id,
        "Customer",
        {"source_key": business_key, "company_name": name, "vat": normalized},
        source,
        {},
        source_classes={"company_name": source_class, "vat": source_class, "source_key": source_class},
    )
    return entity_id


def write_supplier(
    conn: psycopg.Connection,
    ontology: Ontology,
    *,
    company_id: str,
    name: str,
    vat: str,
    source: str,
    source_class: str,
) -> str:
    normalized = _vat(vat) or ""
    business_key = _identity_key(normalized) or name
    entity_id = _ensure_entity(conn, "Supplier", business_key, company_id)
    _alias(conn, "Supplier", normalized or name, entity_id)
    _write_facts(
        conn,
        ontology,
        entity_id,
        "Supplier",
        {"source_key": business_key, "name": name, "vat": normalized},
        source,
        {},
        source_classes={"name": source_class, "vat": source_class, "source_key": source_class},
    )
    return entity_id


def write_invoice(
    conn: psycopg.Connection,
    ontology: Ontology,
    *,
    company_id: str,
    number: str,
    document_type: str,
    issued_at: str,
    amount: Decimal,
    currency: str,
    customer_name: str,
    customer_vat: str,
    order_number: str | None,
    due_dates: list[str],
    lines: list[dict[str, Any]],
    source: str,
    source_class: str,
    label_origin: str,
) -> str:
    if not company_id:
        raise ValueError("Fattura senza azienda")
    customer_id = write_customer(
        conn,
        ontology,
        company_id=company_id,
        name=customer_name,
        vat=customer_vat,
        source=source,
        source_class=source_class,
    )
    entity_id = _ensure_entity(conn, "Invoice", number, company_id)
    _alias(conn, "Invoice", number, entity_id)
    due = sorted(due_dates)[0] if due_dates else ""
    payload = {
        "source_key": number,
        "document_type": document_type,
        "issued_at": issued_at,
        "due_at": due,
        "scadenze": ", ".join(sorted(due_dates)),
        "amount": money(amount),
        "currency": currency or "EUR",
        "customer_vat": _vat(customer_vat) or "",
        "customer_name": customer_name,
        "status": OPEN_INVOICE,
    }
    if order_number:
        payload["order_source_key"] = order_number
    _write_facts(
        conn,
        ontology,
        entity_id,
        "Invoice",
        payload,
        source,
        {},
        source_classes={name: source_class for name in payload},
    )
    for index, line in enumerate(lines, start=1):
        line_key = f"{number}|{line.get('number') or index}"
        line_id = _ensure_entity(conn, "InvoiceLine", line_key, company_id)
        line_payload = {
            "source_key": line_key,
            "description": line.get("description") or "",
            "quantity": line.get("quantity"),
            "unit_price": line.get("unit_price"),
            "line_amount": money(line["line_amount"]),
            "invoice_source_key": number,
            "vat_rate": line.get("vat_rate"),
        }
        _write_facts(
            conn,
            ontology,
            line_id,
            "InvoiceLine",
            {key: value for key, value in line_payload.items() if value not in (None, "")},
            source,
            {},
            source_classes={key: source_class for key in line_payload},
        )
    remember_document(
        conn,
        entity_id=entity_id,
        company_id=company_id,
        source_name=source,
        label_origin=label_origin,
    )
    record_process_event(
        conn,
        ontology,
        company_id=company_id,
        subject_id=entity_id,
        event_type="invoice_issued",
        summary=f"Fattura {number} emessa",
        occurred_at=issued_at,
        process_id=CASH_PROCESS_ID,
        step_id=STEP_INVOICED,
        source=source,
        source_class=source_class,
    )
    del customer_id
    return entity_id


def write_credit_note(
    conn: psycopg.Connection,
    ontology: Ontology,
    *,
    company_id: str,
    number: str,
    issued_at: str,
    amount: Decimal,
    invoice_number: str | None,
    customer_vat: str,
    reason: str,
    source: str,
    source_class: str,
    label_origin: str,
) -> str:
    entity_id = _ensure_entity(conn, "CreditNote", number, company_id)
    _alias(conn, "CreditNote", number, entity_id)
    payload = {
        "source_key": number,
        "document_type": "TD04",
        "issued_at": issued_at,
        "amount": money(amount),
        "customer_vat": _vat(customer_vat) or "",
        "reason": reason,
        "status": OPEN_INVOICE,
    }
    if invoice_number:
        payload["invoice_source_key"] = invoice_number
    _write_facts(
        conn,
        ontology,
        entity_id,
        "CreditNote",
        payload,
        source,
        {},
        source_classes={name: source_class for name in payload},
    )
    remember_document(
        conn,
        entity_id=entity_id,
        company_id=company_id,
        source_name=source,
        label_origin=label_origin,
    )
    record_process_event(
        conn,
        ontology,
        company_id=company_id,
        subject_id=entity_id,
        event_type="credit_note_issued",
        summary=f"Nota di credito {number}",
        occurred_at=issued_at,
        process_id=CASH_PROCESS_ID,
        step_id=STEP_CREDITED,
        source=source,
        source_class=source_class,
    )
    return entity_id


def write_payment(
    conn: psycopg.Connection,
    ontology: Ontology,
    *,
    company_id: str,
    source_key: str,
    direction: str,
    paid_at: str,
    amount: Decimal,
    iban: str,
    causale: str,
    counterparty_name: str,
    counterparty_vat: str,
    reference: str,
    source: str,
    source_class: str,
    label_origin: str,
) -> str:
    if direction not in ("incoming", "outgoing"):
        raise ValueError("Direzione pagamento non ammessa")
    entity_id = _ensure_entity(conn, "Payment", source_key, company_id)
    _alias(conn, "Payment", source_key, entity_id)
    payload = {
        "source_key": source_key,
        "direction": direction,
        "paid_at": paid_at,
        "amount": money(amount),
        "iban": iban,
        "causale": causale,
        "counterparty_name": counterparty_name,
        "counterparty_vat": _vat(counterparty_vat) or "",
        "reference": reference,
        "status": "received",
    }
    _write_facts(
        conn,
        ontology,
        entity_id,
        "Payment",
        {key: value for key, value in payload.items() if value not in (None, "")},
        source,
        {},
        source_classes={key: source_class for key in payload},
    )
    remember_document(
        conn,
        entity_id=entity_id,
        company_id=company_id,
        source_name=source,
        label_origin=label_origin,
    )
    record_process_event(
        conn,
        ontology,
        company_id=company_id,
        subject_id=entity_id,
        event_type="payment_received",
        summary="Incasso registrato",
        occurred_at=paid_at,
        process_id=CASH_PROCESS_ID,
        step_id=STEP_PAYMENT,
        source=source,
        source_class=source_class,
    )
    return entity_id


def import_batch(
    conn: psycopg.Connection,
    ontology: Ontology,
    *,
    company_id: str,
    source_name: str,
    source_class: str,
    label_origin: str,
    invoices: list[dict[str, Any]],
    credit_notes: list[dict[str, Any]],
    payments: list[dict[str, Any]],
) -> dict[str, Any]:
    register_source(conn, source_name, source_class, company_id)
    invoice_ids = []
    for item in invoices:
        invoice_ids.append(
            write_invoice(
                conn,
                ontology,
                company_id=company_id,
                source=source_name,
                source_class=source_class,
                label_origin=label_origin,
                **item,
            )
        )
    credit_ids = []
    for item in credit_notes:
        credit_ids.append(
            write_credit_note(
                conn,
                ontology,
                company_id=company_id,
                source=source_name,
                source_class=source_class,
                label_origin=label_origin,
                **item,
            )
        )
    payment_ids = []
    for item in payments:
        payment_ids.append(
            write_payment(
                conn,
                ontology,
                company_id=company_id,
                source=source_name,
                source_class=source_class,
                label_origin=label_origin,
                **item,
            )
        )
    project_graph(conn)
    return {
        "invoices": invoice_ids,
        "credit_notes": credit_ids,
        "payments": payment_ids,
    }


def propose_company_matches(
    conn: psycopg.Connection,
    ontology: Ontology,
    company_id: str,
) -> dict[str, Any]:
    """Propose matches for this company only. Does not close any invoice."""
    if not company_id:
        return {"proposals": [], "unmatched": []}
    invoices, payments = _books(conn, company_id)
    result = select_matches(payments, invoices)
    created = []
    for proposal in result.proposals:
        stored = _store_proposal(conn, ontology, company_id, proposal)
        if stored:
            created.append(stored)
    project_graph(conn)
    return {"proposals": created, "unmatched": result.unmatched_payment_ids}


def _store_proposal(conn, ontology: Ontology, company_id: str, proposal) -> str | None:
    existing = conn.execute(
        """
        SELECT id::text FROM app.payment_proposals
        WHERE company_id = %s::uuid
          AND payment_entity_id = %s::uuid
          AND invoice_entity_id = %s::uuid
        """,
        (company_id, proposal.payment_id, proposal.invoice_id),
    ).fetchone()
    if existing:
        return existing["id"]
    payment_key = _plain(conn, proposal.payment_id, "source_key")
    allocation_key = f"{payment_key}|{proposal.invoice_number}|{proposal.allocated_amount}"
    allocation_id = _ensure_entity(conn, "PaymentAllocation", allocation_key, company_id)
    _alias(conn, "PaymentAllocation", allocation_key, allocation_id)
    payload = {
        "source_key": allocation_key,
        "payment_source_key": payment_key,
        "invoice_source_key": proposal.invoice_number,
        "allocated_amount": proposal.allocated_amount,
        "status": "proposed",
    }
    _write_facts(
        conn,
        ontology,
        allocation_id,
        "PaymentAllocation",
        payload,
        proposal.source_name,
        {name: proposal.score for name in payload},
        confidence_rules={name: RULE for name in payload},
    )
    evidence = _evidence(conn, proposal.payment_id, proposal.invoice_id, allocation_id)
    fact_ids = [item["fact_id"] for item in evidence]
    digest = evidence_hmac(conn, fact_ids) if fact_ids else evidence_hmac(conn, _any_fact(conn, proposal.payment_id))
    proposal_id = str(uuid.uuid4())
    group = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{company_id}:{proposal.payment_id}"))
    conn.execute(
        """
        INSERT INTO app.payment_proposals (
          id, company_id, payment_entity_id, invoice_entity_id, allocation_entity_id,
          match_group, source_name, label_origin, criteria, score, confidence_method,
          confidence_rule, allocated_amount, evidence, evidence_hash, process_id, step_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'rule', %s, %s, %s, %s, %s, %s)
        """,
        (
            proposal_id,
            company_id,
            proposal.payment_id,
            proposal.invoice_id,
            allocation_id,
            group,
            proposal.source_name or "incasso",
            "synthetic" if proposal.label_origin == "synthetic" else "rule",
            psycopg.types.json.Json(proposal.criteria),
            proposal.score,
            RULE,
            proposal.allocated_amount,
            psycopg.types.json.Json(evidence),
            digest,
            CASH_PROCESS_ID,
            STEP_MATCH,
        ),
    )
    paid_at = _plain(conn, proposal.payment_id, "paid_at") or ""
    record_process_event(
        conn,
        ontology,
        company_id=company_id,
        subject_id=proposal.payment_id,
        event_type="match_proposed",
        summary="Abbinamento proposto, fattura non chiusa",
        occurred_at=paid_at or "proposta",
        process_id=CASH_PROCESS_ID,
        step_id=STEP_MATCH,
        source=proposal.source_name or "incasso",
        source_class="external",
    )
    return proposal_id


def confirm_allocation_status(
    conn: psycopg.Connection,
    ontology: Ontology,
    allocation_id: str,
    *,
    source: str,
) -> None:
    """Human confirmation. A later automatic import does not overwrite it."""
    prop = ontology.require_prop("PaymentAllocation", "status")
    _put_fact(
        conn,
        entity_id=allocation_id,
        name="status",
        value="confirmed",
        classification=prop.classification,
        origin="approved",
        source=source,
        confidence=1,
        source_class="manual",
        confidence_method="human",
        confidence_rule=None,
    )


def _books(conn, company_id: str) -> tuple[list[InvoiceView], list[PaymentView]]:
    invoice_rows = _entities(conn, "Invoice", company_id)
    credit_rows = _entities(conn, "CreditNote", company_id)
    payment_rows = _entities(conn, "Payment", company_id)
    ids = [row["id"] for row in (*invoice_rows, *credit_rows, *payment_rows)]
    plain = _plain_map(conn, ids)
    clear = _clear_map(conn, ids)
    credits_by_invoice: dict[str, Decimal] = {}
    for row in credit_rows:
        facts = plain.get(row["id"], {})
        personal = clear.get(row["id"], {})
        number = str(facts.get("invoice_source_key") or "")
        if not number:
            continue
        credits_by_invoice[number] = money(credits_by_invoice.get(number, 0) + money(facts.get("amount") or 0))
    invoices = []
    for row in invoice_rows:
        facts = plain.get(row["id"], {})
        personal = clear.get(row["id"], {})
        number = str(facts.get("source_key") or row["business_key"])
        residual = money(facts.get("amount") or 0) - credits_by_invoice.get(number, Decimal("0.00"))
        invoices.append(
            InvoiceView(
                entity_id=row["id"],
                number=number,
                residual=residual,
                vat=str(personal.get("customer_vat") or ""),
                name=str(personal.get("customer_name") or ""),
            )
        )
    payments = []
    for row in payment_rows:
        facts = plain.get(row["id"], {})
        personal = clear.get(row["id"], {})
        if facts.get("direction") not in (None, "", "incoming"):
            continue
        origin = _origin(conn, row["id"])
        text = " ".join(
            part
            for part in (personal.get("causale") or "", facts.get("reference") or "")
            if part
        )
        payments.append(
            PaymentView(
                entity_id=row["id"],
                source_key=str(facts.get("source_key") or row["business_key"]),
                amount=money(facts.get("amount") or 0),
                vat=str(personal.get("counterparty_vat") or ""),
                name=str(personal.get("counterparty_name") or ""),
                text=text,
                source_name=origin[0],
                label_origin=origin[1],
                paid_at=str(facts.get("paid_at") or ""),
            )
        )
    return invoices, payments


def _entities(conn, type_name: str, company_id: str) -> list[dict]:
    return list(
        conn.execute(
            """
            SELECT id::text, business_key
            FROM app.entities
            WHERE type = %s AND valid_to IS NULL AND company_id = %s::uuid
            ORDER BY business_key
            """,
            (type_name, company_id),
        ).fetchall()
    )


def _plain_map(conn, entity_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not entity_ids:
        return {}
    rows = conn.execute(
        """
        SELECT entity_id::text, property, value
        FROM app.facts
        WHERE entity_id = ANY(%s::uuid[]) AND valid_to IS NULL
        """,
        (entity_ids,),
    ).fetchall()
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        if is_pii_property(row["property"]):
            continue
        found.setdefault(row["entity_id"], {})[row["property"]] = stable(row["value"])
    return found


def _clear_map(conn, entity_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not entity_ids:
        return {}
    rows = read_cleartext(
        conn,
        """
        SELECT entity_id::text, property, value_cleartext
        FROM app.facts
        WHERE entity_id = ANY(%s::uuid[])
          AND property = ANY(%s)
          AND valid_to IS NULL
          AND value_cleartext IS NOT NULL
        """,
        (entity_ids, list(_PII_FACTS)),
        purpose="payment_match",
        entity_ref=entity_ids[0],
    )
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        found.setdefault(row["entity_id"], {})[row["property"]] = stable(row["value_cleartext"])
    return found


def _origin(conn, entity_id: str) -> tuple[str, str]:
    row = conn.execute(
        """
        SELECT source_name, label_origin FROM app.cash_documents WHERE entity_id = %s::uuid
        """,
        (entity_id,),
    ).fetchone()
    if row is None:
        return "", "rule"
    return row["source_name"], row["label_origin"]


def _plain(conn, entity_id: str, name: str) -> str:
    row = conn.execute(
        """
        SELECT value FROM app.facts
        WHERE entity_id = %s AND property = %s AND valid_to IS NULL
        """,
        (entity_id, name),
    ).fetchone()
    if row is None:
        return ""
    value = stable(row["value"])
    return "" if value is None else str(value)


def _evidence(conn, payment_id: str, invoice_id: str, allocation_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id::text AS fact_id, entity_id::text, property, value, origin, classification
        FROM app.facts
        WHERE entity_id = ANY(%s::uuid[]) AND valid_to IS NULL
          AND property = ANY(%s)
        """,
        (
            [payment_id, invoice_id, allocation_id],
            ["amount", "allocated_amount", "status", "direction", "source_key", "causale", "iban"],
        ),
    ).fetchall()
    evidence = []
    for row in rows:
        value = stable(row["value"])
        if is_pii_property(row["property"]) or value == MASK:
            value = MASK
        evidence.append(
            {
                "fact_id": row["fact_id"],
                "entity_type": "Payment" if row["entity_id"] == payment_id else "Invoice",
                "property": row["property"],
                "value": value,
                "origin": row["origin"],
                "classification": row["classification"],
            }
        )
    return evidence


def _any_fact(conn, entity_id: str) -> list[str]:
    row = conn.execute(
        """
        SELECT id::text AS id FROM app.facts
        WHERE entity_id = %s AND valid_to IS NULL
        LIMIT 1
        """,
        (entity_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Evidenza assente")
    return [row["id"]]


def confirmed_step() -> str:
    return STEP_CONFIRMED
