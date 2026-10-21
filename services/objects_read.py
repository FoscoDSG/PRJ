"""Generic object reads for one company.

The type must exist in the ontology. Personal values leave this module
already replaced with ``riservato``. Amounts stay visible: they are
confidential, not personal. A detail that would show a personal value in
clear calls ``note_clear_personal_view``; that log is not built yet.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg

from ontology.loader import Ontology
from services.cash.balance import remaining_balance
from services.errors import AppError
from services.ontology_view import field_label, type_label
from services.pii import (
    MASK,
    RESERVED,
    clear_personal_names,
    is_pii_property,
    note_clear_personal_view,
    text_has_pii,
)
from services.text import stable


def list_company_objects(
    conn: psycopg.Connection,
    model: Ontology,
    *,
    type_name: str,
    company_id: str | None,
    actor: str,
) -> list[dict[str, Any]]:
    """Objects of one type for this company. An unknown type is a 404."""
    _require_type(model, type_name)
    if not company_id:
        return []
    rows = conn.execute(
        """
        SELECT id::text, type, business_key, company_id::text AS company_id
        FROM app.entities
        WHERE type = %s AND valid_to IS NULL AND company_id = %s::uuid
        ORDER BY business_key
        """,
        (type_name, company_id),
    ).fetchall()
    return [
        _present(conn, model, row, company_id, actor, detail=False)
        for row in rows
    ]


def get_company_object(
    conn: psycopg.Connection,
    model: Ontology,
    *,
    object_id: str,
    company_id: str | None,
    actor: str,
) -> dict[str, Any] | None:
    """One object of this company. Another company, or a bad id, is absent."""
    if not company_id or not _uuid(object_id):
        return None
    row = conn.execute(
        """
        SELECT id::text, type, business_key, company_id::text AS company_id
        FROM app.entities
        WHERE id = %s::uuid AND valid_to IS NULL
        """,
        (object_id,),
    ).fetchone()
    if row is None or row["company_id"] != company_id:
        return None
    if row["type"] not in model.types:
        return None
    return _present(conn, model, row, company_id, actor, detail=True)


def visible_facts(conn: psycopg.Connection, entity_id: str) -> dict[str, Any]:
    """Facts that are not personal. Personal columns are omitted, not opened."""
    rows = conn.execute(
        """
        SELECT property, value
        FROM app.facts
        WHERE entity_id = %s::uuid AND valid_to IS NULL
        ORDER BY property
        """,
        (entity_id,),
    ).fetchall()
    found = {}
    for row in rows:
        if is_pii_property(row["property"]):
            continue
        value = stable(row["value"])
        if value == MASK or (isinstance(value, str) and text_has_pii(value)):
            continue
        found[row["property"]] = value
    return found


def document_history(
    conn: psycopg.Connection,
    entity_id: str,
    type_name: str,
    company_id: str | None,
) -> list[dict[str, Any]]:
    """Events on this object, plus payment decisions for an invoice or a payment."""
    if not company_id:
        return []
    items = _event_history(conn, entity_id, company_id)
    if type_name in ("Invoice", "Payment"):
        items.extend(_decision_history(conn, entity_id, company_id))
    items.sort(key=lambda item: (str(item.get("at") or ""), item.get("kind") or "", item.get("label") or ""))
    return items


def _require_type(model: Ontology, type_name: str) -> None:
    if not type_name or type_name not in model.types:
        raise AppError(404, "Tipo assente")


def _uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def _present(
    conn: psycopg.Connection,
    model: Ontology,
    row: dict,
    company_id: str,
    actor: str,
    *,
    detail: bool,
) -> dict[str, Any]:
    fields = _fields(conn, row["id"], row["type"])
    # The hook is the place a future view log would record a clear personal value.
    note_clear_personal_view(
        conn,
        actor=actor,
        entity_id=row["id"],
        properties=clear_personal_names(fields, row["type"]),
    )
    payload = {
        "id": row["id"],
        "type": row["type"],
        "type_label": type_label(row["type"]),
        "business_key": row["business_key"],
        "fields": fields,
        "links": _links(conn, row["id"], company_id, model),
    }
    if row["type"] == "Invoice":
        balance = remaining_balance(conn, company_id, row["id"])
        payload["remaining"] = None if balance is None else stable(balance)
    if detail:
        payload["history"] = document_history(conn, row["id"], row["type"], company_id)
    return payload


def _fields(conn: psycopg.Connection, entity_id: str, type_name: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT property, value
        FROM app.facts
        WHERE entity_id = %s::uuid AND valid_to IS NULL
        ORDER BY property
        """,
        (entity_id,),
    ).fetchall()
    fields = []
    for row in rows:
        value = stable(row["value"])
        personal = is_pii_property(row["property"], type_name)
        masked = personal or value == MASK or (isinstance(value, str) and text_has_pii(value))
        if masked:
            value = RESERVED
        fields.append(
            {
                "name": row["property"],
                "label": field_label(type_name, row["property"]),
                "value": value,
                "masked": masked,
            }
        )
    return fields


