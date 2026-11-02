"""Append-only audit of proposals, decisions, dry-runs, external calls, and outcomes."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.types.json import Json


def record(
    conn: psycopg.Connection,
    *,
    action: str,
    actor: str | None,
    role: str | None,
    entity_ref: str | None,
    payload: dict[str, Any],
) -> str:
    audit_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.audit_log (id, actor, role, action, entity_ref, payload)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (audit_id, actor, role, action, entity_ref, Json(payload)),
    )
    return audit_id
