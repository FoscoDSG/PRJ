"""Land a connector extract once per watermark, then propose mappings."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.types.json import Json

from services.errors import AppError
from services.mapping.engine import classify_table, propose_mappings
from services.pii import stage_landing

MAX_LANDING_ROWS = 5000


def land(
    conn: psycopg.Connection,
    *,
    source_name: str,
    extracted: dict[str, Any],
    actor: str | None = None,
    role: str | None = None,
    company_id: str | None = None,
) -> dict[str, Any]:
    watermark = extracted["watermark"]
    if len(extracted["rows"]) > MAX_LANDING_ROWS:
        raise AppError(413, "Troppe righe nel file")
    existing = conn.execute(
        """
        SELECT id, row_count FROM app.landing_batches
        WHERE source_name = %s AND watermark = %s
          AND company_id IS NOT DISTINCT FROM %s::uuid
        """,
        (source_name, watermark, company_id),
    ).fetchone()
    if existing:
        return {
            "batch_id": str(existing["id"]),
            "skipped": True,
            "row_count": existing["row_count"],
            "mappings": [],
            "drift": None,
        }
    batch_id = str(uuid.uuid4())
    table = classify_table(extracted["columns"], extracted["rows"])
    stage_landing(batch_id, list(extracted["rows"]))
    conn.execute(
        """
        INSERT INTO app.landing_batches (
          id, source_name, watermark, row_count, proposed_type, proposed_score,
          proposed_rule, company_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            batch_id,
            source_name,
            watermark,
            len(extracted["rows"]),
            table.profile,
            table.score,
            table.rule,
            company_id,
        ),
    )
    for index, row in enumerate(extracted["rows"]):
        conn.execute(
            """
            INSERT INTO app.landing_rows (batch_id, row_index, data)
            VALUES (%s, %s, %s)
            """,
            (batch_id, index, Json(row)),
        )
    conn.execute(
        """
        INSERT INTO app.ingest_watermarks (source_name, watermark, company_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (company_id, source_name) DO UPDATE
        SET watermark = EXCLUDED.watermark, updated_at = now()
        """,
        (source_name, watermark, company_id),
    )
    proposed = propose_mappings(
        conn,
        source_name=source_name,
        columns=extracted["columns"],
        rows=extracted["rows"],
        actor=actor,
        role=role,
        company_id=company_id,
        profile=table.profile,
        batch_id=batch_id,
    )
    return {
        "batch_id": batch_id,
        "skipped": False,
        "row_count": len(extracted["rows"]),
        **proposed,
    }
