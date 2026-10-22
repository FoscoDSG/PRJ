"""Remaining balance of an invoice. Read only: the invoice stays open.

Remaining = invoice amount
            − linked credit notes
            − amounts a person confirmed or corrected.

The last decision of each rule proposal counts. A manual allocation,
one a person recorded without a proposal, counts on its own: two
allocations of the same payment are two amounts. A revoked decision
does not count. A match that is only proposed does not. An allocation
that comes from an outgoing payment does not either: ``is_incoming``
is the same rule the measure uses, so the residual and the measure
stay aligned. A payment with no direction still counts. This function
never writes.
"""

from __future__ import annotations

from decimal import Decimal

import psycopg

from services.agent.payment_match import money
from services.ontology_view import is_incoming
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
    """Kept decisions. Proposals themselves are not read.

    A rule proposal keeps its latest decision. A manual allocation has
    no proposal, so each of those decisions counts by itself.
    """
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
        if row["proposal_id"]:
            key = f"proposal:{row['proposal_id']}"
        else:
            key = f"decision:{row['id']}"
        latest[key] = row
    directions = _payment_directions(
        conn,
        {row["payment_entity_id"] for row in latest.values() if row.get("payment_entity_id")},
    )
    total = _ZERO
    for row in latest.values():
        if row["decision"] not in _KEPT:
            continue
        if row["invoice_entity_id"] != invoice_id or row["allocated_amount"] is None:
            continue
        if not is_incoming({"direction": directions.get(row["payment_entity_id"])}):
            continue
        total += money(row["allocated_amount"])
    return money(total)


def _payment_directions(conn: psycopg.Connection, payment_ids: set[str]) -> dict[str, str | None]:
    """Direction fact of each payment. A missing fact is absent from the map."""
    if not payment_ids:
        return {}
    rows = conn.execute(
        """
        SELECT entity_id::text AS id, value #>> '{}' AS direction
        FROM app.facts
        WHERE property = 'direction' AND valid_to IS NULL
          AND entity_id = ANY(%s::uuid[])
        """,
        (list(payment_ids),),
    ).fetchall()
    return {row["id"]: row["direction"] for row in rows}
