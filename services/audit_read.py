"""Readable audit. Italian labels, Europe/Rome, no writes."""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg

from services.pii import redact, redact_text

ROLE_LABELS = {
    "admin": "amministratore",
    "approver": "approvatore",
    "operator": "operatore",
    "mapper": "mapper",
}
ACTION_LABELS = {
    "proposal": "Proposta",
    "human_decision": "Decisione umana",
    "dry_run": "Prova a secco",
    "external_call": "Chiamata esterna",
    "outcome": "Esito",
    "pii_unmask": "Smascheramento dati personali",
    "pii_read": "Lettura dati personali",
    "pii_mask": "Mascheramento dati personali",
    "approve": "Approvazione",
    "reject": "Rifiuto",
    "modify": "Modifica",
    "decision": "Decisione",
    "status": "Stato",
    "quantity": "Quantità",
    "reason": "Motivo",
    "problem": "Problema",
}

_EVENTS = """
SELECT kind, actor, role, code, at, order_key, batch_id, detail
FROM (
  SELECT
    'audit'::text AS kind,
    a.actor,
    a.role,
    a.action AS code,
    a.at,
    e.business_key AS order_key,
    d.batch_id::text AS batch_id,
    a.payload AS detail
  FROM app.audit_log a
  LEFT JOIN app.recommendations r ON r.id::text = a.entity_ref
  LEFT JOIN app.entities e ON e.id = r.order_entity_id
  LEFT JOIN LATERAL (
    SELECT batch_id
    FROM app.decisions_log
    WHERE recommendation_id = r.id
    ORDER BY decided_at DESC
    LIMIT 1
  ) d ON true
  UNION ALL
  SELECT
    'decision'::text,
    dl.actor,
    dl.actor_role,
    dl.decision,
    dl.decided_at,
    ent.business_key,
    dl.batch_id::text,
    to_jsonb(dl.recommendation)
  FROM app.decisions_log dl
  LEFT JOIN app.recommendations r ON r.id = dl.recommendation_id
  LEFT JOIN app.entities ent ON ent.id = r.order_entity_id
) events
"""


def list_readable(
    conn: psycopg.Connection,
    *,
    user: str | None = None,
    order: str | None = None,
    batch: str | None = None,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Read audit and decisions. The transaction is read-only."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    conn.rollback()
    conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    try:
        where, params = _filters(user, order, batch, date_from, date_to)
        total = conn.execute(
            f"SELECT count(*) AS n FROM ({_EVENTS}) listed {where}",
            params,
        ).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT kind, actor, role, code,
                   to_char(at AT TIME ZONE 'Europe/Rome', 'DD/MM/YYYY HH24:MI:SS') AS when_rome,
                   order_key, detail
            FROM ({_EVENTS}) listed
            {where}
            ORDER BY at DESC, kind
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        ).fetchall()
        conn.commit()
    finally:
        conn.rollback()
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")
    return {
        "events": [_event(row) for row in rows],
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


def _filters(
    user: str | None,
    order: str | None,
    batch: str | None,
    date_from: date | str | None,
    date_to: date | str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if user:
        clauses.append("actor = %s")
        params.append(user)
    if order:
        clauses.append("order_key = %s")
        params.append(order)
    if batch:
        clauses.append("batch_id = %s")
        params.append(batch)
    if date_from:
        clauses.append("at >= (%s::date::timestamp AT TIME ZONE 'Europe/Rome')")
        params.append(date_from)
    if date_to:
        clauses.append("at < ((%s::date + 1)::timestamp AT TIME ZONE 'Europe/Rome')")
        params.append(date_to)
    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def _event(row: dict[str, Any]) -> dict[str, Any]:
    code = str(row["code"] or "")
    role = row["role"] or ""
    actor = row["actor"] or "sistema"
    role_label = ROLE_LABELS.get(role, role or "ruolo non indicato")
    detail = _detail(row["detail"])
    return {
        "kind": row["kind"],
        "who": f"{actor} · {role_label}",
        "when": row["when_rome"],
        "what": ACTION_LABELS.get(code, code),
        "action": code,
        "order": row["order_key"],
        "detail": detail,
    }


def _detail(value: Any) -> str:
    cleaned = redact(value)
    if isinstance(cleaned, str):
        return redact_text(cleaned)
    if isinstance(cleaned, dict):
        parts = []
        for key in ("decision", "status", "quantity", "reason", "problem"):
            if key in cleaned and cleaned[key] not in (None, ""):
                shown = cleaned[key]
                if isinstance(shown, str):
                    shown = ACTION_LABELS.get(shown, shown)
                parts.append(f"{ACTION_LABELS.get(key, key)}: {shown}")
        if parts:
            return redact_text("; ".join(parts))
    if cleaned in (None, ""):
        return ""
    return redact_text(str(cleaned))
