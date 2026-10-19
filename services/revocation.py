"""A decided row stays decided. Revocation is a new row, not a reopen.

The original decision is left as it was. A later ``revoked`` row points at
it, once. The revoking user's company is copied onto that new row. A
recommendation whose action already ran cannot be revoked here: undoing
that write is a separate compensation, and this module does not call it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Json

from services.audit import record
from services.companies import NO_COMPANY_UPLOAD
from services.errors import AppError

ROME = ZoneInfo("Europe/Rome")
QUEUE_MESSAGE = "La decisione è già presa e non torna in coda."
EXECUTED_MESSAGE = "L'azione è già stata eseguita e non si può annullare da qui."
REVOKED_EXECUTE_MESSAGE = "La decisione è stata annullata e non si può eseguire."
REASON_MESSAGE = "Scrivi il motivo dell'annullamento (almeno 5 caratteri)."
ALREADY_REVOKED = "Decisione già annullata"

_OPEN = {
    "field_mappings": frozenset({"proposed"}),
    "recommendations": frozenset({"detected", "proposed"}),
    "identity_reviews": frozenset({"open"}),
    "quarantine_documents": frozenset({"open"}),
    "drift_reviews": frozenset({"open"}),
}
_DECIDED = {
    "field_mappings": frozenset({"confirmed", "rejected"}),
    "recommendations": frozenset({
        "approved", "modified", "rejected", "dry_run_ok", "executed", "failed", "expired", "revoked",
    }),
    "identity_reviews": frozenset({"confirmed", "rejected"}),
    "quarantine_documents": frozenset({"released", "rejected"}),
    "drift_reviews": frozenset({"acknowledged"}),
}


def clean_reason(reason: str | None) -> str:
    """A revocation without a real motive is refused. Whitespace does not count."""
    cleaned = (reason or "").strip()
    if len(cleaned) < 5 or len(cleaned) > 500:
        raise AppError(422, REASON_MESSAGE)
    return cleaned


def assert_queue_transition(table: str, old: str | None, new: str) -> None:
    """Refuse a new transition from a decided status back to the queue.

    The first step into the queue (no previous status, or queue to queue)
    stays allowed. Existing rows are not read here.
    """
    if old is None or old == new:
        return
    decided = _DECIDED.get(table, frozenset())
    queue = _OPEN.get(table, frozenset())
    if old in decided and new in queue:
        raise AppError(409, QUEUE_MESSAGE)


def rome_stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ROME).strftime("%d/%m/%Y %H:%M")


def _require_company(company_id: str | None) -> str:
    if not company_id:
        raise AppError(403, NO_COMPANY_UPLOAD)
    return company_id


def _require_role(role: str, allowed: set[str], message: str) -> None:
    if role not in allowed:
        raise AppError(403, message)


def revoke_inbox_decision(
    conn: psycopg.Connection,
    decision_id: str,
    *,
    actor: str,
    role: str,
    company_id: str | None,
    reason: str,
) -> dict:
    """Append revoked and close the recommendation in the same transaction.

    The recommendation moves to ``revoked``. It does not go back to the
    queue, and a later execute is refused while this decision is the latest.
    """
    _require_role(role, {"admin", "approver"}, "Serve un approvatore")
    company_id = _require_company(company_id)
    motive = clean_reason(reason)
    row = conn.execute(
        """
        SELECT id::text, recommendation_id::text, batch_id::text, row_index,
               decision, evidence_hash, recommendation, confidence,
               confidence_method, confidence_rule, agent_version, rules_version,
               confidence_threshold, amount_threshold_eur, action_label,
               context_snapshot, hash_version, company_id::text AS company_id
        FROM app.decisions_log
        WHERE id = %s::uuid
          AND company_id = %s::uuid
        """,
        (decision_id, company_id),
    ).fetchone()
    if row is None or row["decision"] not in ("approve", "reject", "modify"):
        raise AppError(404, "Decisione assente")
    recommendation = conn.execute(
        "SELECT status FROM app.recommendations WHERE id = %s::uuid FOR UPDATE",
        (row["recommendation_id"],),
    ).fetchone()
    if recommendation is None:
        raise AppError(404, "Decisione assente")
    if recommendation["status"] == "executed":
        raise AppError(409, EXECUTED_MESSAGE)
    already = conn.execute(
        """
        SELECT 1 FROM app.decisions_log
        WHERE revoked_decision_id = %s::uuid
        """,
        (decision_id,),
    ).fetchone()
    if already:
        raise AppError(409, ALREADY_REVOKED)
    revoked_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.decisions_log (
          id, recommendation_id, batch_id, row_index, decision, evidence_hash,
          recommendation, confidence, confidence_method, confidence_rule,
          actor, actor_role, agent_version, rules_version,
          confidence_threshold, amount_threshold_eur, action_label, context_snapshot,
          hash_version, company_id, revoked_decision_id, reason
        )
        VALUES (
          %s, %s, %s, %s, 'revoked', %s,
          %s, %s, %s, %s,
          %s, %s, %s, %s,
          %s, %s, %s, %s,
          %s, %s, %s, %s
        )
        """,
        (
            revoked_id,
            row["recommendation_id"],
            row["batch_id"],
            row["row_index"],
            row["evidence_hash"],
            row["recommendation"],
            row["confidence"],
            row["confidence_method"],
            row["confidence_rule"],
            actor,
            role,
            row["agent_version"],
            row["rules_version"],
            row["confidence_threshold"],
            row["amount_threshold_eur"],
            Json(row["action_label"]) if row["action_label"] is not None else None,
            Json(row["context_snapshot"]) if row["context_snapshot"] is not None else None,
            row["hash_version"],
            company_id,
            decision_id,
            motive,
        ),
    )
    conn.execute(
        """
        INSERT INTO app.decisions_log_sources (decision_id, batch_id, row_index, confidence_method)
        SELECT %s::uuid, batch_id, row_index, confidence_method
        FROM app.decisions_log_sources
        WHERE decision_id = %s::uuid
        ON CONFLICT DO NOTHING
        """,
        (revoked_id, decision_id),
    )
    if recommendation["status"] != "revoked":
        from services.workflow.engine import _transition

        conn.execute(
            "UPDATE app.recommendations SET status = 'revoked' WHERE id = %s::uuid",
            (row["recommendation_id"],),
        )
        _transition(
            conn,
            row["recommendation_id"],
            recommendation["status"],
            "revoked",
            actor,
        )
    record(
        conn,
        action="human_decision",
        actor=actor,
        role=role,
        entity_ref=decision_id,
        payload={
            "kind": "inbox_decision_revoked",
            "revoked_decision_id": decision_id,
            "decision_id": revoked_id,
            "company_id": company_id,
            "reason": motive,
        },
    )
    return {
        "id": revoked_id,
        "revoked_decision_id": decision_id,
        "decision": "revoked",
        "company_id": company_id,
    }


