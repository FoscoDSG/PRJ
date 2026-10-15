"""Queue doubtful matches. Identical identity values already share one entity."""

from __future__ import annotations

import re
import unicodedata
import uuid

import psycopg

from services.text import stable

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
    created = 0
    created += _queue_type(conn, "Customer", "company_name")
    created += _queue_type(conn, "Product", "name")
    created += _queue_type(conn, "Supplier", "name")
    return created


def confirm_review(conn: psycopg.Connection, review_id: str) -> dict:
    review = _lock_review(conn, review_id)
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
    conn.execute("UPDATE app.identity_reviews SET status = 'confirmed' WHERE id = %s", (review_id,))
    return {"id": review_id, "status": "confirmed"}


def reject_review(conn: psycopg.Connection, review_id: str) -> dict:
    _lock_review(conn, review_id)
    conn.execute("UPDATE app.identity_reviews SET status = 'rejected' WHERE id = %s", (review_id,))
    return {"id": review_id, "status": "rejected"}


def list_reviews(conn: psycopg.Connection) -> list[dict]:
    return list(
        conn.execute(
            """
            SELECT id::text, entity_type, left_key, right_key, score, reason, status
            FROM app.identity_reviews
            ORDER BY created_at DESC
            """
        ).fetchall()
    )


def _queue_type(conn: psycopg.Connection, entity_type: str, property_name: str) -> int:
    rows = conn.execute(
        """
        SELECT e.business_key, f.value
        FROM app.entities e
        JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL
        WHERE e.type = %s AND e.valid_to IS NULL AND f.property = %s
        """,
        (entity_type, property_name),
    ).fetchall()
    named = [(row["business_key"], normalize_name(str(stable(row["value"])))) for row in rows]
    created = 0
    for index, (left_key, left_name) in enumerate(named):
        for right_key, right_name in named[index + 1 :]:
            score = name_score(left_name, right_name)
            if score < 0.7 or left_key == right_key:
                continue
            inserted = conn.execute(
                """
                INSERT INTO app.identity_reviews (
                  id, entity_type, left_key, right_key, score, reason, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'open')
                ON CONFLICT (entity_type, left_key, right_key) DO NOTHING
                RETURNING id
                """,
                (
                    str(uuid.uuid4()),
                    entity_type,
                    left_key,
                    right_key,
                    score,
                    f"Nomi vicini senza identità comune: {left_name} / {right_name}",
                ),
            ).fetchone()
            if inserted:
                created += 1
    return created


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
        from services.errors import AppError

        raise AppError(404, "Revisione identità assente")
    return row
