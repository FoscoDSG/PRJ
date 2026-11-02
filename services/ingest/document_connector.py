"""Turn an order email or a text PDF into customer, header, and line rows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from services.ingest.base import rows_from_dicts
from services.text import digest

_LABEL = r"(?:cliente|ragione\s+sociale|p\.?\s*iva|partita\s+iva|vat|codice\s+cliente|da|e-?mail|ordine|numero\s+ordine|data|importo|totale|righe)\s*:"
_COMPANY = re.compile(rf"(?:cliente|ragione\s+sociale)\s*:\s*(.+?)(?=\s*{_LABEL}|$)", re.IGNORECASE)
_VAT = re.compile(rf"(?:p\.?\s*iva|partita\s+iva|vat)\s*:\s*([A-Za-z0-9]+)", re.IGNORECASE)
_STOP = (
    r"(?=\s*(?:cliente|ragione\s+sociale|p\.?\s*iva|partita\s+iva|vat|codice\s+cliente|"
    r"da|e-?mail|ordine|numero\s+ordine|data|importo|totale|righe)\s*:|$)"
)
_CODE = re.compile(rf"(?:codice\s+cliente)\s*:\s*([A-Za-z0-9_\-]+?){_STOP}", re.IGNORECASE)
_EMAIL = re.compile(r"(?:da|e-?mail)\s*:\s*(\S+@\S+)", re.IGNORECASE)
_ORDER = re.compile(rf"(?:ordine|numero\s+ordine)\s*:\s*([A-Za-z0-9_\-]+?){_STOP}", re.IGNORECASE)
_DATE = re.compile(r"(?:data)\s*:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_AMOUNT = re.compile(r"(?:importo|totale)\s*:\s*([0-9]+(?:[.,][0-9]+)?)", re.IGNORECASE)
_LINE = re.compile(r"\b([A-Z][A-Z0-9\-]*)\s+([0-9]+(?:[.,][0-9]+)?)\s+([0-9]+(?:[.,][0-9]+)?)\b")


def read_order_document(path: Path | str) -> dict[str, Any]:
    file_path = Path(path)
    kind = "pdf" if file_path.suffix.lower() == ".pdf" else "email"
    text = _text(file_path, kind)
    parsed = parse_order_text(text)
    stem = _safe_stem(file_path.stem)
    return {
        "order_key": parsed["order_key"],
        "source_kind": kind,
        "customer": parsed["customer"][0],
        "lines": parsed["lines"],
        "stem": stem,
        "sources": [
            (f"{stem}_cliente", _pack(parsed["customer"], "email" if kind == "email" else "pdf")),
            (f"{stem}_ordine", _pack(parsed["order"], "email" if kind == "email" else "pdf")),
            (f"{stem}_righe", _pack(parsed["lines"], "email" if kind == "email" else "pdf")),
        ],
    }


def parse_order_text(text: str) -> dict[str, Any]:
    company = _first(_COMPANY, text)
    vat = _first(_VAT, text)
    code = _first(_CODE, text) or vat
    email = _first(_EMAIL, text)
    order_key = _first(_ORDER, text)
    ordered_at = _first(_DATE, text)
    lines = []
    for match in _LINE.finditer(text):
        sku, quantity, price = match.groups()
        lines.append(
            {
                "ORD_HEAD": order_key,
                "COD_ART": sku.upper(),
                "QTY": _number(quantity),
                "PREZZO": _number(price),
            }
        )
    if not order_key or not code or not lines:
        raise ValueError("Il documento non contiene cliente, numero ordine e almeno una riga")
    amount = _first(_AMOUNT, text)
    if amount is None:
        amount = sum(line["QTY"] * line["PREZZO"] for line in lines)
    else:
        amount = _number(amount)
    for line in lines:
        line["ORD_HEAD"] = order_key
    return {
        "order_key": order_key,
        "customer": [
            {
                "ANAG_CLI": code,
                "RAGIONE_SOCIALE": company or code,
                "PIVA": vat,
                "EMAIL": email,
            }
        ],
        "order": [
            {
                "ORD_HEAD": order_key,
                "ANAG_CLI": code,
                "IMPORTO": amount,
                "STATO": "open",
                "DATA": ordered_at,
            }
        ],
        "lines": lines,
    }


def _text(path: Path, kind: str) -> str:
    if kind == "pdf":
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    else:
        text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("Documento senza testo")
    return text


def _pack(records: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    columns, rows = rows_from_dicts(records)
    return {
        "columns": columns,
        "rows": rows,
        "watermark": digest({"kind": kind, "rows": rows}),
        "source_kind": kind,
    }


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    value = match.group(1).strip().strip(",")
    return value or None


def _number(value: str | int | float) -> int | float:
    if isinstance(value, bool):
        raise ValueError("Quantità non valida")
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    number = float(str(value).replace(",", "."))
    return int(number) if number.is_integer() else number


def _safe_stem(stem: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_")
    return cleaned or "documento"
