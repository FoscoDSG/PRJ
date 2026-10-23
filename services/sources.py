"""Declared provenance for a connector. The class is not inferred from the name."""

from __future__ import annotations

import psycopg

from services.errors import AppError

# Strongest first. A calculated fact takes the weakest class among its inputs.
SOURCE_CLASSES = ("system_of_record", "operational", "external", "manual", "unknown")


def weakest_source_class(classes: list[str]) -> str:
    """Return the weakest declared class. No inputs, or any unknown input, is unknown."""
    if not classes:
        return "unknown"
    rank = {name: index for index, name in enumerate(SOURCE_CLASSES)}
    weakest = "unknown"
    weakest_rank = -1
    for name in classes:
        key = name if name in rank else "unknown"
        if rank[key] > weakest_rank:
            weakest = key
            weakest_rank = rank[key]
    return weakest


def register_source(
    conn: psycopg.Connection,
    name: str,
    source_class: str,
    company_id: str | None = None,
) -> None:
    """Register the source for this company. Does not update an existing row.

    A legacy row with no company stays without one. The same name under
    another company is a separate source. The company is the one of the
    user doing the upload, passed by the server, not by the client.
    """
    cleaned = (name or "").strip()
    if cleaned == "" or len(cleaned) > 128:
        raise AppError(422, "Nome fonte non valido")
    if source_class not in SOURCE_CLASSES:
        raise AppError(422, "Classe di provenienza non valida")
    current = conn.execute(
        """
        SELECT source_class FROM app.sources
        WHERE name = %s AND company_id IS NOT DISTINCT FROM %s::uuid
        """,
        (cleaned, company_id),
    ).fetchone()
    if current is None:
        conn.execute(
            "INSERT INTO app.sources (name, source_class, company_id) VALUES (%s, %s, %s)",
            (cleaned, source_class, company_id),
        )
        return
    if current["source_class"] != source_class:
        raise AppError(409, "La fonte è già registrata con un'altra provenienza")
