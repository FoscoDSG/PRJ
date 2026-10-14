"""State machine for a recommendation. The agent never executes from here."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.types.json import Json

from services.agent.order_agent import build_context
from services.audit import record
from services.errors import AppError

OPEN_STATES = ("detected", "proposed", "approved", "modified", "dry_run_ok")
DECISION_ROLES = {"admin", "approver"}


def propose(
    conn: psycopg.Connection,
    *,
    order_id: str,
    problem: str,
    evidence: list[dict],
    evidence_hash: str,
    actions: list[dict],
    expected_impact: str,
    risk: str,
    confidence: float,
    requires_cfo: bool,
    actor: str | None,
    role: str | None,
) -> str:
    recommendation_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.recommendations (
          id, order_entity_id, problem, evidence, evidence_hash, actions,
          expected_impact, risk, confidence, status, requires_cfo
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'detected', %s)
        """,
        (
            recommendation_id,
            order_id,
            problem,
            Json(evidence),
            evidence_hash,
            Json(actions),
            expected_impact,
            risk,
            confidence,
            requires_cfo,
        ),
    )
    _transition(conn, recommendation_id, None, "detected", actor)
    _transition(conn, recommendation_id, "detected", "proposed", actor)
    conn.execute(
        "UPDATE app.recommendations SET status = 'proposed' WHERE id = %s",
        (recommendation_id,),
    )
    record(
        conn,
        action="proposal",
        actor=actor,
        role=role,
        entity_ref=recommendation_id,
        payload={"order_id": order_id, "problem": problem, "actions": actions, "requires_cfo": requires_cfo},
    )
    return recommendation_id


def modify(
    conn: psycopg.Connection,
    recommendation_id: str,
    *,
    quantity: int,
    actor: str,
    role: str,
) -> dict:
    _require_role(role)
    row = _lock(conn, recommendation_id)
    _ensure_fresh(conn, row)
    if row["status"] not in ("proposed", "modified"):
        raise AppError(409, "La raccomandazione non è modificabile")
    if quantity <= 0:
        raise AppError(400, "La quantità deve essere positiva")
    actions = row["actions"]
    updated = False
    for action in actions:
        if action["type"] == "createPurchaseOrder":
            action["params"]["quantity"] = quantity
            updated = True
    if not updated:
        raise AppError(400, "Nessuna azione di acquisto da modificare")
    conn.execute(
        "UPDATE app.recommendations SET actions = %s, status = 'modified' WHERE id = %s",
        (Json(actions), recommendation_id),
    )
    _transition(conn, recommendation_id, row["status"], "modified", actor)
    record(
        conn,
        action="human_decision",
        actor=actor,
        role=role,
        entity_ref=recommendation_id,
        payload={"decision": "modify", "quantity": quantity},
    )
    return _get(conn, recommendation_id)


def reject(conn: psycopg.Connection, recommendation_id: str, *, reason: str, actor: str, role: str) -> dict:
    _require_role(role)
    row = _lock(conn, recommendation_id)
    if row["status"] not in ("proposed", "modified"):
        raise AppError(409, "La raccomandazione non è rifiutabile")
    conn.execute(
        "UPDATE app.recommendations SET status = 'rejected' WHERE id = %s",
        (recommendation_id,),
    )
    _transition(conn, recommendation_id, row["status"], "rejected", actor)
    record(
        conn,
        action="human_decision",
        actor=actor,
        role=role,
        entity_ref=recommendation_id,
        payload={"decision": "reject", "reason": reason},
    )
    return _get(conn, recommendation_id)


def mark_approved(conn: psycopg.Connection, recommendation_id: str, *, actor: str, role: str) -> dict:
    if role not in DECISION_ROLES | {"operator"}:
        raise AppError(403, "Serve un approvatore")
    row = _lock(conn, recommendation_id)
    _ensure_fresh(conn, row)
    if row["requires_cfo"] and role not in DECISION_ROLES:
        raise AppError(403, "Serve l'approvazione CFO")
    if row["status"] not in ("proposed", "modified"):
        raise AppError(409, "La raccomandazione non è approvabile")
    conn.execute(
        "UPDATE app.recommendations SET status = 'approved' WHERE id = %s",
        (recommendation_id,),
    )
    _transition(conn, recommendation_id, row["status"], "approved", actor)
    record(
        conn,
        action="human_decision",
        actor=actor,
        role=role,
        entity_ref=recommendation_id,
        payload={"decision": "approve", "requires_cfo": row["requires_cfo"]},
    )
    return _get(conn, recommendation_id)


