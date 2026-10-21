"""Remaining balance of an invoice. Read only: the invoice stays open.

Remaining = invoice amount
            − linked credit notes
            − amounts a person confirmed or corrected.

The last decision of each proposal counts. A revoked decision does not.
A match that is only proposed does not. This function never writes.
"""

from __future__ import annotations

from decimal import Decimal

import psycopg

from services.agent.payment_match import money
from services.text import stable

_ZERO = Decimal("0.00")
_KEPT = frozenset({"confirm", "correct"})


def remaining_balance(
    conn: psycopg.Connection,
    company_id: str,
    invoice_id: str,
) -> Decimal | None:
    """Balance for this company's invoice. Another company is ``None``."""
    row = conn.execute(
        """
        SELECT business_key, company_id::text AS company_id
        FROM app.entities
        WHERE id = %s::uuid AND type = 'Invoice' AND valid_to IS NULL
        """,
        (invoice_id,),
    ).fetchone()
    if row is None or row["company_id"] != company_id:
        return None
    amount = _fact_amount(conn, invoice_id)
    credits = _credit_notes(conn, company_id, row["business_key"])
    allocated = _human_allocations(conn, company_id, invoice_id)
    return money(amount - credits - allocated)


def _fact_amount(conn: psycopg.Connection, entity_id: str) -> Decimal:
    row = conn.execute(
        """
        SELECT value FROM app.facts
        WHERE entity_id = %s::uuid AND property = 'amount' AND valid_to IS NULL
        """,
        (entity_id,),
    ).fetchone()
    if row is None or row["value"] is None:
        return _ZERO
    return money(stable(row["value"]))


def _credit_notes(conn: psycopg.Connection, company_id: str, invoice_number: str) -> Decimal:
    import json

    rows = conn.execute(
        """
        SELECT e.id::text
        FROM app.entities e
        JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL
        WHERE e.type = 'CreditNote' AND e.valid_to IS NULL
          AND e.company_id = %s::uuid
          AND f.property = 'invoice_source_key' AND f.value = %s::jsonb
        """,
        (company_id, json.dumps(invoice_number)),
    ).fetchall()
    total = _ZERO
    for row in rows:
        total += _fact_amount(conn, row["id"])
    return money(total)


def _human_allocations(
    conn: psycopg.Connection,
    company_id: str,
    invoice_id: str,
) -> Decimal:
    """Last kept decision per proposal. Proposals themselves are not read."""
    rows = conn.execute(
        """
        SELECT id::text, proposal_id::text AS proposal_id,
               payment_entity_id::text AS payment_entity_id,
               decision, invoice_entity_id::text AS invoice_entity_id,
               allocated_amount, revokes_decision_id::text AS revokes_decision_id
        FROM app.payment_decisions
        WHERE company_id = %s::uuid
        ORDER BY decided_at, id
        """,
        (company_id,),
    ).fetchall()
    revoked = {row["revokes_decision_id"] for row in rows if row["revokes_decision_id"]}
    latest: dict[str, dict] = {}
    for row in rows:
        if row["decision"] == "revoked" or row["id"] in revoked:
            continue
        key = row["proposal_id"] or f"payment:{row['payment_entity_id'] or row['id']}"
        latest[key] = row
    total = _ZERO
    for row in latest.values():
        if row["decision"] not in _KEPT:
            continue
        if row["invoice_entity_id"] != invoice_id or row["allocated_amount"] is None:
            continue
        total += money(row["allocated_amount"])
    return money(total)
