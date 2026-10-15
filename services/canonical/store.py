"""Idempotent canonical upsert. Facts keep provenance and validity intervals."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Json

from ontology.loader import Ontology
from services.graph.projection import project_graph
from services.text import coerce_number, stable

PROFILE_ORDER = {
    "customer": 10,
    "product": 20,
    "supplier": 30,
    "order": 40,
    "order_line": 50,
    "stock": 60,
    "unknown": 90,
}


def materialize(conn: psycopg.Connection, ontology: Ontology) -> dict[str, int]:
    sources = conn.execute(
        """
        SELECT DISTINCT source_name FROM app.field_mappings
        WHERE valid_to IS NULL AND status = 'confirmed'
        """
    ).fetchall()
    counts = {"entities": 0, "facts": 0}
    ordered = sorted(
        (row["source_name"] for row in sources),
        key=lambda name: PROFILE_ORDER.get(_profile(conn, name), 100),
    )
    for source_name in ordered:
        written = _materialize_source(conn, ontology, source_name)
        counts["entities"] += written["entities"]
        counts["facts"] += written["facts"]
    counts["calculated"] = _calculate(conn, ontology)
    from services.identity.resolve import queue_identity_doubts
    from services.quality import check_canonical

    counts["identity_reviews"] = queue_identity_doubts(conn)
    counts["quality_issues"] = check_canonical(conn)
    project_graph(conn)
    return counts


def _profile(conn: psycopg.Connection, source_name: str) -> str:
    row = conn.execute(
        """
        SELECT profile FROM app.field_mappings
        WHERE source_name = %s AND valid_to IS NULL
        ORDER BY valid_from DESC
        LIMIT 1
        """,
        (source_name,),
    ).fetchone()
    return row["profile"] if row else "unknown"


def _materialize_source(conn: psycopg.Connection, ontology: Ontology, source_name: str) -> dict[str, int]:
    batch = conn.execute(
        """
        SELECT id FROM app.landing_batches
        WHERE source_name = %s
        ORDER BY loaded_at DESC
        LIMIT 1
        """,
        (source_name,),
    ).fetchone()
    if batch is None:
        return {"entities": 0, "facts": 0}
    mappings = conn.execute(
        """
        SELECT source_field, target_type, target_property, confidence, profile
        FROM app.field_mappings
        WHERE source_name = %s AND status = 'confirmed' AND valid_to IS NULL
        """,
        (source_name,),
    ).fetchall()
    rows = conn.execute(
        """
        SELECT row_index, data FROM app.landing_rows
        WHERE batch_id = %s
        ORDER BY row_index
        """,
        (batch["id"],),
    ).fetchall()
    profile = mappings[0]["profile"] if mappings else "unknown"
    from services.mapping.engine import rule_id_for

    entities = 0
    facts = 0
    for row in rows:
        data = row["data"]
        values: dict[str, Any] = {}
        confidence: dict[str, float] = {}
        confidence_rules: dict[str, str] = {}
        target_type = None
        for mapping in mappings:
            raw = data.get(mapping["source_field"])
            if raw in (None, ""):
                continue
            prop = ontology.prop(mapping["target_type"], mapping["target_property"])
            if prop is None:
                continue
            target_type = mapping["target_type"]
            cast = coerce_number(raw) if prop.type == "number" else str(raw).strip()
            if cast in (None, ""):
                continue
            values[mapping["target_property"]] = cast
            confidence[mapping["target_property"]] = float(mapping["confidence"])
            confidence_rules[mapping["target_property"]] = rule_id_for(
                mapping["profile"], mapping["source_field"]
            )
        if not values:
            continue
        if profile == "unknown":
            title = values.get("title")
            if title:
                facts += _write_entity(
                    conn,
                    ontology,
                    "Document",
                    str(title),
                    {"title": title},
                    source_name,
                    confidence,
                    confidence_rules,
                )
                entities += 1
            continue
        written = _write_profile(
            conn, ontology, profile, values, source_name, confidence, confidence_rules
        )
        entities += written[0]
        facts += written[1]
    return {"entities": entities, "facts": facts}


def _write_profile(
    conn: psycopg.Connection,
    ontology: Ontology,
    profile: str,
    values: dict[str, Any],
    source: str,
    confidence: dict[str, float],
    confidence_rules: dict[str, str] | None = None,
) -> tuple[int, int]:
    if profile == "customer":
        source_key = _text(values.get("source_key"))
        vat = _vat(values.get("vat"))
        business_key = _identity_key(vat) or source_key
        if not business_key:
            return 0, 0
        entity_id = _ensure_entity(conn, "Customer", business_key)
        _alias(conn, "Customer", source_key, entity_id)
        payload = dict(values)
        if vat:
            payload["vat"] = vat
        if source_key:
            payload["source_key"] = source_key
        if "source_key" in payload and _has_fact(conn, entity_id, "source_key"):
            payload.pop("source_key")
        return 1, _write_facts(
            conn, ontology, entity_id, "Customer", payload, source, confidence, confidence_rules=confidence_rules
        )
    if profile == "product":
        source_key = _text(values.get("source_key"))
        if not source_key:
            return 0, 0
        entity_id = _ensure_entity(conn, "Product", source_key)
        _alias(conn, "Product", source_key, entity_id)
        return 1, _write_facts(
            conn, ontology, entity_id, "Product", values, source, confidence, confidence_rules=confidence_rules
        )
    if profile == "supplier":
        source_key = _text(values.get("source_key"))
        if not source_key:
            return 0, 0
        entity_id = _ensure_entity(conn, "Supplier", source_key)
        _alias(conn, "Supplier", source_key, entity_id)
        product_key = _text(values.get("product_source_key"))
        if product_key:
            _ensure_shell(conn, ontology, "Product", product_key, source)
        return 1, _write_facts(
            conn, ontology, entity_id, "Supplier", values, source, confidence, confidence_rules=confidence_rules
        )
    if profile == "order":
        source_key = _text(values.get("source_key"))
        if not source_key:
            return 0, 0
        entity_id = _ensure_entity(conn, "Order", source_key)
        _alias(conn, "Order", source_key, entity_id)
        return 1, _write_facts(
            conn, ontology, entity_id, "Order", values, source, confidence, confidence_rules=confidence_rules
        )
    if profile == "order_line":
        order_key = _text(values.get("order_source_key"))
        product_key = _text(values.get("product_source_key"))
        if not order_key or not product_key:
            return 0, 0
        business_key = f"{order_key}|{product_key}"
        values["source_key"] = business_key
        entity_id = _ensure_entity(conn, "OrderLine", business_key)
        _alias(conn, "OrderLine", business_key, entity_id)
        _ensure_shell(conn, ontology, "Order", order_key, source)
        _ensure_shell(conn, ontology, "Product", product_key, source)
        return 1, _write_facts(
            conn, ontology, entity_id, "OrderLine", values, source, confidence, confidence_rules=confidence_rules
        )
    if profile == "stock":
        product_key = _text(values.get("product_source_key"))
        warehouse_key = _text(values.get("warehouse_source_key")) or "DEFAULT"
        if not product_key:
            return 0, 0
        values["warehouse_source_key"] = warehouse_key
        business_key = f"{product_key}|{warehouse_key}"
        values["source_key"] = business_key
        entity_id = _ensure_entity(conn, "StockPosition", business_key)
        _alias(conn, "StockPosition", business_key, entity_id)
        _ensure_shell(conn, ontology, "Product", product_key, source)
        warehouse_id = _ensure_entity(conn, "Warehouse", warehouse_key)
        _alias(conn, "Warehouse", warehouse_key, warehouse_id)
        _write_facts(
            conn,
            ontology,
            warehouse_id,
            "Warehouse",
            {"source_key": warehouse_key, "name": warehouse_key},
            source,
            {},
        )
        return 1, _write_facts(
            conn,
            ontology,
            entity_id,
            "StockPosition",
            values,
            source,
            confidence,
            confidence_rules=confidence_rules,
        )
    return 0, 0


def _calculate(conn: psycopg.Connection, ontology: Ontology) -> int:
    written = 0
    stocks = _entities_of_type(conn, "StockPosition")
    available_by_product: dict[str, int | float] = {}
    for stock in stocks:
        facts = _open_map(conn, stock["id"])
        on_hand = _num(facts.get("on_hand"))
        reserved = _num(facts.get("reserved"))
        if on_hand is None:
            continue
        available = on_hand - (reserved or 0)
        written += _write_facts(
            conn,
            ontology,
            stock["id"],
            "StockPosition",
            {"available": available},
            "on_hand - reserved",
            {},
            origin="calculated",
        )
        product_key = facts.get("product_source_key")
        if product_key:
            available_by_product[str(product_key)] = available_by_product.get(str(product_key), 0) + available
    lines = _entities_of_type(conn, "OrderLine")
    shortage_by_order: dict[str, int | float] = {}
    revenue_by_order: dict[str, float] = {}
    cost_by_order: dict[str, float] = {}
    priced_orders: set[str] = set()
    remaining = dict(available_by_product)
    for line in lines:
        facts = _open_map(conn, line["id"])
        quantity = _num(facts.get("quantity"))
        product_key = facts.get("product_source_key")
        order_key = facts.get("order_source_key")
        if quantity is None or not product_key or not order_key:
            continue
        available = remaining.get(str(product_key), 0)
        shortage = max(0, quantity - available)
        remaining[str(product_key)] = max(0, available - quantity)
        written += _write_facts(
            conn,
            ontology,
            line["id"],
            "OrderLine",
            {"shortage": shortage},
            "quantity - available",
            {},
            origin="calculated",
        )
        order_id = str(order_key)
        shortage_by_order[order_id] = shortage_by_order.get(order_id, 0) + shortage
        price = _num(facts.get("unit_price"))
        product = _resolve(conn, "Product", str(product_key))
        cost = None
        if product:
            cost = _num(_open_map(conn, product).get("cost"))
        if price is not None and cost is not None:
            revenue_by_order[order_id] = revenue_by_order.get(order_id, 0) + quantity * price
            cost_by_order[order_id] = cost_by_order.get(order_id, 0) + quantity * cost
            priced_orders.add(order_id)
    for order in _entities_of_type(conn, "Order"):
        order_key = order["business_key"]
        payload: dict[str, Any] = {"shortage_qty": shortage_by_order.get(order_key, 0)}
        if order_key in priced_orders and revenue_by_order.get(order_key):
            revenue = revenue_by_order[order_key]
            payload["margin"] = round((revenue - cost_by_order[order_key]) / revenue, 4)
        written += _write_facts(
            conn,
            ontology,
            order["id"],
            "Order",
            payload,
            "calculated",
            {},
            origin="calculated",
        )
    return written


def _write_entity(
    conn: psycopg.Connection,
    ontology: Ontology,
    type_name: str,
    business_key: str,
    values: dict[str, Any],
    source: str,
    confidence: dict[str, float],
    confidence_rules: dict[str, str] | None = None,
) -> int:
    entity_id = _ensure_entity(conn, type_name, business_key)
    _alias(conn, type_name, business_key, entity_id)
    return _write_facts(
        conn, ontology, entity_id, type_name, values, source, confidence, confidence_rules=confidence_rules
    )


def _ensure_shell(
    conn: psycopg.Connection, ontology: Ontology, type_name: str, business_key: str, source: str
) -> str:
    entity_id = _ensure_entity(conn, type_name, business_key)
    _alias(conn, type_name, business_key, entity_id)
    if not _has_fact(conn, entity_id, "source_key"):
        _write_facts(
            conn,
            ontology,
            entity_id,
            type_name,
            {"source_key": business_key},
            source,
            {},
            origin="transformed",
        )
    return entity_id


def _ensure_entity(conn: psycopg.Connection, type_name: str, business_key: str) -> str:
    found = conn.execute(
        """
        SELECT id FROM app.entities
        WHERE type = %s AND business_key = %s AND valid_to IS NULL
        """,
        (type_name, business_key),
    ).fetchone()
    if found:
        return str(found["id"])
    entity_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO app.entities (id, type, business_key) VALUES (%s, %s, %s)",
        (entity_id, type_name, business_key),
    )
    return entity_id


def _alias(conn: psycopg.Connection, entity_type: str, alias: str | None, entity_id: str) -> None:
    if not alias:
        return
    conn.execute(
        """
        INSERT INTO app.identity_aliases (entity_type, alias, entity_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (entity_type, alias) DO UPDATE SET entity_id = EXCLUDED.entity_id
        """,
        (entity_type, alias, entity_id),
    )


def _write_facts(
    conn: psycopg.Connection,
    ontology: Ontology,
    entity_id: str,
    type_name: str,
    values: dict[str, Any],
    source: str,
    confidence: dict[str, float],
    origin: str | None = None,
    confidence_rules: dict[str, str] | None = None,
) -> int:
    written = 0
    for name, value in values.items():
        prop = ontology.prop(type_name, name)
        if prop is None or value is None:
            continue
        fact_origin = origin or prop.origin
        raw_confidence = confidence.get(name) if confidence else None
        if raw_confidence is None:
            fact_confidence = None
            method = "unknown"
            rule = None
        else:
            fact_confidence = float(raw_confidence)
            method = "rule"
            rule = (confidence_rules or {}).get(name)
        if _put_fact(
            conn,
            entity_id=entity_id,
            name=name,
            value=value,
            classification=prop.classification,
            origin=fact_origin,
            source=source,
            confidence=fact_confidence,
            source_class=_source_class(conn, source, fact_origin),
            confidence_method=method,
            confidence_rule=rule,
        ):
            written += 1
    return written


def _put_fact(
    conn: psycopg.Connection,
    *,
    entity_id: str,
    name: str,
    value: Any,
    classification: str,
    origin: str,
    source: str | None,
    confidence: float | None,
    source_class: str | None = None,
    confidence_method: str = "unknown",
    confidence_rule: str | None = None,
) -> bool:
    if origin == "inferred" and source_class not in (None, "ai"):
        raise ValueError("origin inferred è riservata a un modello")
    fact_class = source_class or _source_class(conn, source or "", origin)
    if origin == "inferred":
        fact_class = "ai"
    if confidence_method not in ("rule", "human", "unknown"):
        raise ValueError(f"confidence_method non ammesso: {confidence_method}")
    method = confidence_method
    rule = confidence_rule if method == "rule" and confidence is not None else None
    if method == "rule" and confidence is None:
        method = "unknown"
    current = conn.execute(
        """
        SELECT id, value, source_class, confidence_method FROM app.facts
        WHERE entity_id = %s AND property = %s AND valid_to IS NULL
        """,
        (entity_id, name),
    ).fetchone()
    if current is not None and stable(current["value"]) == stable(value):
        return False
    if current is not None and origin != "calculated":
        # A human correction stays until another human correction replaces it.
        # Automatic ingest does not overwrite it, even from a higher source class.
        if current["confidence_method"] == "human" and confidence_method != "human":
            return False
        if confidence_method != "human":
            from services.db import SOURCE_RANK

            current_rank = SOURCE_RANK.get(current["source_class"] or "unknown", 0)
            new_rank = SOURCE_RANK.get(fact_class, 0)
            if new_rank < current_rank:
                return False
    if current is not None:
        conn.execute("UPDATE app.facts SET valid_to = now() WHERE id = %s", (current["id"],))
    conn.execute(
        """
        INSERT INTO app.facts (
          id, entity_id, property, value, classification, origin, source,
          confidence, source_class, confidence_method, confidence_rule
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid.uuid4()),
            entity_id,
            name,
            Json(stable(value)),
            classification,
            origin,
            source,
            confidence,
            fact_class,
            method,
            rule,
        ),
    )
    return True


