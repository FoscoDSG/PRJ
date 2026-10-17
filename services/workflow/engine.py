"""State machine for a recommendation. The agent never executes from here."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.types.json import Json

from services.agent.order_agent import AGENT_VERSION, build_context
from services.audit import record
from services.errors import AppError
from services.mapping.threshold import active_threshold
from services.rules.engine import CFO_THRESHOLD, RULES_VERSION

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
    confidence_method: str = "rule",
    confidence_rule: str | None = None,
) -> str:
    _stored, method, rule = _score(confidence, confidence_method, confidence_rule)
    del _stored
    recommendation_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.recommendations (
          id, order_entity_id, problem, evidence, evidence_hash, actions,
          expected_impact, risk, confidence, status, requires_cfo,
          confidence_method, confidence_rule
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'detected', %s, %s, %s)
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
            method,
            rule,
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
    evidence_hash: str,
) -> dict:
    _require_role(role)
    row = _lock(conn, recommendation_id)
    _ensure_fresh(conn, row)
    _require_presented_hash(conn, row, evidence_hash)
    if row["status"] not in ("proposed", "modified"):
        raise AppError(409, "La raccomandazione non è modificabile")
    if quantity <= 0:
        raise AppError(400, "La quantità deve essere positiva")
    actions = row["actions"]
    updated = False
    previous = None
    for action in actions:
        if action["type"] == "createPurchaseOrder":
            previous = action["params"]["quantity"]
            action["params"]["quantity"] = quantity
            updated = True
    if not updated:
        raise AppError(400, "Nessuna azione di acquisto da modificare")
    conn.execute(
        """
        INSERT INTO app.agent_overrides (
          id, recommendation_id, field_name, previous_value, new_value, actor
        )
        VALUES (%s, %s, 'quantity', %s, %s, %s)
        """,
        (str(uuid.uuid4()), recommendation_id, Json(previous), Json(quantity), actor),
    )
    _record_decision(conn, row, decision="modify", actor=actor, role=role)
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


def reject(
    conn: psycopg.Connection,
    recommendation_id: str,
    *,
    reason: str,
    actor: str,
    role: str,
    evidence_hash: str,
) -> dict:
    _require_role(role)
    row = _lock(conn, recommendation_id)
    _require_presented_hash(conn, row, evidence_hash)
    if row["status"] in ("executed", "rejected", "expired", "failed"):
        raise AppError(409, "La raccomandazione non è rifiutabile")
    conn.execute(
        "UPDATE app.recommendations SET status = 'rejected' WHERE id = %s",
        (recommendation_id,),
    )
    _transition(conn, recommendation_id, row["status"], "rejected", actor)
    _record_decision(conn, row, decision="reject", actor=actor, role=role)
    record(
        conn,
        action="human_decision",
        actor=actor,
        role=role,
        entity_ref=recommendation_id,
        payload={"decision": "reject", "reason": reason},
    )
    return _get(conn, recommendation_id)


def mark_approved(
    conn: psycopg.Connection,
    recommendation_id: str,
    *,
    actor: str,
    role: str,
    evidence_hash: str,
) -> dict:
    _require_role(role)
    row = _lock(conn, recommendation_id)
    _ensure_fresh(conn, row)
    _require_presented_hash(conn, row, evidence_hash)
    if row["requires_cfo"] and role not in DECISION_ROLES:
        raise AppError(403, "Serve l'approvazione CFO")
    if row["status"] not in ("proposed", "modified"):
        raise AppError(409, "La raccomandazione non è approvabile")
    _refuse_unknown_provenance(conn, row)
    conn.execute(
        "UPDATE app.recommendations SET status = 'approved' WHERE id = %s",
        (recommendation_id,),
    )
    _transition(conn, recommendation_id, row["status"], "approved", actor)
    _record_decision(conn, row, decision="approve", actor=actor, role=role)
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


def _require_presented_hash(conn: psycopg.Connection, row: dict, evidence_hash: str) -> None:
    stored = row["evidence_hash"]
    try:
        current = build_context(conn, str(row["order_entity_id"]))["evidence_hash"]
    except ValueError:
        current = None
    if evidence_hash != stored or evidence_hash != current:
        raise AppError(409, "Le evidenze sono cambiate")


def _refuse_unknown_provenance(conn: psycopg.Connection, row: dict) -> None:
    evidence = row["evidence"] if isinstance(row["evidence"], list) else []
    fact_ids = [
        item["fact_id"]
        for item in evidence
        if isinstance(item, dict) and item.get("fact_id")
    ]
    if not fact_ids:
        raise AppError(422, "Approvazione rifiutata: provenienza sconosciuta")
    found = conn.execute(
        "SELECT source_class FROM app.facts WHERE id = ANY(%s::uuid[])",
        (fact_ids,),
    ).fetchall()
    if len(found) != len(set(fact_ids)) or any(item["source_class"] in (None, "unknown") for item in found):
        raise AppError(422, "Approvazione rifiutata: provenienza sconosciuta")


def _score(
    confidence: float | None,
    confidence_method: str | None,
    confidence_rule: str | None,
) -> tuple[float | None, str, str | None]:
    """A null score is allowed only with method unknown."""
    method = confidence_method or "unknown"
    if method not in ("rule", "human", "unknown"):
        method = "unknown"
    if confidence is None:
        return None, "unknown", None
    rule = confidence_rule if method == "rule" else None
    return confidence, method, rule


def _record_decision(
    conn: psycopg.Connection,
    row: dict,
    *,
    decision: str,
    actor: str,
    role: str,
) -> None:
    """Persist who decided and when. Recommendation and confidence come from the row."""
    header = _source_row(conn, str(row["order_entity_id"]))
    sources = _source_rows(conn, str(row["order_entity_id"]))
    confidence, method, rule = _score(
        row["confidence"],
        row.get("confidence_method"),
        row.get("confidence_rule"),
    )
    threshold, _measured, _support = active_threshold(conn)
    context = build_context(conn, str(row["order_entity_id"]))
    if decision == "reject":
        action_label: list = []
    else:
        action_label = list(row["actions"] or [])
    snapshot = {
        "order_key": context["order_key"],
        "amount": context["amount"],
        "customer_key": context["customer_key"],
        "lines": [
            {
                "product": line.get("product"),
                "shortage": line.get("shortage"),
                "quantity": line.get("quantity"),
            }
            for line in context["lines"]
        ],
        "suppliers": [
            {
                "source_key": supplier.get("source_key"),
                "product": supplier.get("product"),
                "delay_days": supplier.get("delay_days"),
            }
            for supplier in context["suppliers"]
        ],
    }
    decision_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.decisions_log (
          id, recommendation_id, batch_id, row_index, decision, evidence_hash,
          recommendation, confidence, confidence_method, confidence_rule,
          actor, actor_role, agent_version, rules_version,
          confidence_threshold, amount_threshold_eur, action_label, context_snapshot,
          hash_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            decision_id,
            str(row["id"]),
            header["batch_id"],
            header["row_index"],
            decision,
            row["evidence_hash"],
            row["problem"],
            confidence,
            method,
            rule,
            actor,
            role,
            AGENT_VERSION,
            RULES_VERSION,
            threshold,
            CFO_THRESHOLD,
            Json(action_label),
            Json(snapshot),
            "hmac-sha256-v1",
        ),
    )
    for source in sources:
        source_method = _source_confidence_method(conn, source["batch_id"], source["row_index"])
        conn.execute(
            """
            INSERT INTO app.decisions_log_sources (
              decision_id, batch_id, row_index, confidence_method
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (decision_id, source["batch_id"], source["row_index"], source_method),
        )


