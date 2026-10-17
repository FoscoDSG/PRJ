"""Dry-run shows the ERP payload. Execute writes the sandbox once per idempotency key."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.types.json import Json

from ontology.loader import Ontology
from services.audit import record
from services.errors import AppError
from services.pii import MASK
from services.graph.projection import record_trigger
from services.workflow.engine import expire_stale, mark_dry_run, mark_executed, mark_failed


def payloads_for(conn: psycopg.Connection, recommendation: dict) -> list[dict[str, Any]]:
    if recommendation.get("status") == "dry_run_ok":
        stored = _latest_dry_run(conn, str(recommendation["id"]))
        if stored:
            return stored
    return [_payload(conn, action, recommendation) for action in recommendation["actions"]]


def dry_run(
    conn: psycopg.Connection,
    recommendation_id: str,
    *,
    actor: str,
    role: str,
) -> list[dict[str, Any]]:
    row = _recommendation(conn, recommendation_id)
    if row["status"] == "dry_run_ok":
        stored = conn.execute(
            """
            SELECT payloads FROM app.action_batches
            WHERE recommendation_id = %s AND kind = 'dry_run'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (recommendation_id,),
        ).fetchone()
        return stored["payloads"] if stored else []
    if row["status"] != "approved":
        raise AppError(409, "Il dry-run segue l'approvazione")
    payloads = [_payload(conn, action, row) for action in row["actions"]]
    mark_dry_run(conn, recommendation_id, payloads, actor=actor, role=role)
    return payloads


def execute(
    conn: psycopg.Connection,
    ontology: Ontology,
    recommendation_id: str,
    *,
    actor: str,
    role: str,
) -> dict[str, Any]:
    expire_stale(conn)
    row = conn.execute(
        "SELECT * FROM app.recommendations WHERE id = %s FOR UPDATE",
        (recommendation_id,),
    ).fetchone()
    if row is None:
        raise AppError(404, "Raccomandazione assente")
    if row["status"] == "expired":
        conn.commit()
        raise AppError(409, "Le evidenze sono cambiate: la raccomandazione è scaduta")
    if row["status"] == "executed":
        return _existing_result(conn, recommendation_id)
    if row["status"] != "dry_run_ok":
        raise AppError(409, "Esegui solo dopo il dry-run")
    stored = _latest_dry_run(conn, recommendation_id)
    if not stored:
        raise AppError(409, "Manca il payload approvato")
    created: list[dict[str, Any]] = []
    try:
        with conn.transaction():
            for index, payload in enumerate(stored):
                action_type = payload["action"]
                key = f"{_idempotency_key_from_payload(recommendation_id, payload)}:{index}"
                if action_type in ("createPurchaseOrder", "createOrder"):
                    document = _sandbox(conn, action_type, key, payload)
                    record(
                        conn,
                        action="external_call",
                        actor=actor,
                        role=role,
                        entity_ref=document["id"],
                        payload={"port": "ErpPort", "idempotency_key": key, "payload": payload},
                    )
                    created.append(document)
                elif action_type in ("notifyCustomer", "sendConfirmation"):
                    email = _email(conn, key, payload, str(row["order_entity_id"]))
                    record(
                        conn,
                        action="external_call",
                        actor=actor,
                        role=role,
                        entity_ref=email["id"],
                        payload={"port": "EmailPort", "idempotency_key": key, "payload": payload},
                    )
                    created.append(email)
            event_id = _event(conn, ontology, row, created)
            record_trigger(conn, str(row["order_entity_id"]), event_id)
            result = {"documents": created, "event_id": event_id}
            mark_executed(conn, recommendation_id, actor=actor, role=role, payload=result)
        return result
    except AppError:
        raise
    except Exception as exc:
        mark_failed(conn, recommendation_id, actor=actor, role=role, message=str(exc))
        conn.commit()
        raise AppError(500, "Esecuzione fallita") from exc


def compensate(conn: psycopg.Connection, document_id: str, *, actor: str, role: str) -> dict:
    row = conn.execute(
        "SELECT * FROM app.sandbox_documents WHERE id = %s FOR UPDATE",
        (document_id,),
    ).fetchone()
    if row is None:
        raise AppError(404, "Documento sandbox assente")
    if row["status"] == "compensated":
        return row
    compensation = row["compensation"] or {
        "action": "cancelPurchaseOrder" if row["kind"] == "purchase_order" else "cancelOrder",
        "of": str(row["id"]),
    }
    conn.execute(
        """
        UPDATE app.sandbox_documents
        SET status = 'compensated', compensation = %s
        WHERE id = %s
        """,
        (Json(compensation), document_id),
    )
    record(
        conn,
        action="external_call",
        actor=actor,
        role=role,
        entity_ref=document_id,
        payload={"port": "ErpPort", "compensation": compensation},
    )
    record(
        conn,
        action="outcome",
        actor=actor,
        role=role,
        entity_ref=document_id,
        payload={"status": "compensated"},
    )
    return conn.execute(
        "SELECT * FROM app.sandbox_documents WHERE id = %s",
        (document_id,),
    ).fetchone()


