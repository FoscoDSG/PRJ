"""Read an incoming bank-statement CSV.

Only a file that declares ``label_origin: synthetic`` is accepted.
IBAN and the description (causale) are personal data and are stored
through the same masking as the other personal fields.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any

from services.errors import AppError
from services.ingest.fatturapa import SYNTHETIC_MARK, require_synthetic


def parse_bank_csv(text: str) -> list[dict[str, Any]]:
    require_synthetic(text)
    readable = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    reader = csv.DictReader(io.StringIO(readable))
    if not reader.fieldnames:
        raise AppError(422, "Estratto senza intestazione")
    payments = []
    for index, row in enumerate(reader, start=1):
        normalized = {_key(key): (value or "").strip() for key, value in row.items() if key}
        paid_at = normalized.get("data") or normalized.get("paid_at") or ""
        amount_text = normalized.get("importo") or normalized.get("amount") or ""
        if paid_at == "" or amount_text == "":
            continue
        amount = Decimal(amount_text.replace(",", "."))
        reference = normalized.get("riferimento") or normalized.get("reference") or ""
        causale = normalized.get("causale") or ""
        direction = (normalized.get("direzione") or "incoming").lower()
        if direction not in ("incoming", "outgoing"):
            raise AppError(422, "Direzione pagamento non ammessa")
        source_key = normalized.get("incasso") or f"{paid_at}|{amount}|{reference}|{index}"
        payments.append(
            {
                "source_key": source_key,
                "direction": direction,
                "paid_at": paid_at,
                "amount": amount,
                "iban": normalized.get("iban") or "",
                "causale": causale,
                "counterparty_name": normalized.get("cliente") or normalized.get("nome") or "",
                "counterparty_vat": normalized.get("partita_iva") or normalized.get("piva") or "",
                "reference": reference,
            }
        )
    if not payments:
        raise AppError(422, "Estratto senza incassi")
    if SYNTHETIC_MARK not in text:
        raise AppError(422, "Il file non dichiara label_origin synthetic")
    return payments


def _key(name: str) -> str:
    return name.strip().lower().replace(" ", "_")