def _revoke_state(
    conn: psycopg.Connection,
    *,
    table: str,
    subject_table: str,
    subject_id: str,
    decided_statuses: tuple[str, ...],
    actor: str,
    role: str,
    company_id: str | None,
    reason: str,
    missing: str,
) -> dict:
    company_id = _require_company(company_id)
    motive = clean_reason(reason)
    subject = conn.execute(
        f"""
        SELECT id::text, status, company_id::text AS company_id
        FROM {subject_table}
        WHERE id = %s::uuid
        """,
        (subject_id,),
    ).fetchone()
    if subject is None or subject["status"] not in decided_statuses:
        raise AppError(404, missing)
    if subject["company_id"] != company_id:
        raise AppError(404, missing)
    already = conn.execute(
        f"""
        SELECT 1 FROM {table}
        WHERE revoked_decision_id = %s::uuid
        """,
        (subject_id,),
    ).fetchone()
    if already:
        raise AppError(409, ALREADY_REVOKED)
    revoked_id = str(uuid.uuid4())
    conn.execute(
        f"""
        INSERT INTO {table} (
          id, company_id, decision, revoked_decision_id, reason, actor, actor_role
        )
        VALUES (%s, %s, 'revoked', %s, %s, %s, %s)
        """,
        (revoked_id, company_id, subject_id, motive, actor, role),
    )
    record(
        conn,
        action="human_decision",
        actor=actor,
        role=role,
        entity_ref=subject_id,
        payload={
            "kind": "state_decision_revoked",
            "table": table,
            "revoked_decision_id": subject_id,
            "decision_id": revoked_id,
            "company_id": company_id,
            "reason": motive,
        },
    )
    return {
        "id": revoked_id,
        "revoked_decision_id": subject_id,
        "decision": "revoked",
        "company_id": company_id,
        "status": subject["status"],
    }


def revoke_quarantine(
    conn: psycopg.Connection,
    quarantine_id: str,
    *,
    actor: str,
    role: str,
    company_id: str | None,
    reason: str,
) -> dict:
    _require_role(role, {"admin", "mapper"}, "Serve il ruolo mapper")
    return _revoke_state(
        conn,
        table="app.quarantine_decisions",
        subject_table="app.quarantine_documents",
        subject_id=quarantine_id,
        decided_statuses=("released", "rejected"),
        actor=actor,
        role=role,
        company_id=company_id,
        reason=reason,
        missing="Documento in quarantena assente",
    )


def revoke_identity(
    conn: psycopg.Connection,
    review_id: str,
    *,
    actor: str,
    role: str,
    company_id: str | None,
    reason: str,
) -> dict:
    _require_role(role, {"admin", "mapper"}, "Serve il ruolo mapper")
    return _revoke_state(
        conn,
        table="app.identity_review_decisions",
        subject_table="app.identity_reviews",
        subject_id=review_id,
        decided_statuses=("confirmed", "rejected"),
        actor=actor,
        role=role,
        company_id=company_id,
        reason=reason,
        missing="Revisione identità assente",
    )


