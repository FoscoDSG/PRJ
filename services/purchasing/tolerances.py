"""Tolleranze acquisti. Una modifica è una riga nuova, per azienda.

Un'azienda senza righe legge i predefiniti e non ne riceve una scritta.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg

from services.errors import AppError

PRICE_RATE = Decimal("0.01")
PRICE_ABS = Decimal("5")
VAT_ABS = Decimal("0.01")
TOTAL_ABS = Decimal("0.05")
PARTIAL_RECEIPT = True

_FIELDS = ("price_rate", "price_abs", "vat_abs", "total_abs", "partial_receipt")


def defaults() -> dict[str, Any]:
    return {
        "version": None,
        "price_rate": PRICE_RATE,
        "price_abs": PRICE_ABS,
        "vat_abs": VAT_ABS,
        "total_abs": TOTAL_ABS,
        "partial_receipt": PARTIAL_RECEIPT,
        "stored": False,
    }


def current_tolerances(conn: psycopg.Connection, company_id: str) -> dict[str, Any]:
    if not company_id:
        raise AppError(422, "Tolleranze senza azienda")
    row = conn.execute(
        """
        SELECT version, price_rate, price_abs, vat_abs, total_abs, partial_receipt
        FROM app.p2p_tolerances
        WHERE company_id = %s::uuid
        ORDER BY version DESC
        LIMIT 1
        """,
        (company_id,),
    ).fetchone()
    if row is None:
        found = defaults()
        found["company_id"] = company_id
        return found
    return {
        "company_id": company_id,
        "version": int(row["version"]),
        "price_rate": Decimal(row["price_rate"]),
        "price_abs": Decimal(row["price_abs"]),
        "vat_abs": Decimal(row["vat_abs"]),
        "total_abs": Decimal(row["total_abs"]),
        "partial_receipt": bool(row["partial_receipt"]),
        "stored": True,
    }


def record_tolerances(
    conn: psycopg.Connection,
    company_id: str,
    **values: Any,
) -> dict[str, Any]:
    """Append the next version. The previous row stays as it was."""
    unknown = set(values) - set(_FIELDS)
    if unknown:
        raise AppError(422, "Tolleranza sconosciuta")
    current = current_tolerances(conn, company_id)
    version = 1 if current["version"] is None else current["version"] + 1
    stored = {name: values.get(name, current[name]) for name in _FIELDS}
    conn.execute(
        """
        INSERT INTO app.p2p_tolerances (
          id, company_id, version, price_rate, price_abs, vat_abs, total_abs, partial_receipt
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid4()),
            company_id,
            version,
            stored["price_rate"],
            stored["price_abs"],
            stored["vat_abs"],
            stored["total_abs"],
            stored["partial_receipt"],
        ),
    )
    return current_tolerances(conn, company_id)