def _source_class(conn: psycopg.Connection, source: str, origin: str) -> str:
    """Provenance comes from the registered source, not from its name.

    Calculated and inferred origins keep their own classes. A connector that
    was never registered, or that was registered as unknown, stays unknown.
    """
    if origin == "calculated":
        return "calculated"
    if origin == "inferred":
        return "ai"
    row = conn.execute(
        "SELECT source_class FROM app.sources WHERE name = %s",
        (source,),
    ).fetchone()
    if row is None:
        return "unknown"
    return row["source_class"]


def _has_fact(conn: psycopg.Connection, entity_id: str, name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM app.facts
        WHERE entity_id = %s AND property = %s AND valid_to IS NULL
        """,
        (entity_id, name),
    ).fetchone()
    return row is not None


def _open_map(conn: psycopg.Connection, entity_id: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT property, value FROM app.facts
        WHERE entity_id = %s AND valid_to IS NULL
        """,
        (entity_id,),
    ).fetchall()
    return {row["property"]: stable(row["value"]) for row in rows}


def _entities_of_type(conn: psycopg.Connection, type_name: str) -> list[dict]:
    return list(
        conn.execute(
            """
            SELECT id::text, business_key FROM app.entities
            WHERE type = %s AND valid_to IS NULL
            ORDER BY business_key
            """,
            (type_name,),
        ).fetchall()
    )


def _resolve(conn: psycopg.Connection, entity_type: str, alias: str) -> str | None:
    row = conn.execute(
        """
        SELECT entity_id::text FROM app.identity_aliases
        WHERE entity_type = %s AND alias = %s
        """,
        (entity_type, alias),
    ).fetchone()
    return row["entity_id"] if row else None


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip()


def _identity_key(vat: str | None) -> str | None:
    if not vat:
        return None
    digest = hashlib.sha256(vat.encode()).hexdigest()[:20]
    return f"id:{digest}"


def _vat(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return text.replace(" ", "").upper()


def _num(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        number = coerce_number(value)
    except Exception:
        return None
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        return None
    return number
