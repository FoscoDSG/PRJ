"""Minimum checks for the five operating objects. Documents stay in quarantine."""

from __future__ import annotations

import re
import uuid
from typing import Any

import psycopg

from services.text import stable

_VAT = re.compile(r"^IT\d{11}$")


def document_reasons(customer: dict[str, Any], lines: list[dict[str, Any]], conn: psycopg.Connection) -> list[str]:
    reasons = []
    vat = str(customer.get("PIVA") or "")
    if not _VAT.fullmatch(vat):
        reasons.append("P.IVA non valida")
    known_prices = _prices(conn)
    for line in lines:
        quantity = line.get("QTY")
        sku = str(line.get("COD_ART") or "")
        price = line.get("PREZZO")
        if not sku:
            reasons.append("Prodotto senza codice")
        if quantity is None or quantity <= 0:
            reasons.append(f"Quantità non valida per {sku or 'riga'}")
        expected = known_prices.get(sku)
        if expected is not None and price != expected:
            reasons.append(f"Prezzo di {sku} diverso dal listino ({expected})")
    return reasons


def check_canonical(conn: psycopg.Connection) -> int:
    created = 0
    prices = _prices(conn)
    warehouses = {
        row["business_key"]
        for row in conn.execute(
            "SELECT business_key FROM app.entities WHERE type = 'Warehouse' AND valid_to IS NULL"
        ).fetchall()
    }
    for row in conn.execute(
        """
        SELECT e.id::text AS entity_id, e.type, e.business_key, f.property, f.value
        FROM app.entities e
        JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL
        WHERE e.valid_to IS NULL
          AND e.type IN ('Customer', 'Order', 'Product', 'Warehouse', 'Supplier', 'OrderLine', 'StockPosition')
        """
    ).fetchall():
        value = stable(row["value"])
        issue = None
        if row["type"] == "Customer" and row["property"] == "vat":
            valid = conn.execute(
                "SELECT app.vat_is_valid(%s::uuid) AS ok",
                (row["entity_id"],),
            ).fetchone()
            if valid is None or not valid["ok"]:
                issue = ("vat", "P.IVA non valida")
        elif row["type"] == "OrderLine" and row["property"] == "quantity" and (value is None or value <= 0):
            issue = ("quantity", "Quantità non positiva")
        elif row["type"] == "OrderLine" and row["property"] == "unit_price":
            product = _line_product(conn, row["business_key"])
            expected = prices.get(product or "")
            if expected is not None and value != expected:
                issue = ("price", f"Prezzo {value} diverso dal listino {expected}")
        elif row["type"] == "StockPosition" and row["property"] == "warehouse_source_key":
            if str(value) not in warehouses:
                issue = ("warehouse", f"Magazzino sconosciuto: {value}")
        elif row["type"] in ("Product", "Supplier", "Warehouse") and row["property"] == "source_key" and not value:
            issue = ("source_key", "Codice assente")
        if issue is None:
            continue
        created += _issue(conn, row["type"], row["business_key"], issue[0], issue[1])
    return created


def _prices(conn: psycopg.Connection) -> dict[str, int | float]:
    rows = conn.execute("SELECT product_source_key, price FROM app.price_list").fetchall()
    prices = {}
    for row in rows:
        number = stable(row["price"])
        prices[row["product_source_key"]] = int(number) if float(number).is_integer() else float(number)
    return prices


def _line_product(conn: psycopg.Connection, business_key: str) -> str | None:
    if "|" not in business_key:
        return None
    return business_key.split("|", 1)[1]


def _issue(conn: psycopg.Connection, entity_type: str, business_key: str, check_name: str, detail: str) -> int:
    existing = conn.execute(
        """
        SELECT 1 FROM app.quality_issues
        WHERE entity_type = %s AND business_key = %s AND check_name = %s AND status = 'open'
        """,
        (entity_type, business_key, check_name),
    ).fetchone()
    if existing:
        return 0
    conn.execute(
        """
        INSERT INTO app.quality_issues (id, entity_type, business_key, check_name, detail)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (str(uuid.uuid4()), entity_type, business_key, check_name, detail),
    )
    return 1
