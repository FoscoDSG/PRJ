"""Read a FatturaPA invoice: header, lines, due dates, credit notes (TD04).

Only a file that declares ``label_origin: synthetic`` is accepted.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import Decimal
from typing import Any

from services.errors import AppError

SYNTHETIC_MARK = "label_origin: synthetic"


def require_synthetic(text: str) -> None:
    if SYNTHETIC_MARK not in text:
        raise AppError(422, "Il file non dichiara label_origin synthetic")


def parse_fatturapa(text: str) -> dict[str, list[dict[str, Any]]]:
    require_synthetic(text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise AppError(422, "XML non leggibile") from exc
    header = _first(root, "FatturaElettronicaHeader")
    if header is None:
        raise AppError(422, "Testata fattura assente")
    customer_name, customer_vat = _party(header, "CessionarioCommittente")
    invoices = []
    credit_notes = []
    for body in _all(root, "FatturaElettronicaBody"):
        general = _first(body, "DatiGeneraliDocumento")
        if general is None:
            continue
        document_type = _text(_first(general, "TipoDocumento")) or "TD01"
        number = _text(_first(general, "Numero"))
        issued_at = _text(_first(general, "Data"))
        amount = _decimal(_text(_first(general, "ImportoTotaleDocumento")))
        currency = _text(_first(general, "Divisa")) or "EUR"
        if not number or amount is None or not issued_at:
            raise AppError(422, "Testata fattura incompleta")
        order_block = _first(body, "DatiOrdineAcquisto")
        order_number = _text(_first(order_block, "IdDocumento")) if order_block is not None else ""
        related_block = _first(body, "DatiFattureCollegate")
        related = _text(_first(related_block, "IdDocumento")) if related_block is not None else ""
        due_dates = [value for value in (_text(node) for node in _all(body, "DataScadenzaPagamento")) if value]
        lines = []
        for line in _all(body, "DettaglioLinee"):
            line_amount = _decimal(_text(_first(line, "PrezzoTotale")))
            if line_amount is None:
                continue
            lines.append(
                {
                    "number": _text(_first(line, "NumeroLinea")) or str(len(lines) + 1),
                    "description": _text(_first(line, "Descrizione")) or "",
                    "quantity": _decimal(_text(_first(line, "Quantita"))),
                    "unit_price": _decimal(_text(_first(line, "PrezzoUnitario"))),
                    "line_amount": line_amount,
                    "vat_rate": _decimal(_text(_first(line, "AliquotaIVA"))),
                }
            )
        if document_type == "TD04":
            credit_notes.append(
                {
                    "number": number,
                    "issued_at": issued_at,
                    "amount": amount,
                    "invoice_number": related or None,
                    "customer_vat": customer_vat,
                    "reason": _text(_first(general, "Causale")) or "Nota di credito di prova",
                }
            )
            continue
        invoices.append(
            {
                "number": number,
                "document_type": document_type,
                "issued_at": issued_at,
                "amount": amount,
                "currency": currency,
                "customer_name": customer_name or "Cliente di prova",
                "customer_vat": customer_vat,
                "order_number": order_number or None,
                "due_dates": due_dates,
                "lines": lines,
            }
        )
    if not invoices and not credit_notes:
        raise AppError(422, "Nessun documento nel file")
    return {"invoices": invoices, "credit_notes": credit_notes}


def _party(header, name: str) -> tuple[str, str]:
    node = _first(header, name)
    if node is None:
        return "", ""
    denomination = _text(_first(node, "Denominazione"))
    country = _text(_first(node, "IdPaese"))
    code = _text(_first(node, "IdCodice"))
    vat = f"{country}{code}" if code else ""
    return denomination, vat


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _all(node, name: str) -> list[ET.Element]:
    return [item for item in node.iter() if _local(item.tag) == name]


def _first(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    for item in node.iter():
        if _local(item.tag) == name:
            return item
    return None


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _decimal(value: str) -> Decimal | None:
    if value == "":
        return None
    return Decimal(value.replace(",", "."))