def revoke_drift(
    conn: psycopg.Connection,
    drift_id: str,
    *,
    actor: str,
    role: str,
    company_id: str | None,
    reason: str,
) -> dict:
    _require_role(role, {"admin", "mapper"}, "Serve il ruolo mapper")
    return _revoke_state(
        conn,
        table="app.drift_decisions",
        subject_table="app.drift_reviews",
        subject_id=drift_id,
        decided_statuses=("acknowledged",),
        actor=actor,
        role=role,
        company_id=company_id,
        reason=reason,
        missing="Drift assente",
    )


def _history_entry(
    *,
    decision_id: str,
    decision: str,
    actor: str | None,
    decided_at,
    revoker: str | None,
    revoked_at,
    revoke_reason: str | None,
) -> dict:
    revoked = revoker is not None
    return {
        "id": decision_id,
        "decision": decision,
        "actor": actor,
        "decided_at": rome_stamp(decided_at),
        "revoked": revoked,
        "revoker": revoker if revoked else None,
        "revoked_at": rome_stamp(revoked_at) if revoked else None,
        "revoke_reason": revoke_reason if revoked else None,
    }


def mapping_histories(
    conn: psycopg.Connection,
    mapping_ids: list[str],
    company_id: str | None = None,
) -> dict[str, list[dict]]:
    """Decisions of this company, plus rows that never had a company.

    Another company's correction stays out of the list, even on a shared mapping.
    """
    if not mapping_ids:
        return {}
    rows = conn.execute(
        """
        SELECT d.mapping_id::text AS mapping_id, d.id::text AS id, d.decision,
               d.actor, d.decided_at, rev.actor AS revoker, rev.decided_at AS revoked_at,
               rev.reason AS revoke_reason
        FROM app.mapping_decisions d
        LEFT JOIN app.mapping_decisions rev ON rev.revoked_decision_id = d.id
        WHERE d.mapping_id = ANY(%s::uuid[])
          AND d.decision IN ('confirm', 'reject')
          AND (
            d.company_id IS NULL
            OR (%s::uuid IS NOT NULL AND d.company_id = %s::uuid)
          )
        ORDER BY d.decided_at, d.id
        """,
        (mapping_ids, company_id, company_id),
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["mapping_id"], []).append(
            _history_entry(
                decision_id=row["id"],
                decision=row["decision"],
                actor=row["actor"],
                decided_at=row["decided_at"],
                revoker=row["revoker"],
                revoked_at=row["revoked_at"],
                revoke_reason=row["revoke_reason"],
            )
        )
    return grouped


def inbox_histories(conn: psycopg.Connection, recommendation_ids: list[str]) -> dict[str, list[dict]]:
    if not recommendation_ids:
        return {}
    rows = conn.execute(
        """
        SELECT d.recommendation_id::text AS recommendation_id, d.id::text AS id,
               d.decision, d.actor, d.decided_at, rev.actor AS revoker,
               rev.decided_at AS revoked_at, rev.reason AS revoke_reason
        FROM app.decisions_log d
        LEFT JOIN app.decisions_log rev ON rev.revoked_decision_id = d.id
        WHERE d.recommendation_id = ANY(%s::uuid[])
          AND d.decision IN ('approve', 'reject', 'modify')
        ORDER BY d.decided_at, d.id
        """,
        (recommendation_ids,),
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["recommendation_id"], []).append(
            _history_entry(
                decision_id=row["id"],
                decision=row["decision"],
                actor=row["actor"],
                decided_at=row["decided_at"],
                revoker=row["revoker"],
                revoked_at=row["revoked_at"],
                revoke_reason=row["revoke_reason"],
            )
        )
    return grouped


def state_histories(
    conn: psycopg.Connection,
    *,
    subject_table: str,
    revocation_table: str,
    subject_ids: list[str],
    decision_for_status: dict[str, str],
) -> dict[str, list[dict]]:
    if not subject_ids:
        return {}
    rows = conn.execute(
        f"""
        SELECT s.id::text AS id, s.status, s.actor, s.created_at,
               rev.actor AS revoker, rev.decided_at AS revoked_at, rev.reason AS revoke_reason
        FROM {subject_table} s
        LEFT JOIN {revocation_table} rev ON rev.revoked_decision_id = s.id
        WHERE s.id = ANY(%s::uuid[])
        """,
        (subject_ids,),
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        decision = decision_for_status.get(row["status"])
        if decision is None:
            grouped[row["id"]] = []
            continue
        grouped[row["id"]] = [
            _history_entry(
                decision_id=row["id"],
                decision=decision,
                actor=row["actor"],
                decided_at=row["created_at"],
                revoker=row["revoker"],
                revoked_at=row["revoked_at"],
                revoke_reason=row["revoke_reason"],
            )
        ]
    return grouped
