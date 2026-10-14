"""Map source columns onto the ontology. Ambiguous columns stay below the review threshold."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.types.json import Json

from services.audit import record
from services.text import digest

THRESHOLD = 0.75

# profile -> normalized column -> (target type, property, confidence)
PROFILES: dict[str, dict[str, tuple[str, str, float]]] = {
    "customer": {
        "anag_cli": ("Customer", "source_key", 0.96),
        "anag_clienti": ("Customer", "source_key", 0.98),
        "ragione_sociale": ("Customer", "company_name", 0.95),
        "piva": ("Customer", "vat", 0.97),
        "email": ("Customer", "email", 0.90),
    },
    "order": {
        "ord_head": ("Order", "source_key", 0.98),
        "anag_cli": ("Order", "customer_source_key", 0.94),
        "importo": ("Order", "amount", 0.93),
        "stato": ("Order", "status", 0.86),
        "data": ("Order", "ordered_at", 0.84),
    },
    "order_line": {
        "ord_head": ("OrderLine", "order_source_key", 0.95),
        "cod_art": ("OrderLine", "product_source_key", 0.99),
        "qty": ("OrderLine", "quantity", 0.92),
        "quantita": ("OrderLine", "quantity", 0.93),
        "prezzo": ("OrderLine", "unit_price", 0.90),
    },
    "product": {
        "cod_art": ("Product", "source_key", 0.99),
        "descrizione": ("Product", "name", 0.90),
        "costo": ("Product", "cost", 0.92),
    },
    "stock": {
        "cod_art": ("StockPosition", "product_source_key", 0.95),
        "magazzino": ("StockPosition", "warehouse_source_key", 0.90),
        "giacenza": ("StockPosition", "on_hand", 0.94),
        "impegnato": ("StockPosition", "reserved", 0.90),
    },
    "supplier": {
        "forn": ("Supplier", "source_key", 0.93),
        "nome": ("Supplier", "name", 0.84),
        "cod_art": ("Supplier", "product_source_key", 0.90),
        "ritardo_giorni": ("Supplier", "delay_days", 0.92),
    },
}

SIGNALS: dict[str, set[str]] = {
    "supplier": {"ritardo_giorni", "forn"},
    "stock": {"giacenza"},
    "order_line": {"qty", "quantita"},
    "order": {"importo", "ord_head"},
    "customer": {"ragione_sociale", "piva"},
    "product": {"descrizione", "costo"},
}


def detect_profile(columns: list[str]) -> str:
    present = set(columns)
    for name in ("supplier", "stock", "order_line", "order", "customer", "product"):
        if SIGNALS[name] & present:
            if name == "order" and "importo" not in present:
                continue
            if name == "supplier" and "ritardo_giorni" not in present and "forn" not in present:
                continue
            return name
    return "unknown"


def _samples(rows: list[dict[str, Any]], column: str) -> list[Any]:
    values = []
    for row in rows:
        value = row.get(column)
        if value not in (None, ""):
            values.append(value)
        if len(values) == 3:
            break
    return values


def propose_mappings(
    conn: psycopg.Connection,
    *,
    source_name: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    actor: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    profile = detect_profile(columns)
    column_hash = digest(columns)
    previous = conn.execute(
        """
        SELECT column_hash, columns
        FROM app.schema_snapshots
        WHERE source_name = %s
        ORDER BY captured_at DESC
        LIMIT 1
        """,
        (source_name,),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO app.schema_snapshots (id, source_name, column_hash, columns)
        VALUES (%s, %s, %s, %s)
        """,
        (str(uuid.uuid4()), source_name, column_hash, Json(columns)),
    )
    drift = None
    if previous and previous["column_hash"] != column_hash:
        old_columns = list(previous["columns"])
        diff = {
            "added": [column for column in columns if column not in old_columns],
            "removed": [column for column in old_columns if column not in columns],
        }
        drift_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO app.drift_reviews (id, source_name, previous_hash, new_hash, diff, status)
            VALUES (%s, %s, %s, %s, %s, 'open')
            """,
            (drift_id, source_name, previous["column_hash"], column_hash, Json(diff)),
        )
        drift = {"id": drift_id, "diff": diff}
        for removed in diff["removed"]:
            conn.execute(
                """
                UPDATE app.field_mappings
                SET valid_to = now()
                WHERE source_name = %s AND source_field = %s AND valid_to IS NULL
                """,
                (source_name, removed),
            )
    known = PROFILES.get(profile, {})
    created = []
    for column in columns:
        existing = conn.execute(
            """
            SELECT id FROM app.field_mappings
            WHERE source_name = %s AND source_field = %s AND valid_to IS NULL
            """,
            (source_name, column),
        ).fetchone()
        if existing:
            continue
        if column in known:
            target_type, target_property, confidence = known[column]
        else:
            target_type, target_property, confidence = ("Document", "title", 0.42)
        mapping_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO app.field_mappings (
              id, source_name, source_field, target_type, target_property,
              confidence, status, samples, profile
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'proposed', %s, %s)
            """,
            (
                mapping_id,
                source_name,
                column,
                target_type,
                target_property,
                confidence,
                Json(_samples(rows, column)),
                profile,
            ),
        )
        created.append(
            {
                "id": mapping_id,
                "source_field": column,
                "target_type": target_type,
                "target_property": target_property,
                "confidence": confidence,
                "status": "proposed",
            }
        )
    if drift is not None:
        record(
            conn,
            action="proposal",
            actor=actor,
            role=role,
            entity_ref=source_name,
            payload={"kind": "schema_drift", "diff": drift["diff"]},
        )
    return {"profile": profile, "mappings": created, "drift": drift, "column_hash": column_hash}
