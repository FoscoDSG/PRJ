"""Fattura fornitore. La chiave è P.IVA, numero e anno, e non si ripete.

Il tipo è SupplierInvoice. Non entra nel saldo né nella misura degli incassi.
In questa versione si registra solo un file che dichiara label_origin synthetic.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg

from ontology.loader import Ontology
from services.agent.payment_match import money
from services.canonical.store import _ensure_entity, _vat, _write_facts
from services.errors import AppError

_OPEN = "received"


def supplier_invoice_key(vat: str, number: str, year: int) -> str:
    normalized = _vat(vat) or ""
    clean_number = (number or "").strip()
    if normalized == "" or clean_number == "" or not isinstance(year, int) or year < 1000 or year > 9999:
        raise AppError(422, "Fattura fornitore incompleta")
    return f"{normalized}|{clean_number}|{year}"


def invoice_year(issued_at: str) -> int:
    text = (issued_at or "").strip()
    if len(text) < 4 or not text[:4].isdigit():
        raise AppError(422, "Fattura fornitore incompleta")
    return int(text[:4])


def write_supplier_invoice(
    conn: psycopg.Connection,
    ontology: Ontology,
    *,
    company_id: str,
    supplier_vat: str,
    supplier_name: str,
    number: str,
    issued_at: str,
    amount: Decimal,
    currency: str,
    document_type: str,
    label_origin: str,
    source: str,
    source_class: str,
    lines: list[dict[str, Any]] | None = None,
    order_number: str | None = None,
    due_at: str = "",
) -> str:
    if not company_id:
        raise AppError(422, "Fattura fornitore senza azienda")
    if label_origin != "synthetic":
        raise AppError(422, "Il file non dichiara label_origin synthetic")
    year = invoice_year(issued_at)
    key = supplier_invoice_key(supplier_vat, number, year)
    found = conn.execute(
        """
        SELECT id::text
        FROM app.entities
        WHERE type = 'SupplierInvoice' AND business_key = %s AND valid_to IS NULL
          AND company_id = %s::uuid
        """,
        (key, company_id),
    ).fetchone()
    if found is not None:
        raise AppError(409, "Fattura fornitore già presente")
    entity_id = _ensure_entity(conn, "SupplierInvoice", key, company_id)
    payload: dict[str, Any] = {
        "source_key": key,
        "supplier_vat": _vat(supplier_vat) or "",
        "supplier_name": supplier_name,
        "invoice_number": number.strip(),
        "invoice_year": year,
        "document_type": document_type,
        "issued_at": issued_at,
        "amount": money(amount),
        "currency": currency or "EUR",
        "status": _OPEN,
    }
    if due_at:
        payload["due_at"] = due_at
    if order_number:
        payload["order_source_key"] = order_number
    _write_facts(
        conn,
        ontology,
        entity_id,
        "SupplierInvoice",
        payload,
        source,
        {},
        source_classes={name: source_class for name in payload},
    )
    for index, line in enumerate(lines or [], start=1):
        line_key = f"{key}|{line.get('number') or index}"
        line_id = _ensure_entity(conn, "SupplierInvoiceLine", line_key, company_id)
        line_payload = {
            "source_key": line_key,
            "description": line.get("description") or "",
            "product_source_key": line.get("product_source_key") or "",
            "quantity": line.get("quantity"),
            "unit_price": line.get("unit_price"),
            "line_amount": money(line["line_amount"]) if line.get("line_amount") is not None else None,
            "vat_rate": line.get("vat_rate"),
            "supplier_invoice_source_key": key,
        }
        _write_facts(
            conn,
            ontology,
            line_id,
            "SupplierInvoiceLine",
            {name: value for name, value in line_payload.items() if value not in (None, "")},
            source,
            {},
            source_classes={name: source_class for name in line_payload},
        )
    return entity_id