def _links(
    conn: psycopg.Connection,
    entity_id: str,
    company_id: str,
    model: Ontology,
) -> list[dict[str, Any]]:
    outbound = conn.execute(
        """
        SELECT g.rel, g.to_id::text AS other_id, e.type AS other_type,
               e.business_key AS other_key, 'out' AS direction
        FROM app.graph_edges g
        JOIN app.entities e ON e.id = g.to_id
        WHERE g.from_id = %s::uuid AND g.valid_to IS NULL
          AND e.valid_to IS NULL AND e.company_id = %s::uuid
        """,
        (entity_id, company_id),
    ).fetchall()
    inbound = conn.execute(
        """
        SELECT g.rel, g.from_id::text AS other_id, e.type AS other_type,
               e.business_key AS other_key, 'in' AS direction
        FROM app.graph_edges g
        JOIN app.entities e ON e.id = g.from_id
        WHERE g.to_id = %s::uuid AND g.valid_to IS NULL
          AND e.valid_to IS NULL AND e.company_id = %s::uuid
        """,
        (entity_id, company_id),
    ).fetchall()
    known = {item.get("name") for item in model.relations}
    links = []
    for row in list(outbound) + list(inbound):
        if row["rel"] not in known or row["other_type"] not in model.types:
            continue
        links.append(
            {
                "rel": row["rel"],
                "direction": row["direction"],
                "type": row["other_type"],
                "type_label": type_label(row["other_type"]),
                "id": row["other_id"],
                "key": row["other_key"],
            }
        )
    links.sort(key=lambda item: (item["rel"], item["direction"], item["key"] or ""))
    return links


def _event_history(
    conn: psycopg.Connection,
    entity_id: str,
    company_id: str,
) -> list[dict[str, Any]]:
    events = conn.execute(
        """
        SELECT e.id::text
        FROM app.graph_edges g
        JOIN app.entities e ON e.id = g.to_id
        WHERE g.from_id = %s::uuid AND g.rel = 'triggered' AND g.valid_to IS NULL
          AND e.type = 'Event' AND e.valid_to IS NULL AND e.company_id = %s::uuid
        """,
        (entity_id, company_id),
    ).fetchall()
    items = []
    for event in events:
        facts = visible_facts(conn, event["id"])
        label = str(facts.get("summary") or facts.get("event_type") or "")
        items.append(
            {
                "kind": "event",
                "at": str(facts.get("occurred_at") or ""),
                "label": label,
                "actor": "",
                "decision": "",
            }
        )
    return items


def _decision_history(
    conn: psycopg.Connection,
    entity_id: str,
    company_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id::text, decision, actor, decided_at, revokes_decision_id::text AS revokes
        FROM app.payment_decisions
        WHERE company_id = %s::uuid
          AND (invoice_entity_id = %s::uuid OR payment_entity_id = %s::uuid)
        ORDER BY decided_at, id
        """,
        (company_id, entity_id, entity_id),
    ).fetchall()
    revoked = {row["revokes"] for row in rows if row["revokes"]}
    items = []
    for row in rows:
        when = row["decided_at"]
        at = when.isoformat() if hasattr(when, "isoformat") else str(when or "")
        items.append(
            {
                "kind": "decision",
                "at": at,
                "label": row["decision"],
                "actor": row["actor"] or "",
                "decision": row["decision"],
                "revoked": row["decision"] == "revoked" or row["id"] in revoked,
                "id": row["id"],
            }
        )
    return items
