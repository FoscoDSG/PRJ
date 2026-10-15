"""Orchestrate ingest, mapping, canonical load, the order agent, and actions."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import psycopg

from ontology.loader import Ontology, load_merged
from services.actions.executor import dry_run, execute
from services.agent.order_agent import build_context, run_order_agent
from services.agent.stub_model import StubOrderModel
from services.canonical.store import _put_fact, materialize
from services.errors import AppError
from services.ingest.csv_connector import read_csv
from services.ingest.excel_connector import read_excel
from services.ingest.landing import land
from services.ingest.sql_connector import extract_sql
from services.mapping.engine import THRESHOLD
from services.rules.engine import requires_cfo
from services.text import coerce_number
from services.workflow.engine import expire_stale, mark_approved, propose

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
MODEL = StubOrderModel()

FIXTURE_FILES = {
    "anagrafica": "anagrafica.csv",
    "ordini": "ordini.csv",
    "righe": "righe.csv",
    "articoli": "articoli.csv",
    "giacenze": "giacenze.csv",
    "fornitori": "fornitori.csv",
    "ambiguo": "ambiguo.csv",
}

SRC_TABLES = {
    "anagrafica": "src.clienti",
    "ordini": "src.ordini",
    "righe": "src.righe",
    "articoli": "src.articoli",
    "giacenze": "src.giacenze",
    "fornitori": "src.fornitori",
}


def ontology() -> Ontology:
    return load_merged()


def ingest_csv(conn: psycopg.Connection, path: Path, source_name: str, actor: str, role: str) -> dict:
    _require_mapper(role)
    return land(conn, source_name=source_name, extracted=read_csv(path), actor=actor, role=role)


def ingest_excel(conn: psycopg.Connection, path: Path, source_name: str, actor: str, role: str) -> dict:
    _require_mapper(role)
    return land(conn, source_name=source_name, extracted=read_excel(path), actor=actor, role=role)


def ingest_sql(conn: psycopg.Connection, table: str, source_name: str, actor: str, role: str) -> dict:
    _require_mapper(role)
    try:
        extracted = extract_sql(conn, table)
    except ValueError as exc:
        raise AppError(400, str(exc)) from exc
    return land(conn, source_name=source_name, extracted=extracted, actor=actor, role=role)


def confirm_mapping(conn: psycopg.Connection, mapping_id: str, actor: str, role: str, model: Ontology) -> dict:
    _require_mapper(role)
    row = conn.execute(
        """
        SELECT id::text, target_type, target_property, status
        FROM app.field_mappings
        WHERE id = %s AND valid_to IS NULL
        """,
        (mapping_id,),
    ).fetchone()
    if row is None:
        raise AppError(404, "Mapping assente")
    model.require_prop(row["target_type"], row["target_property"])
    conn.execute(
        "UPDATE app.field_mappings SET status = 'confirmed' WHERE id = %s",
        (mapping_id,),
    )
    return {"id": mapping_id, "status": "confirmed"}


def reject_mapping(conn: psycopg.Connection, mapping_id: str, role: str) -> dict:
    _require_mapper(role)
    updated = conn.execute(
        """
        UPDATE app.field_mappings SET status = 'rejected'
        WHERE id = %s AND valid_to IS NULL
        RETURNING id::text
        """,
        (mapping_id,),
    ).fetchone()
    if updated is None:
        raise AppError(404, "Mapping assente")
    return {"id": mapping_id, "status": "rejected"}


def confirm_confident(conn: psycopg.Connection, role: str, model: Ontology) -> int:
    _require_mapper(role)
    rows = conn.execute(
        """
        SELECT id::text, target_type, target_property
        FROM app.field_mappings
        WHERE status = 'proposed' AND valid_to IS NULL AND confidence >= %s
        """,
        (THRESHOLD,),
    ).fetchall()
    count = 0
    for row in rows:
        model.require_prop(row["target_type"], row["target_property"])
        conn.execute(
            "UPDATE app.field_mappings SET status = 'confirmed' WHERE id = %s",
            (row["id"],),
        )
        count += 1
    return count


def refresh_canonical(conn: psycopg.Connection, model: Ontology) -> dict[str, int]:
    return materialize(conn, model)


def run_agents(conn: psycopg.Connection, *, actor: str, role: str, model: Any | None = None) -> list[str]:
    if role not in ("admin", "approver", "operator", "mapper"):
        raise AppError(403, "Accesso negato")
    provider = model or MODEL
    orders = conn.execute(
        """
        SELECT id::text FROM app.entities
        WHERE type = 'Order' AND valid_to IS NULL
        ORDER BY business_key
        """
    ).fetchall()
    created = []
    for order in orders:
        context = build_context(conn, order["id"])
        existing = conn.execute(
            """
            SELECT 1 FROM app.recommendations
            WHERE order_entity_id = %s
              AND status <> 'expired'
              AND evidence_hash = %s
            """,
            (order["id"], context["evidence_hash"]),
        ).fetchone()
        if existing:
            continue
        result = run_order_agent(conn, order["id"], provider)
        if not result["actions"]:
            continue
        recommendation_id = propose(
            conn,
            order_id=order["id"],
            problem=result["problem"],
            evidence=result["evidence"],
            evidence_hash=result["evidence_hash"],
            actions=result["actions"],
            expected_impact=result["expected_impact"],
            risk=result["risk"],
            confidence=result["confidence"],
            requires_cfo=requires_cfo(result["amount"]),
            actor=actor,
            role=role,
        )
        created.append(recommendation_id)
    return created


def approve(conn: psycopg.Connection, recommendation_id: str, actor: str, role: str) -> dict:
    mark_approved(conn, recommendation_id, actor=actor, role=role)
    payloads = dry_run(conn, recommendation_id, actor=actor, role=role)
    return {"id": recommendation_id, "status": "dry_run_ok", "dry_run": payloads}


def execute_recommendation(
    conn: psycopg.Connection, recommendation_id: str, actor: str, role: str, model: Ontology
) -> dict:
    if role not in ("admin", "approver"):
        raise AppError(403, "Solo un approvatore esegue azioni")
    return execute(conn, model, recommendation_id, actor=actor, role=role)


def load_fixtures(conn: psycopg.Connection, actor: str, role: str, model: Ontology) -> dict[str, Any]:
    _require_mapper(role)
    loaded = []
    for source_name, filename in FIXTURE_FILES.items():
        result = ingest_csv(conn, FIXTURES / filename, source_name, actor, role)
        loaded.append({"source": source_name, **result})
    seed_src(conn)
    confirmed = confirm_confident(conn, role, model)
    counts = refresh_canonical(conn, model)
    recommendations = run_agents(conn, actor=actor, role=role)
    return {
        "sources": loaded,
        "confirmed_mappings": confirmed,
        "canonical": counts,
        "recommendations": recommendations,
    }


def seed_src(conn: psycopg.Connection) -> None:
    for source_name, table in SRC_TABLES.items():
        conn.execute(f"TRUNCATE {table}")
        path = FIXTURES / FIXTURE_FILES[source_name]
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            if not rows:
                continue
            columns = reader.fieldnames or []
            quoted = ", ".join(columns)
            placeholders = ", ".join(["%s"] * len(columns))
            for row in rows:
                conn.execute(
                    f"INSERT INTO {table} ({quoted}) VALUES ({placeholders})",
                    [row[column] or None for column in columns],
                )


def adjust_on_hand(conn: psycopg.Connection, model: Ontology, product_key: str, on_hand: int) -> list[str]:
    import json

    row = conn.execute(
        """
        SELECT e.id::text
        FROM app.entities e
        JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL
        WHERE e.type = 'StockPosition'
          AND f.property = 'product_source_key' AND f.value = %s::jsonb
        LIMIT 1
        """,
        (json.dumps(product_key),),
    ).fetchone()
    if row is None:
        raise AppError(404, "Giacenza assente")
    prop = model.require_prop("StockPosition", "on_hand")
    _put_fact(
        conn,
        entity_id=row["id"],
        name="on_hand",
        value=coerce_number(on_hand),
        classification=prop.classification,
        origin="original",
        source="adjustment",
        confidence=1,
    )
    materialize(conn, model)
    return expire_stale(conn)


def _require_mapper(role: str) -> None:
    if role not in ("admin", "mapper"):
        raise AppError(403, "Serve il ruolo mapper")


def acknowledge_drift(conn: psycopg.Connection, drift_id: str, role: str) -> dict:
    _require_mapper(role)
    row = conn.execute(
        """
        UPDATE app.drift_reviews SET status = 'acknowledged'
        WHERE id = %s RETURNING id::text
        """,
        (drift_id,),
    ).fetchone()
    if row is None:
        raise AppError(404, "Drift assente")
    return {"id": drift_id, "status": "acknowledged"}
