"""Companies are created and users are assigned only from the admin CLI.

There is no API and no page for this. Each successful operation is an
audit row in the same transaction. A user's company is written once,
from NULL to a value. A later change is a new user. ``is_demo`` marks
the demo company: the name is never the signal.
"""

from __future__ import annotations

import uuid

import psycopg

from services.audit import record

NO_COMPANY_UPLOAD = (
    "Il tuo utente non è ancora collegato a un'azienda: chiedi all'amministratore"
)
_MAX_NAME = 200


def create_company(conn: psycopg.Connection, name: str, *, demo: bool) -> dict:
    """Insert one company. ``demo`` is the flag, not a guess from the name."""
    cleaned = _clean_name(name)
    company_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO app.companies (id, name, is_demo) VALUES (%s, %s, %s)",
        (company_id, cleaned, bool(demo)),
    )
    actor = _actor(conn)
    record(
        conn,
        action="company_create",
        actor=actor,
        role="admin",
        entity_ref=company_id,
        payload={"company_id": company_id, "name": cleaned, "is_demo": bool(demo)},
    )
    return {"company_id": company_id, "name": cleaned, "is_demo": bool(demo)}


def assign_user_company(conn: psycopg.Connection, username: str, company_id: str) -> dict:
    """Set a user's company once. A user who already has one is refused."""
    cleaned_user = (username or "").strip()
    if cleaned_user == "":
        raise RuntimeError("Utente non valido")
    company = _company(conn, company_id)
    user = conn.execute(
        """
        SELECT username, company_id::text AS company_id
        FROM app.users
        WHERE username = %s
        """,
        (cleaned_user,),
    ).fetchone()
    if user is None:
        raise RuntimeError("Utente assente")
    if user["company_id"] is not None:
        raise RuntimeError(
            "L'azienda di un utente si assegna una sola volta: per cambiarla serve un utente nuovo"
        )
    updated = conn.execute(
        """
        UPDATE app.users
        SET company_id = %s
        WHERE username = %s AND company_id IS NULL
        RETURNING username
        """,
        (company["id"], cleaned_user),
    ).fetchone()
    if updated is None:
        raise RuntimeError(
            "L'azienda di un utente si assegna una sola volta: per cambiarla serve un utente nuovo"
        )
    actor = _actor(conn)
    record(
        conn,
        action="user_company_assign",
        actor=actor,
        role="admin",
        entity_ref=f"user:{cleaned_user}",
        payload={
            "username": cleaned_user,
            "company_id": company["id"],
            "name": company["name"],
            "is_demo": company["is_demo"],
        },
    )
    return {
        "username": cleaned_user,
        "company_id": company["id"],
        "name": company["name"],
        "is_demo": company["is_demo"],
    }


def _company(conn: psycopg.Connection, company_id: str) -> dict:
    try:
        parsed = str(uuid.UUID(str(company_id).strip()))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("Identificativo azienda non valido") from exc
    row = conn.execute(
        """
        SELECT id::text AS id, name, is_demo
        FROM app.companies
        WHERE id = %s
        """,
        (parsed,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Azienda assente")
    return row


def _clean_name(name: str) -> str:
    cleaned = (name or "").strip()
    if cleaned == "" or len(cleaned) > _MAX_NAME:
        raise RuntimeError("Nome azienda non valido")
    return cleaned


def _actor(conn: psycopg.Connection) -> str:
    return conn.execute("SELECT current_user AS actor").fetchone()["actor"]
