"""Read models for object pages, the inbox, and the audit trail."""

from __future__ import annotations

from typing import Any

import psycopg

from services.text import stable

MASK = "••••"
STOCK_FORMULA = "disponibile = giacenza − impegnato"
PRIVILEGED = {"admin", "approver"}
SENSITIVE_KEYS = {"vat", "amount", "cost", "margin", "piva", "costo", "importo"}


def list_mappings(conn: psycopg.Connection, role: str, ontology: Any) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id::text, source_name, source_field, target_type, target_property,
               confidence, status, samples, profile
        FROM app.field_mappings
        WHERE valid_to IS NULL
        ORDER BY source_name, confidence DESC, source_field
        """
    ).fetchall()
    visible = []
    for row in rows:
        item = dict(row)
        prop = ontology.prop(item["target_type"], item["target_property"]) if ontology else None
        if role not in PRIVILEGED and prop is not None and prop.classification == "confidential":
            item["samples"] = [MASK for _ in (item["samples"] or [])]
        visible.append(item)
    return visible


def list_drift(conn: psycopg.Connection) -> list[dict]:
    return list(
        conn.execute(
            """
            SELECT id::text, source_name, previous_hash, new_hash, diff, status, created_at
            FROM app.drift_reviews
            ORDER BY created_at DESC
            """
        ).fetchall()
    )


def list_orders(conn: psycopg.Connection, role: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id::text, business_key FROM app.entities
        WHERE type = 'Order' AND valid_to IS NULL
        ORDER BY business_key
        """
    ).fetchall()
    return [_order_summary(conn, row, role) for row in rows]


def get_order(conn: psycopg.Connection, order_id: str, role: str) -> dict | None:
    row = conn.execute(
        """
        SELECT id::text, type, business_key FROM app.entities
        WHERE id = %s AND type = 'Order' AND valid_to IS NULL
        """,
        (order_id,),
    ).fetchone()
    if row is None:
        return None
    summary = _order_summary(conn, row, role)
    summary["facts"] = _facts(conn, order_id, role)
    summary["lines"] = _lines(conn, row["business_key"], role)
    summary["edges"] = _edges(conn, order_id)
    summary["stock_formula"] = STOCK_FORMULA
    customer_key = next((fact["value"] for fact in summary["facts"] if fact["property"] == "customer_source_key"), None)
    if customer_key and customer_key != MASK:
        customer_id = _alias(conn, "Customer", str(customer_key))
        summary["customer_id"] = customer_id
    return summary


def get_customer(conn: psycopg.Connection, customer_id: str, role: str) -> dict | None:
    row = conn.execute(
        """
        SELECT id::text, business_key FROM app.entities
        WHERE id = %s AND type = 'Customer' AND valid_to IS NULL
        """,
        (customer_id,),
    ).fetchone()
    if row is None:
        return None
    facts = _facts(conn, customer_id, role)
    orders = conn.execute(
        """
        SELECT e.id::text, e.business_key
        FROM app.graph_edges g
        JOIN app.entities e ON e.id = g.to_id
        WHERE g.from_id = %s AND g.rel = 'places' AND g.valid_to IS NULL
        ORDER BY e.business_key
        """,
        (customer_id,),
    ).fetchall()
    return {
        "id": row["id"],
        "business_key": row["business_key"],
        "company_name": _visible(facts, "company_name"),
        "facts": facts,
        "orders": list(orders),
        "edges": _edges(conn, customer_id),
    }