def _source_confidence_method(conn: psycopg.Connection, batch_id: str, row_index: int) -> str:
    """Weakest method of the facts written from this landing row.

    unknown wins. Otherwise a human fact wins over a rule. Calculated facts
    are not mapping targets and do not force unknown.
    """
    rows = conn.execute(
        """
        SELECT f.confidence_method
        FROM app.landing_rows lr
        JOIN app.landing_batches lb ON lb.id = lr.batch_id
        JOIN app.field_mappings fm
          ON fm.source_name = lb.source_name
         AND fm.valid_to IS NULL
         AND fm.status = 'confirmed'
         AND jsonb_exists(lr.data, fm.source_field)
        JOIN app.facts f
          ON f.source = lb.source_name
         AND f.property = fm.target_property
         AND f.valid_to IS NULL
         AND f.origin IS DISTINCT FROM 'calculated'
        WHERE lr.batch_id = %s AND lr.row_index = %s
        """,
        (batch_id, row_index),
    ).fetchall()
    methods = [row["confidence_method"] or "unknown" for row in rows]
    if not methods or any(method == "unknown" for method in methods):
        return "unknown"
    if any(method == "human" for method in methods):
        return "human"
    return "rule"


def _source_rows(conn: psycopg.Connection, order_entity_id: str) -> list[dict]:
    """Every landing row of the order header and of its lines."""
    order = conn.execute(
        "SELECT business_key FROM app.entities WHERE id = %s",
        (order_entity_id,),
    ).fetchone()
    if order is None:
        raise AppError(422, "Decisione rifiutata: riga di origine assente")
    rows = conn.execute(
        """
        SELECT lr.batch_id::text AS batch_id, lr.row_index
        FROM app.landing_rows lr
        JOIN app.landing_batches lb ON lb.id = lr.batch_id
        JOIN app.field_mappings fm
          ON fm.source_name = lb.source_name
         AND fm.valid_to IS NULL
         AND (
              (fm.target_type = 'Order' AND fm.target_property = 'source_key')
           OR (fm.target_type = 'OrderLine' AND fm.target_property = 'order_source_key')
         )
        WHERE lr.data ->> fm.source_field = %s
        GROUP BY lr.batch_id, lr.row_index
        ORDER BY lr.batch_id, lr.row_index
        """,
        (str(order["business_key"]),),
    ).fetchall()
    if not rows:
        raise AppError(422, "Decisione rifiutata: riga di origine assente")
    return list(rows)


def _source_row(conn: psycopg.Connection, order_entity_id: str) -> dict:
    order = conn.execute(
        "SELECT business_key FROM app.entities WHERE id = %s",
        (order_entity_id,),
    ).fetchone()
    if order is None:
        raise AppError(422, "Decisione rifiutata: riga di origine assente")
    found = conn.execute(
        """
        SELECT lr.batch_id::text AS batch_id, lr.row_index
        FROM app.landing_rows lr
        JOIN app.landing_batches lb ON lb.id = lr.batch_id
        JOIN app.field_mappings fm
          ON fm.source_name = lb.source_name
         AND fm.target_type = 'Order'
         AND fm.target_property = 'source_key'
         AND fm.valid_to IS NULL
        WHERE lr.data ->> fm.source_field = %s
        ORDER BY lb.loaded_at DESC, lr.row_index
        LIMIT 1
        """,
        (str(order["business_key"]),),
    ).fetchone()
    if found is None:
        raise AppError(422, "Decisione rifiutata: riga di origine assente")
    return found


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
