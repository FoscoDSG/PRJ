"""Build order context and validate the model output. Tools are read-only.

Human decisions recorded in the decisions log are not a training source.
This agent does not read that log and does not learn from it.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import jsonschema
import psycopg

from services.text import digest, stable

AGENT_VERSION = "order-agent@v1"

RECOMMENDATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["problem", "evidence", "actions", "expected_impact", "risk", "confidence"],
    "properties": {
        "problem": {"type": "string"},
        "evidence": {"type": "array"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "params"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "createPurchaseOrder",
                            "createOrder",
                            "notifyCustomer",
                            "sendConfirmation",
                        ],
                    },
                    "params": {"type": "object"},
                },
            },
        },
        "expected_impact": {"type": "string"},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


class ModelProvider(Protocol):
    def complete_structured(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        pass


def build_context(conn: psycopg.Connection, order_id: str) -> dict[str, Any]:
    order = conn.execute(
        """
        SELECT id::text, business_key FROM app.entities
        WHERE id = %s AND type = 'Order' AND valid_to IS NULL
        """,
        (order_id,),
    ).fetchone()
    if order is None:
        raise ValueError("Ordine assente")
    order_facts = _fact_rows(conn, order["id"])
    customer_key = _value(order_facts, "customer_source_key")
    customer_id = _alias(conn, "Customer", str(customer_key)) if customer_key else None
    customer_facts = _fact_rows(conn, customer_id) if customer_id else []
    lines = []
    evidence: list[dict[str, Any]] = []
    _collect(evidence, "Order", order["business_key"], order_facts, ("amount", "status", "shortage_qty"))
    _collect(evidence, "Customer", str(customer_key or ""), customer_facts, ("company_name", "vat"))
    line_rows = conn.execute(
        """
        SELECT e.id::text
        FROM app.entities e
        JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL
        WHERE e.type = 'OrderLine' AND e.valid_to IS NULL
          AND f.property = 'order_source_key' AND f.value = %s::jsonb
        """,
        (json.dumps(order["business_key"]),),
    ).fetchall()
    product_keys: list[str] = []
    for line in line_rows:
        facts = _fact_rows(conn, line["id"])
        product_key = str(_value(facts, "product_source_key") or "")
        product_keys.append(product_key)
        product_id = _alias(conn, "Product", product_key)
        product_facts = _fact_rows(conn, product_id) if product_id else []
        lines.append(
            {
                "product": product_key,
                "name": _value(product_facts, "name"),
                "quantity": _value(facts, "quantity") or 0,
                "shortage": _value(facts, "shortage") or 0,
                "unit_price": _value(facts, "unit_price"),
                "cost": _value(product_facts, "cost"),
            }
        )
        _collect(evidence, "OrderLine", f"{order['business_key']}|{product_key}", facts, ("quantity", "shortage", "unit_price"))
        _collect(evidence, "Product", product_key, product_facts, ("cost", "name"))
        stock_id = _stock_for_product(conn, product_key)
        if stock_id:
            stock_facts = _fact_rows(conn, stock_id)
            _collect(evidence, "StockPosition", product_key, stock_facts, ("on_hand", "reserved", "available"))
    suppliers = []
    for product_key in product_keys:
        for supplier in _suppliers_for_product(conn, product_key):
            facts = _fact_rows(conn, supplier["id"])
            suppliers.append(
                {
                    "source_key": supplier["business_key"],
                    "name": _value(facts, "name"),
                    "product": product_key,
                    "delay_days": _value(facts, "delay_days") or 0,
                }
            )
            _collect(evidence, "Supplier", supplier["business_key"], facts, ("delay_days", "name"))
    evidence_sorted = sorted(evidence, key=lambda item: (item["entity_key"], item["property"], item["fact_id"]))
    return {
        "order_id": order["id"],
        "order_key": order["business_key"],
        "amount": _value(order_facts, "amount") or 0,
        "customer_key": str(customer_key or ""),
        "customer_name": _value(customer_facts, "company_name"),
        "lines": lines,
        "suppliers": suppliers,
        "evidence": evidence_sorted,
        "evidence_hash": digest(evidence_sorted),
    }


def run_order_agent(conn: psycopg.Connection, order_id: str, model: ModelProvider) -> dict[str, Any]:
    context = build_context(conn, order_id)
    prompt = (
        "Analizza il contesto e proponi solo azioni tipizzate. "
        "Non eseguire nulla.\nCONTEXT_JSON="
        + json.dumps(context, ensure_ascii=False, default=str)
    )
    raw = model.complete_structured(prompt, RECOMMENDATION_SCHEMA)
    jsonschema.validate(raw, RECOMMENDATION_SCHEMA)
    raw["evidence_hash"] = context["evidence_hash"]
    raw["order_id"] = context["order_id"]
    raw["order_key"] = context["order_key"]
    raw["amount"] = context["amount"]
    return raw


def _fact_rows(conn: psycopg.Connection, entity_id: str | None) -> list[dict]:
    if not entity_id:
        return []
    return list(
        conn.execute(
            """
            SELECT id::text AS fact_id, property, value, origin, classification
            FROM app.facts
            WHERE entity_id = %s AND valid_to IS NULL
            """,
            (entity_id,),
        ).fetchall()
    )


def _value(facts: list[dict], name: str) -> Any:
    for fact in facts:
        if fact["property"] == name:
            return stable(fact["value"])
    return None


def _collect(evidence: list[dict], entity_type: str, entity_key: str, facts: list[dict], names: tuple[str, ...]) -> None:
    for fact in facts:
        if fact["property"] not in names:
            continue
        evidence.append(
            {
                "fact_id": fact["fact_id"],
                "entity_type": entity_type,
                "entity_key": entity_key,
                "property": fact["property"],
                "value": stable(fact["value"]),
                "origin": fact["origin"],
                "classification": fact["classification"],
            }
        )


def _alias(conn: psycopg.Connection, entity_type: str, alias: str) -> str | None:
    if not alias:
        return None
    row = conn.execute(
        """
        SELECT entity_id::text FROM app.identity_aliases
        WHERE entity_type = %s AND alias = %s
        """,
        (entity_type, alias),
    ).fetchone()
    return row["entity_id"] if row else None


def _stock_for_product(conn: psycopg.Connection, product_key: str) -> str | None:
    row = conn.execute(
        """
        SELECT e.id::text AS entity_id
        FROM app.entities e
        JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL
        WHERE e.type = 'StockPosition' AND e.valid_to IS NULL
          AND f.property = 'product_source_key' AND f.value = %s::jsonb
        ORDER BY e.business_key
        LIMIT 1
        """,
        (json.dumps(product_key),),
    ).fetchone()
    return row["entity_id"] if row else None


def _suppliers_for_product(conn: psycopg.Connection, product_key: str) -> list[dict]:
    return list(
        conn.execute(
            """
            SELECT e.id::text, e.business_key
            FROM app.entities e
            JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL
            WHERE e.type = 'Supplier' AND e.valid_to IS NULL
              AND f.property = 'product_source_key' AND f.value = %s::jsonb
            ORDER BY e.business_key
            """,
            (json.dumps(product_key),),
        ).fetchall()
    )
