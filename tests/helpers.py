"""Helpers that reset only the pytest database."""

from __future__ import annotations

import os

import psycopg

from services.migrate import database_name


def wipe_test_database(conn: psycopg.Connection) -> None:
    """Drop application schemas on the test database and commit.

    Refuses any database whose name is not the current ``DATABASE_URL``
    and does not end with ``_test``.
    """
    name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
    expected = database_name(os.environ["DATABASE_URL"])
    if name != expected or not name.endswith("_test"):
        raise RuntimeError(f"Reset di test rifiutato sul database {name}")
    conn.execute("DROP SCHEMA IF EXISTS app CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS src CASCADE")
    conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    conn.commit()