def list_inbox(conn: psycopg.Connection, role: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT r.id::text, r.order_entity_id::text, e.business_key AS order_key,
               r.problem, r.evidence, r.evidence_hash, r.actions, r.expected_impact,
               r.risk, r.confidence, r.confidence_method, r.confidence_rule,
               r.status, r.requires_cfo, r.created_at
        FROM app.recommendations r
        JOIN app.entities e ON e.id = r.order_entity_id
        ORDER BY r.created_at DESC
        """
    ).fetchall()
    inbox = []
    for row in rows:
        payloads = conn.execute(
            """
            SELECT payloads FROM app.action_batches
            WHERE recommendation_id = %s AND kind = 'dry_run'
            ORDER BY created_at DESC LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        item = dict(row)
        item["confidence"] = stable(item["confidence"])
        evidence = _mask_evidence(item["evidence"], role)
        classes = _fact_classes(conn, _fact_ids(evidence))
        item["evidence"] = _with_provenance(evidence, classes)
        item["blocked_reason"] = _blocked_reason(item, classes)
        from services.actions.executor import payloads_for

        preview_payloads = payloads["payloads"] if payloads else payloads_for(conn, item)
        item["preview"] = {
            "formula": STOCK_FORMULA,
            "payloads": _mask_tree(preview_payloads, role),
        }
        item["dry_run"] = item["preview"]["payloads"] if payloads else None
        inbox.append(item)
    return inbox


def list_audit(conn: psycopg.Connection, role: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id::text, at, actor, role, action, entity_ref, payload
        FROM app.audit_log
        ORDER BY at DESC
        LIMIT 200
        """
    ).fetchall()
    visible = []
    for row in rows:
        item = dict(row)
        item["payload"] = _mask_tree(item["payload"], role)
        visible.append(item)
    return visible


def _fact_ids(evidence: Any) -> list[str]:
    if not isinstance(evidence, list):
        return []
    return [
        item.get("fact_id")
        for item in evidence
        if isinstance(item, dict) and item.get("fact_id")
    ]


def _fact_classes(conn: psycopg.Connection, fact_ids: list[str]) -> dict[str, str | None]:
    if not fact_ids:
        return {}
    rows = conn.execute(
        "SELECT id::text AS id, source_class FROM app.facts WHERE id = ANY(%s::uuid[])",
        (fact_ids,),
    ).fetchall()
    return {row["id"]: row["source_class"] for row in rows}


def _blocked_reason(item: dict, classes: dict[str, str | None]) -> str | None:
    """Why approval is refused. Reads only: the inbox does not write."""
    if item["status"] not in ("proposed", "modified"):
        return None
    fact_ids = _fact_ids(item["evidence"])
    if not fact_ids:
        return "provenienza sconosciuta"
    if len(classes) != len(set(fact_ids)) or any(
        classes.get(fact_id) in (None, "unknown") for fact_id in fact_ids
    ):
        return "provenienza sconosciuta"
    return None


def _with_provenance(evidence: Any, classes: dict[str, str | None]) -> Any:
    """Attach the stored source class for display. It is not part of the evidence hash."""
    if not isinstance(evidence, list) or not classes:
        return evidence
    enriched = []
    for item in evidence:
        if not isinstance(item, dict):
            enriched.append(item)
            continue
        copy = dict(item)
        fact_id = copy.get("fact_id")
        if fact_id in classes:
            copy["source_class"] = classes[fact_id]
        enriched.append(copy)
    return enriched


def _mask_evidence(evidence: Any, role: str) -> Any:
    if role in PRIVILEGED or not isinstance(evidence, list):
        return evidence
    masked = []
    for item in evidence:
        if not isinstance(item, dict):
            masked.append(item)
            continue
        copy = dict(item)
        if copy.get("classification") == "confidential" or copy.get("property") in SENSITIVE_KEYS:
            copy["value"] = MASK
            copy["masked"] = True
        masked.append(copy)
    return masked


def _mask_tree(value: Any, role: str) -> Any:
    if role in PRIVILEGED:
        return value
    if isinstance(value, list):
        return [_mask_tree(item, role) for item in value]
    if isinstance(value, dict):
        classification = value.get("classification")
        prop = value.get("property")
        masked = {}
        for key, item in value.items():
            if key == "value" and (classification == "confidential" or prop in SENSITIVE_KEYS):
                masked[key] = MASK
            elif key in SENSITIVE_KEYS:
                masked[key] = MASK
            else:
                masked[key] = _mask_tree(item, role)
        return masked
    return value


def _order_summary(conn: psycopg.Connection, row: dict, role: str) -> dict:
    facts = _facts(conn, row["id"], role)
    customer_key = _raw_fact(conn, row["id"], "customer_source_key")
    customer_name = None
    customer_id = None
    if customer_key:
        customer_id = _alias(conn, "Customer", str(customer_key))
        if customer_id:
            customer_name = _raw_fact(conn, customer_id, "company_name")
    return {
        "id": row["id"],
        "business_key": row["business_key"],
        "amount": _visible(facts, "amount"),
        "status": _visible(facts, "status"),
        "shortage_qty": _visible(facts, "shortage_qty"),
        "margin": _visible(facts, "margin"),
        "customer_name": customer_name,
        "customer_id": customer_id,
        "stock_formula": STOCK_FORMULA,
    }


def _lines(conn: psycopg.Connection, order_key: str, role: str) -> list[dict]:
    import json

    rows = conn.execute(
        """
        SELECT e.id::text
        FROM app.entities e
        JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL
        WHERE e.type = 'OrderLine' AND e.valid_to IS NULL
          AND f.property = 'order_source_key' AND f.value = %s::jsonb
        ORDER BY e.business_key
        """,
        (json.dumps(order_key),),
    ).fetchall()
    lines = []
    for row in rows:
        facts = _facts(conn, row["id"], role)
        product_key = _raw_fact(conn, row["id"], "product_source_key")
        stock = None
        if product_key:
            stock_row = conn.execute(
                """
                SELECT e.id::text
                FROM app.entities e
                JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL
                WHERE e.type = 'StockPosition'
                  AND f.property = 'product_source_key' AND f.value = %s::jsonb
                LIMIT 1
                """,
                (json.dumps(str(product_key)),),
            ).fetchone()
            if stock_row:
                stock_facts = _facts(conn, stock_row["id"], role)
                stock = {
                    "on_hand": _visible(stock_facts, "on_hand"),
                    "reserved": _visible(stock_facts, "reserved"),
                    "available": _visible(stock_facts, "available"),
                    "formula": STOCK_FORMULA,
                }
        lines.append(
            {
                "id": row["id"],
                "product": product_key,
                "quantity": _visible(facts, "quantity"),
                "shortage": _visible(facts, "shortage"),
                "unit_price": _visible(facts, "unit_price"),
                "stock": stock,
            }
        )
    return lines


def _facts(conn: psycopg.Connection, entity_id: str, role: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id::text, property, value, classification, origin, source,
               confidence, source_class, confidence_method, confidence_rule, valid_from
        FROM app.facts
        WHERE entity_id = %s AND valid_to IS NULL
        ORDER BY property
        """,
        (entity_id,),
    ).fetchall()
    visible = []
    for row in rows:
        item = dict(row)
        item["value"] = stable(item["value"])
        item["confidence"] = stable(item["confidence"])
        if item["classification"] == "confidential" and role not in ("admin", "approver"):
            item["value"] = MASK
            item["masked"] = True
        visible.append(item)
    return visible


def _edges(conn: psycopg.Connection, entity_id: str) -> list[dict]:
    outbound = conn.execute(
        """
        SELECT g.rel, g.to_id::text AS other_id, e.type AS other_type, e.business_key AS other_key, 'out' AS direction
        FROM app.graph_edges g
        JOIN app.entities e ON e.id = g.to_id
        WHERE g.from_id = %s AND g.valid_to IS NULL
        """,
        (entity_id,),
    ).fetchall()
    inbound = conn.execute(
        """
        SELECT g.rel, g.from_id::text AS other_id, e.type AS other_type, e.business_key AS other_key, 'in' AS direction
        FROM app.graph_edges g
        JOIN app.entities e ON e.id = g.from_id
        WHERE g.to_id = %s AND g.valid_to IS NULL
        """,
        (entity_id,),
    ).fetchall()
    return list(outbound) + list(inbound)


def _visible(facts: list[dict], name: str) -> Any:
    for fact in facts:
        if fact["property"] == name:
            return fact["value"]
    return None


def _raw_fact(conn: psycopg.Connection, entity_id: str, name: str) -> Any:
    row = conn.execute(
        """
        SELECT value FROM app.facts
        WHERE entity_id = %s AND property = %s AND valid_to IS NULL
        """,
        (entity_id, name),
    ).fetchone()
    return None if row is None else stable(row["value"])


def _alias(conn: psycopg.Connection, entity_type: str, alias: str) -> str | None:
    row = conn.execute(
        """
        SELECT entity_id::text FROM app.identity_aliases
        WHERE entity_type = %s AND alias = %s
        """,
        (entity_type, alias),
    ).fetchone()
    return row["entity_id"] if row else None
