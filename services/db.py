"""PostgreSQL connection, migrations, and users."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid

import psycopg
from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError
from psycopg.rows import dict_row

from services.migrate import require_migrated

ROLES = ("admin", "mapper", "operator", "approver")
SOURCE_RANK = {"unknown": 0, "ai": 1, "manual": 2, "crm": 3, "erp": 4, "calculated": 5}

# argon2-cffi defaults: argon2id, t=3, m=64 MiB, p=4.
_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://ubuntu@/ieol")


def password_hash(password: str) -> str:
    return _HASHER.hash(password)


def _legacy_sha256(password: str) -> str:
    return hashlib.sha256(f"ieol:{password}".encode()).hexdigest()


def _verify_scrypt(password: str, stored: str) -> bool:
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


def _is_legacy_sha256(stored: str) -> bool:
    return len(stored) == 64 and all(char in "0123456789abcdef" for char in stored)


def demo_mode() -> bool:
    return os.environ.get("IEOL_DEMO_MODE") == "1"


def _seed_password(role: str) -> str | None:
    raw = os.environ.get(f"IEOL_SEED_{role.upper()}_PASSWORD")
    if raw is None or raw == "":
        return None
    if raw.casefold() == role.casefold():
        raise RuntimeError(f"IEOL_SEED_{role.upper()}_PASSWORD must not equal the role name")
    return raw


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url(), row_factory=dict_row, autocommit=False)


def seed_users(conn: psycopg.Connection) -> None:
    for role in ROLES:
        password = _seed_password(role)
        row = conn.execute(
            "SELECT password_hash FROM app.users WHERE username = %s",
            (role,),
        ).fetchone()
        if row is None:
            if password is None:
                continue
            conn.execute(
                """
                INSERT INTO app.users (username, password_hash, role)
                VALUES (%s, %s, %s)
                """,
                (role, password_hash(password), role),
            )
            continue
        if not _password_ok(row["password_hash"], role):
            continue
        replacement = password if password is not None else secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE app.users SET password_hash = %s WHERE username = %s",
            (password_hash(replacement), role),
        )


def ensure_ready(conn: psycopg.Connection, packages: list) -> None:
    require_migrated(conn)
    seed_users(conn)
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
    if row is None or not _password_ok(row["password_hash"], password):
        return None
    stored = row["password_hash"]
    legacy = _is_legacy_sha256(stored) or stored.startswith("scrypt$")
    if legacy or (stored.startswith("$argon2") and _HASHER.check_needs_rehash(stored)):
        conn.execute(
            "UPDATE app.users SET password_hash = %s WHERE username = %s",
            (password_hash(password), row["username"]),
        )
    return {"username": row["username"], "role": row["role"]}


def _password_ok(stored: str, password: str) -> bool:
    if stored.startswith("$argon2"):
        try:
            return bool(_HASHER.verify(stored, password))
        except VerificationError:
            return False
    if stored.startswith("scrypt$"):
        return _verify_scrypt(password, stored)
    if _is_legacy_sha256(stored):
        return hmac.compare_digest(stored, _legacy_sha256(password))
    return False
