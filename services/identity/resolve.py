"""Queue doubtful matches. Identical identity values already share one entity."""

from __future__ import annotations

import re
import unicodedata

import psycopg

from services.companies import NO_COMPANY_UPLOAD
from services.errors import AppError
from services.revocation import QUEUE_MESSAGE

_LEGAL = re.compile(r"\b(srl|s\.r\.l|spa|s\.p\.a|sas|snc|srls|llc|ltd)\b", re.IGNORECASE)


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.strip().lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _LEGAL.sub(" ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


def name_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 0.95
    left_tokens = left.split()
    right_tokens = right.split()
    if left_tokens[-1] == right_tokens[-1] and left_tokens[0][:1] == right_tokens[0][:1]:
        return 0.72
    return 0.0


def queue_identity_doubts(conn: psycopg.Connection) -> int:
    """Pair close names in the database. The reason does not contain the names."""
    row = conn.execute("SELECT app.queue_name_reviews() AS created").fetchone()
    return int(row["created"] or 0)


def confirm_review(
    conn: psycopg.Connection,
    review_id: str,
    *,
    actor: str,
    company_id: str | None,
) -> dict:
    if not company_id:
        raise AppError(403, NO_COMPANY_UPLOAD)
    review = _lock_review(conn, review_id)
    if review["status"] != "open":
        raise AppError(409, QUEUE_MESSAGE)
    left = _entity_by_key(conn, review["entity_type"], review["left_key"])
    right = _entity_by_key(conn, review["entity_type"], review["right_key"])
    if left and right and left != right:
        conn.execute(
            """
            UPDATE app.identity_aliases
            SET entity_id = %s
            WHERE entity_id = %s
            """,
            (left, right),
        )
        conn.execute(
            """
            INSERT INTO app.identity_aliases (entity_type, alias, entity_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (entity_type, alias) DO UPDATE SET entity_id = EXCLUDED.entity_id
            """,
            (review["entity_type"], review["right_key"], left),
        )
        conn.execute("UPDATE app.entities SET valid_to = now() WHERE id = %s", (right,))
    conn.execute(
        """
        UPDATE app.identity_reviews
        SET status = 'confirmed', company_id = %s, actor = %s
        WHERE id = %s
        """,
        (company_id, actor, review_id),
    )
    return {"id": review_id, "status": "confirmed"}


def reject_review(
    conn: psycopg.Connection,
    review_id: str,
    *,
    actor: str,
    company_id: str | None,
) -> dict:
    if not company_id:
        raise AppError(403, NO_COMPANY_UPLOAD)
    review = _lock_review(conn, review_id)
    if review["status"] != "open":
        raise AppError(409, QUEUE_MESSAGE)
    conn.execute(
        """
        UPDATE app.identity_reviews
        SET status = 'rejected', company_id = %s, actor = %s
        WHERE id = %s
        """,
        (company_id, actor, review_id),
    )
    return {"id": review_id, "status": "rejected"}


def list_reviews(conn: psycopg.Connection) -> list[dict]:
    from services.revocation import state_histories

    rows = list(
        conn.execute(
            """
            SELECT id::text, entity_type, left_key, right_key, score, reason, status
            FROM app.identity_reviews
            ORDER BY created_at DESC
            """
        ).fetchall()
    )
    histories = state_histories(
        conn,
        subject_table="app.identity_reviews",
        revocation_table="app.identity_review_decisions",
        subject_ids=[row["id"] for row in rows],
        decision_for_status={"confirmed": "confirmed", "rejected": "rejected"},
    )
    for row in rows:
        row["history"] = histories.get(row["id"], [])
        row["executed"] = False
    return rows


def _entity_by_key(conn: psycopg.Connection, entity_type: str, business_key: str) -> str | None:
    row = conn.execute(
        """
        SELECT id::text FROM app.entities
        WHERE type = %s AND business_key = %s AND valid_to IS NULL
        """,
        (entity_type, business_key),
    ).fetchone()
    return row["id"] if row else None


def _lock_review(conn: psycopg.Connection, review_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM app.identity_reviews WHERE id = %s FOR UPDATE",
        (review_id,),
    ).fetchone()
    if row is None:
        raise AppError(404, "Revisione identità assente")
    return row
