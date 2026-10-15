"""Declared provenance for a connector. The class is not inferred from the name."""

from __future__ import annotations

import psycopg

from services.errors import AppError

SOURCE_CLASSES = ("system_of_record", "operational", "external", "manual", "unknown")


def register_source(conn: psycopg.Connection, name: str, source_class: str) -> None:
    cleaned = (name or "").strip()
    if cleaned == "" or len(cleaned) > 128:
        raise AppError(422, "Nome fonte non valido")
    if source_class not in SOURCE_CLASSES:
        raise AppError(422, "Classe di provenienza non valida")
    current = conn.execute(
        "SELECT source_class FROM app.sources WHERE name = %s",
        (cleaned,),
    ).fetchone()
    if current is None:
        conn.execute(
            "INSERT INTO app.sources (name, source_class) VALUES (%s, %s)",
            (cleaned, source_class),
        )
        return
    if current["source_class"] != source_class:
        raise AppError(409, "La fonte è già registrata con un'altra provenienza")
