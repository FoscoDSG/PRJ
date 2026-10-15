"""PostgreSQL connection, schema bootstrap, and demo users."""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "db" / "schema.sql"

ROLES = ("admin", "mapper", "operator", "approver")


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://ubuntu@/ieol")


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$", 2)
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        )
        return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url(), row_factory=dict_row, autocommit=False)


def apply_schema(conn: psycopg.Connection, *, seed_demo: bool = False) -> None:
    conn.execute("DROP SCHEMA IF EXISTS app CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS src CASCADE")
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    if seed_demo:
        seed_users(conn)
    conn.commit()


def seed_users(conn: psycopg.Connection) -> None:
    for role in ROLES:
        conn.execute(
            """
            INSERT INTO app.users (username, password_hash, role)
            VALUES (%s, %s, %s)
            ON CONFLICT (username) DO NOTHING
            """,
            (role, password_hash(role), role),
        )


def ensure_ready(conn: psycopg.Connection, packages: list) -> None:
    row = conn.execute("SELECT to_regclass('app.users') AS rel").fetchone()
    if row is None or row["rel"] is None:
        apply_schema(conn)
    for package in packages:
        found = conn.execute(
            """
            SELECT 1 FROM app.ontology_versions
            WHERE package = %s AND version = %s
            """,
            (package.name, package.version),
        ).fetchone()
        if found is None:
            conn.execute(
                """
                INSERT INTO app.ontology_versions (id, package, version, changelog)
                VALUES (%s, %s, %s, %s)
                """,
                (str(uuid.uuid4()), package.name, package.version, package.changelog),
            )
    conn.commit()


def authenticate(conn: psycopg.Connection, username: str, password: str) -> dict | None:
    row = conn.execute(
        "SELECT username, role, password_hash FROM app.users WHERE username = %s",
        (username,),
    ).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    return {"username": row["username"], "role": row["role"]}
