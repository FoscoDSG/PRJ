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
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from services.migrate import require_migrated

ROLES = ("admin", "mapper", "operator", "approver")
# Same vocabulary as SOURCE_CLASSES. Higher rank wins an automatic write.
SOURCE_RANK = {
    "unknown": 0,
    "external": 1,
    "manual": 2,
    "operational": 3,
    "system_of_record": 4,
}
DEMO_DATABASE_NAME = "ieol_demo"

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
    """Owner connection. Migrations and password reset use this."""
    return psycopg.connect(url or database_url(), row_factory=dict_row, autocommit=False)


def app_database_url(dbname: str | None = None) -> str:
    """URL of the least-privilege login. The user in the URL is ieol_app."""
    raw = os.environ.get("IEOL_APP_DATABASE_URL", "").strip()
    if raw == "":
        raise RuntimeError(
            "IEOL_APP_DATABASE_URL assente. L'API entra come ieol_app. "
            "DATABASE_URL resta l'owner, solo per ieol migrate e ieol reset-password."
        )
    if dbname is None:
        return raw
    info = conninfo_to_dict(raw)
    info["dbname"] = dbname
    return make_conninfo(**info)


def app_connect(url: str | None = None) -> psycopg.Connection:
    """Application connection. Logs in as ieol_app. Does not change role."""
    conn = psycopg.connect(url or app_database_url(), row_factory=dict_row, autocommit=False)
    try:
        current = conn.execute(
            "SELECT session_user AS session_user, current_user AS current_user"
        ).fetchone()
        if (
            current is None
            or current["session_user"] != "ieol_app"
            or current["current_user"] != "ieol_app"
        ):
            raise RuntimeError(
                "La connessione applicativa deve autenticarsi come ieol_app. "
                "Controlla IEOL_APP_DATABASE_URL."
            )
    except Exception:
        conn.close()
        raise
    return conn


def assert_demo_database(conn: psycopg.Connection) -> None:
    """Refuse demo loads on any database other than ``ieol_demo``."""
    from services.errors import AppError

    row = conn.execute("SELECT current_database() AS name").fetchone()
    name = None if row is None else row["name"]
    if name != DEMO_DATABASE_NAME:
        raise AppError(409, "Il demo si carica solo sul database ieol_demo")


def reset_password(conn: psycopg.Connection, role: str) -> None:
    """Replace one role password with the hash of ``IEOL_SEED_<ROLE>_PASSWORD``."""
    if role not in ROLES:
        raise RuntimeError("Ruolo sconosciuto")
    password = _seed_password(role)
    if password is None:
        raise RuntimeError(f"IEOL_SEED_{role.upper()}_PASSWORD assente")
    updated = conn.execute(
        """
        UPDATE app.users
        SET password_hash = %s
        WHERE username = %s AND role = %s
        RETURNING username
        """,
        (password_hash(password), role, role),
    ).fetchone()
    if updated is None:
        raise RuntimeError(f"Utente {role} assente")
    conn.commit()


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
