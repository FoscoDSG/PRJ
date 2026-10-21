"""Personal data: field names, masking for responses and logs, landing staging.

Stored cleartext lives in columns ``ieol_app`` cannot select. Every read of
those columns goes through ``read_cleartext``, which writes an audit row
without the values and then opens ``ieol_pii``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

MASK = "••••"

PII_PROPERTIES = frozenset(
    {
        "company_name",
        "full_name",
        "customer_name",
        "email",
        "vat",
        "phone",
        "telephone",
        "telefono",
        "cellulare",
        "iban",
        "causale",
        "customer_vat",
        "counterparty_name",
        "counterparty_vat",
        "address",
        "indirizzo",
        "codice_fiscale",
        "tax_code",
        "pec",
        "via",
        "cap",
    }
)
PII_NAME_TYPES = frozenset({"Person", "Customer", "Supplier", "Organization"})
PII_KEYS = PII_PROPERTIES | frozenset(
    {
        "e_mail",
        "mail",
        "piva",
        "partita_iva",
        "tel",
        "mobile",
        "cf",
        "citta",
        "city",
        "comune",
        "provincia",
        "ragione_sociale",
        "nome",
        "recipient",
        "referente",
        "intestatario",
        "cognome",
        "causale",
        "customer_vat",
        "counterparty_name",
        "counterparty_vat",
    }
)

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_IBAN = re.compile(r"\bIT[0-9]{2}[A-Z0-9]{23}\b", re.IGNORECASE)
_CF = re.compile(r"\b[A-Z]{6}[0-9]{2}[A-Z][0-9]{2}[A-Z][0-9]{3}[A-Z]\b", re.IGNORECASE)
_VAT = re.compile(r"\bIT[0-9]{11}\b", re.IGNORECASE)
_PHONE = re.compile(r"\+[0-9][0-9 ./\-]{7,}[0-9]")

# Rows still held in this process, so materialize can run before commit.
_STAGED: dict[str, list[dict[str, Any]]] = {}


def is_pii_key(name: str | None) -> bool:
    return bool(name) and name.lower() in PII_KEYS


def is_pii_property(name: str | None, entity_type: str | None = None) -> bool:
    if not name:
        return False
    if name in PII_PROPERTIES or name.lower() in PII_KEYS:
        return True
    return name == "name" and entity_type in PII_NAME_TYPES


def text_has_pii(value: str) -> bool:
    return bool(_EMAIL.search(value) or _IBAN.search(value) or _CF.search(value) or _VAT.search(value) or _PHONE.search(value))


def redact_text(value: str) -> str:
    redacted = _EMAIL.sub(MASK, value)
    redacted = _IBAN.sub(MASK, redacted)
    redacted = _CF.sub(MASK, redacted)
    redacted = _VAT.sub(MASK, redacted)
    return _PHONE.sub(MASK, redacted)


def redact(value: Any) -> Any:
    """Copy with personal data replaced. Does not mutate the input."""
    if isinstance(value, str):
        return redact_text(value) if text_has_pii(value) else value
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        prop = value.get("property")
        entity_type = value.get("entity_type")
        masked: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key == "value" and is_pii_property(str(prop) if prop is not None else None, str(entity_type) if entity_type else None):
                masked[key] = MASK
            elif is_pii_key(key_text):
                masked[key] = MASK
            else:
                masked[key] = redact(item)
        return masked
    return value


def mask_evidence_item(item: dict[str, Any]) -> dict[str, Any]:
    copy = dict(item)
    if is_pii_property(copy.get("property"), copy.get("entity_type")):
        copy["value"] = MASK
        copy["masked"] = True
    elif isinstance(copy.get("value"), str) and text_has_pii(copy["value"]):
        copy["value"] = MASK
        copy["masked"] = True
    return copy


def stage_landing(batch_id: str, rows: list[dict[str, Any]]) -> None:
    _STAGED[str(batch_id)] = [dict(row) for row in rows]


def staged_landing(batch_id: str) -> list[dict[str, Any]] | None:
    rows = _STAGED.get(str(batch_id))
    if rows is None:
        return None
    return [{"row_index": index, "data": dict(row)} for index, row in enumerate(rows)]


def clear_staged() -> None:
    _STAGED.clear()


def landing_rows(conn: Any, batch_id: str) -> list[dict[str, Any]]:
    """Original landing values for materialize.

    Same-transaction rows come from the process cache filled at ingest.
    After commit, the cleartext overlay is read as ``ieol_pii``.
    """
    staged = staged_landing(batch_id)
    if staged is not None:
        return staged
    rows = conn.execute(
        """
        SELECT row_index, data
        FROM app.landing_rows
        WHERE batch_id = %s
        ORDER BY row_index
        """,
        (batch_id,),
    ).fetchall()
    extra_rows = read_cleartext(
        conn,
        """
        SELECT row_index, data_cleartext
        FROM app.landing_rows
        WHERE batch_id = %s
        """,
        (batch_id,),
        purpose="landing_overlay",
        entity_ref=str(batch_id),
    )
    overlay = {row["row_index"]: row["data_cleartext"] or {} for row in extra_rows}
    merged = []
    for row in rows:
        data = dict(row["data"] or {})
        extra = overlay.get(row["row_index"]) or {}
        if isinstance(extra, dict):
            data.update(extra)
        merged.append({"row_index": row["row_index"], "data": data})
    return merged


class _RedactLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if isinstance(record.args, dict):
            record.args = {
                key: redact_text(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        elif record.args:
            record.args = tuple(redact_text(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


def read_cleartext(
    conn: Any,
    query: Any,
    params: Any = None,
    *,
    purpose: str,
    actor: str = "sistema",
    role: str = "sistema",
    entity_ref: str | None = None,
    action: str = "pii_read",
    summarize: Any = None,
) -> list[dict[str, Any]]:
    """Read as ``ieol_pii`` and write one audit row. The payload has no values.

    This is the only function that opens ``pii_connect``. ``summarize`` may
    add names (properties, reason, purpose) and must not add cleartext.
    """
    from services.audit import record
    from services.db import pii_connect

    with pii_connect() as reader:
        cursor = reader.execute(query) if params is None else reader.execute(query, params)
        if cursor.description is None:
            reader.rollback()
            raise ValueError("Il connector SQL accetta solo SELECT")
        rows = [dict(row) for row in cursor.fetchall()]
        reader.rollback()
    extra = summarize(rows) if summarize is not None else {}
    if not isinstance(extra, dict):
        raise RuntimeError("Il riepilogo della lettura non è un oggetto")
    record(
        conn,
        action=action,
        actor=actor,
        role=role,
        entity_ref=entity_ref,
        payload={"purpose": purpose, "rows": len(rows), **extra},
    )
    return rows


def unmask_entity(conn: Any, entity_id: str, reason: str, *, actor: str, role: str) -> dict[str, Any]:
    """Audit the request, then read cleartext as ieol_pii. The audit stores no values."""
    from services.errors import AppError
    from services.text import stable

    entity = conn.execute(
        """
        SELECT id::text, type FROM app.entities
        WHERE id = %s AND valid_to IS NULL
        """,
        (entity_id,),
    ).fetchone()
    if entity is None:
        raise AppError(404, "Entità assente")

    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "entity_id": entity_id,
            "entity_type": entity["type"],
            "properties": [row["property"] for row in rows],
            "reason": redact_text(reason),
        }

    rows = read_cleartext(
        conn,
        """
        SELECT property, value_cleartext
        FROM app.facts
        WHERE entity_id = %s AND valid_to IS NULL AND value_cleartext IS NOT NULL
        ORDER BY property
        """,
        (entity_id,),
        purpose="unmask",
        actor=actor,
        role=role,
        entity_ref=entity_id,
        action="pii_unmask",
        summarize=summarize,
    )
    return {
        "entity_id": entity_id,
        "properties": [
            {"property": row["property"], "value": stable(row["value_cleartext"])} for row in rows
        ],
    }


def install_log_redaction() -> None:
    """Mask personal data in log records before they are emitted."""
    root = logging.getLogger()
    if any(isinstance(item, _RedactLog) for item in root.filters):
        return
    root.addFilter(_RedactLog())