def mark_dry_run(conn: psycopg.Connection, recommendation_id: str, payloads: list[dict], *, actor: str, role: str) -> None:
    row = _get(conn, recommendation_id)
    conn.execute(
        """
        INSERT INTO app.action_batches (id, recommendation_id, kind, status, payloads)
        VALUES (%s, %s, 'dry_run', 'dry_run_ok', %s)
        """,
        (str(uuid.uuid4()), recommendation_id, Json(payloads)),
    )
    conn.execute(
        "UPDATE app.recommendations SET status = 'dry_run_ok' WHERE id = %s",
        (recommendation_id,),
    )
    _transition(conn, recommendation_id, row["status"], "dry_run_ok", actor)
    record(
        conn,
        action="dry_run",
        actor=actor,
        role=role,
        entity_ref=recommendation_id,
        payload={"payloads": payloads},
    )


def mark_executed(conn: psycopg.Connection, recommendation_id: str, *, actor: str, role: str, payload: dict) -> None:
    row = _get(conn, recommendation_id)
    conn.execute(
        "UPDATE app.recommendations SET status = 'executed' WHERE id = %s",
        (recommendation_id,),
    )
    _transition(conn, recommendation_id, row["status"], "executed", actor)
    record(
        conn,
        action="outcome",
        actor=actor,
        role=role,
        entity_ref=recommendation_id,
        payload=payload,
    )


def mark_failed(conn: psycopg.Connection, recommendation_id: str, *, actor: str, role: str, message: str) -> None:
    row = _get(conn, recommendation_id)
    conn.execute(
        "UPDATE app.recommendations SET status = 'failed' WHERE id = %s",
        (recommendation_id,),
    )
    _transition(conn, recommendation_id, row["status"], "failed", actor)
    record(
        conn,
        action="outcome",
        actor=actor,
        role=role,
        entity_ref=recommendation_id,
        payload={"status": "failed", "message": message},
    )


def expire_stale(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT id::text, order_entity_id::text, evidence_hash, status
        FROM app.recommendations
        WHERE status = ANY(%s)
        """,
        (list(OPEN_STATES),),
    ).fetchall()
    expired = []
    for row in rows:
        try:
            current = build_context(conn, row["order_entity_id"])["evidence_hash"]
        except ValueError:
            current = None
        if current != row["evidence_hash"]:
            conn.execute(
                "UPDATE app.recommendations SET status = 'expired' WHERE id = %s",
                (row["id"],),
            )
            _transition(conn, row["id"], row["status"], "expired", "system")
            expired.append(row["id"])
    return expired


def _ensure_fresh(conn: psycopg.Connection, row: dict) -> None:
    expire_stale(conn)
    current = _get(conn, row["id"])
    if current["status"] == "expired":
        raise AppError(409, "Le evidenze sono cambiate: la raccomandazione è scaduta")
    row["status"] = current["status"]


def _require_role(role: str) -> None:
    if role not in DECISION_ROLES:
        raise AppError(403, "Serve un approvatore")


def _lock(conn: psycopg.Connection, recommendation_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM app.recommendations WHERE id = %s FOR UPDATE",
        (recommendation_id,),
    ).fetchone()
    if row is None:
        raise AppError(404, "Raccomandazione assente")
    return row


def _get(conn: psycopg.Connection, recommendation_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM app.recommendations WHERE id = %s",
        (recommendation_id,),
    ).fetchone()
    if row is None:
        raise AppError(404, "Raccomandazione assente")
    return row


def _transition(
    conn: psycopg.Connection,
    recommendation_id: str,
    from_status: str | None,
    to_status: str,
    actor: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO app.workflow_events (id, recommendation_id, from_status, to_status, actor)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (str(uuid.uuid4()), recommendation_id, from_status, to_status, actor),
    )
