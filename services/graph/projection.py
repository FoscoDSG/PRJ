"""Project current facts into places, contains, and suppliedBy edges."""

from __future__ import annotations

import uuid

import psycopg

from services.text import stable


def project_graph(conn: psycopg.Connection) -> int:
    conn.execute(
        """
        UPDATE app.graph_edges
        SET valid_to = now()
        WHERE valid_to IS NULL AND rel <> 'triggered'
        """
    )
    created = 0
    orders = _rows(
        conn,
        """
        SELECT id::text, business_key, company_id::text AS company_id
        FROM app.entities WHERE type = 'Order' AND valid_to IS NULL
        """,
    )
    for order in orders:
        customer_key = _fact(conn, order["id"], "customer_source_key")
        customer_id = _alias(conn, "Customer", str(customer_key), order["company_id"]) if customer_key else None
        if customer_id:
            created += _edge(conn, customer_id, "places", order["id"])
    lines = _rows(
        conn,
        """
        SELECT id::text, company_id::text AS company_id
        FROM app.entities WHERE type = 'OrderLine' AND valid_to IS NULL
        """,
    )
    for line in lines:
        order_key = _fact(conn, line["id"], "order_source_key")
        product_key = _fact(conn, line["id"], "product_source_key")
        order_id = _alias(conn, "Order", str(order_key), line["company_id"]) if order_key else None
        product_id = _alias(conn, "Product", str(product_key), line["company_id"]) if product_key else None
        if order_id and product_id:
            created += _edge(conn, order_id, "contains", product_id)
    suppliers = _rows(
        conn,
        """
        SELECT id::text, company_id::text AS company_id
        FROM app.entities WHERE type = 'Supplier' AND valid_to IS NULL
        """,
    )
    for supplier in suppliers:
        product_key = _fact(conn, supplier["id"], "product_source_key")
        product_id = _alias(conn, "Product", str(product_key), supplier["company_id"]) if product_key else None
        if product_id:
            created += _edge(conn, product_id, "suppliedBy", supplier["id"])
    stocks = _rows(
        conn,
        """
        SELECT id::text, company_id::text AS company_id
        FROM app.entities WHERE type = 'StockPosition' AND valid_to IS NULL
        """,
    )
    for stock in stocks:
        warehouse_key = _fact(conn, stock["id"], "warehouse_source_key")
        warehouse_id = _alias(conn, "Warehouse", str(warehouse_key), stock["company_id"]) if warehouse_key else None
        if warehouse_id:
            created += _edge(conn, stock["id"], "storedAt", warehouse_id)
    return created


def record_trigger(conn: psycopg.Connection, order_id: str, event_id: str) -> None:
    _edge(conn, order_id, "triggered", event_id)


def _edge(conn: psycopg.Connection, from_id: str, rel: str, to_id: str) -> int:
    exists = conn.execute(
        """
        SELECT 1 FROM app.graph_edges
        WHERE from_id = %s AND rel = %s AND to_id = %s AND valid_to IS NULL
        """,
        (from_id, rel, to_id),
    ).fetchone()
    if exists:
        return 0
    conn.execute(
        """
        INSERT INTO app.graph_edges (id, from_id, rel, to_id, origin)
        VALUES (%s, %s, %s, %s, 'transformed')
        """,
        (str(uuid.uuid4()), from_id, rel, to_id),
    )
    return 1


def _fact(conn: psycopg.Connection, entity_id: str, name: str):
    row = conn.execute(
        """
        SELECT value FROM app.facts
        WHERE entity_id = %s AND property = %s AND valid_to IS NULL
        """,
        (entity_id, name),
    ).fetchone()
    if row is None:
        return None
    return stable(row["value"])


def _alias(conn: psycopg.Connection, entity_type: str, alias: str, company_id: str | None) -> str | None:
    if not alias:
        return None
    row = conn.execute(
        """
        SELECT entity_id::text FROM app.identity_aliases
        WHERE entity_type = %s AND alias = %s
          AND company_id IS NOT DISTINCT FROM %s::uuid
        """,
        (entity_type, alias, company_id),
    ).fetchone()
    return row["entity_id"] if row else None


def _rows(conn: psycopg.Connection, statement: str) -> list[dict]:
    return list(conn.execute(statement).fetchall())
