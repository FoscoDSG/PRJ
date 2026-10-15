"""Read-only SQL connector. Identifiers are quoted; statements other than SELECT are rejected."""

from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg import sql

from services.ingest.base import rows_from_dicts
from services.text import digest, stable

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*")
ALLOWED_SCHEMAS = {"src"}


def _validated(table: str) -> tuple[str, str]:
    if not _IDENT.fullmatch(table):
        raise ValueError(f"Tabella non valida: {table}")
    schema, name = table.split(".")
    if schema not in ALLOWED_SCHEMAS:
        raise ValueError(f"Tabella non valida: {table}")
    return schema, name


def _table(table: str) -> sql.Composed:
    schema, name = _validated(table)
    return sql.SQL(".").join(sql.Identifier(part) for part in (schema, name))


def discover_sql(conn: psycopg.Connection, table: str) -> list[dict[str, str]]:
    schema, name = _validated(table)
    rows = conn.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, name),
    ).fetchall()
    if not rows:
        raise ValueError(f"Tabella assente: {table}")
    return [{"name": row["column_name"], "data_type": row["data_type"]} for row in rows]


def extract_sql(conn: psycopg.Connection, table: str) -> dict[str, Any]:
    statement = sql.SQL("SELECT * FROM {}").format(_table(table))
    cursor = conn.execute(statement)
    if cursor.description is None:
        raise ValueError("Il connector SQL accetta solo SELECT")
    columns_raw = [item.name for item in cursor.description]
    records = []
    for row in cursor.fetchall():
        records.append({column: stable(row[column]) for column in columns_raw})
    columns, rows = rows_from_dicts(records)
    return {
        "columns": columns,
        "rows": rows,
        "watermark": digest({"columns": columns, "rows": rows}),
        "source_kind": "sql",
        "discovered": discover_sql(conn, table),
    }