def _payload(conn: psycopg.Connection, action: dict, recommendation: dict) -> dict[str, Any]:
    params = dict(action.get("params") or {})
    kind = action["type"]
    order_key = params.get("order")
    if kind == "createPurchaseOrder":
        return {
            "action": "createPurchaseOrder",
            "order": order_key,
            "supplier": params.get("supplier"),
            "product": params.get("product"),
            "quantity": params.get("quantity"),
        }
    if kind == "createOrder":
        return {
            "action": "createOrder",
            "order": order_key,
            "customer": params.get("customer"),
            "product": params.get("product"),
            "quantity": params.get("quantity"),
        }
    return {
        "action": kind,
        "order": order_key,
        "customer": params.get("customer"),
        "recipient": MASK,
        "subject": f"Aggiornamento ordine {order_key}",
        "body": recommendation["problem"],
    }


def _latest_dry_run(conn: psycopg.Connection, recommendation_id: str) -> list[dict]:
    stored = conn.execute(
        """
        SELECT payloads FROM app.action_batches
        WHERE recommendation_id = %s AND kind = 'dry_run'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (recommendation_id,),
    ).fetchone()
    if stored is None:
        return []
    return list(stored["payloads"] or [])


def _idempotency_key_from_payload(recommendation_id: str, payload: dict) -> str:
    return ":".join(
        [
            recommendation_id,
            str(payload.get("action") or ""),
            str(payload.get("supplier") or ""),
            str(payload.get("product") or ""),
            str(payload.get("order") or ""),
        ]
    )


def _idempotency_key(recommendation_id: str, action: dict) -> str:
    params = action.get("params") or {}
    parts = [
        recommendation_id,
        action["type"],
        str(params.get("supplier") or ""),
        str(params.get("product") or ""),
        str(params.get("order") or ""),
    ]
    return ":".join(parts)


def _sandbox(conn: psycopg.Connection, action_type: str, key: str, payload: dict) -> dict:
    kind = "purchase_order" if action_type == "createPurchaseOrder" else "sales_order"
    compensation = {
        "action": "cancelPurchaseOrder" if kind == "purchase_order" else "cancelOrder",
        "idempotency_key": key,
    }
    document_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.sandbox_documents (id, kind, idempotency_key, payload, status, compensation)
        VALUES (%s, %s, %s, %s, 'created', %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (document_id, kind, key, Json(payload), Json(compensation)),
    )
    return conn.execute(
        "SELECT id::text, kind, idempotency_key, payload, status, compensation FROM app.sandbox_documents WHERE idempotency_key = %s",
        (key,),
    ).fetchone()


def _email(conn: psycopg.Connection, key: str, payload: dict, order_id: str) -> dict:
    conn.execute(
        "SELECT app.queue_customer_email(%s::uuid, %s, %s, %s)",
        (order_id, key, payload.get("subject") or "", payload.get("body") or ""),
    )
    row = conn.execute(
        """
        SELECT id::text, idempotency_key, recipient, subject, body
        FROM app.email_outbox WHERE idempotency_key = %s
        """,
        (key,),
    ).fetchone()
    return {**row, "kind": "email", "status": "queued"}


def _event(conn: psycopg.Connection, ontology: Ontology, recommendation: dict, created: list[dict]) -> str:
    from services.canonical.store import _ensure_entity, _write_facts

    summary = recommendation["problem"]
    event_key = f"action:{recommendation['id']}"
    event_id = _ensure_entity(conn, "Event", event_key)
    _write_facts(
        conn,
        ontology,
        event_id,
        "Event",
        {"event_type": "action_executed", "summary": summary},
        "action",
        {},
        origin="approved",
    )
    return event_id


def _existing_result(conn: psycopg.Connection, recommendation_id: str) -> dict[str, Any]:
    batch = conn.execute(
        """
        SELECT payloads FROM app.action_batches
        WHERE recommendation_id = %s AND kind = 'execute'
        ORDER BY created_at DESC LIMIT 1
        """,
        (recommendation_id,),
    ).fetchone()
    documents = conn.execute(
        """
        SELECT id::text, kind, idempotency_key, payload, status
        FROM app.sandbox_documents
        WHERE idempotency_key LIKE %s
        ORDER BY created_at
        """,
        (f"{recommendation_id}:%",),
    ).fetchall()
    event = conn.execute(
        """
        SELECT id::text FROM app.entities
        WHERE type = 'Event' AND business_key = %s
        """,
        (f"action:{recommendation_id}",),
    ).fetchone()
    return {
        "documents": list(documents),
        "event_id": event["id"] if event else None,
        "replayed": True,
        "batch": batch["payloads"] if batch else None,
    }


def _recommendation(conn: psycopg.Connection, recommendation_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM app.recommendations WHERE id = %s",
        (recommendation_id,),
    ).fetchone()
    if row is None:
        raise AppError(404, "Raccomandazione assente")
    return row


