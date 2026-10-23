"""Orchestrate ingest, mapping, canonical load, the order agent, and actions."""

from __future__ import annotations

import csv
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Json

from ontology.loader import Ontology, OntologyError, load_merged
from services.actions.executor import dry_run, execute
from services.agent.baseline import BASELINE_CONFIDENCE_RULE
from services.agent.order_agent import build_context, run_order_agent
from services.agent.stub_model import StubOrderModel
from services.canonical.store import _put_fact, materialize
from services.audit import record
from services.companies import USER_WITHOUT_COMPANY
from services.errors import AppError
from services.quality import document_reasons
from services.ingest.csv_connector import read_csv
from services.ingest.document_connector import read_order_document
from services.ingest.excel_connector import read_excel
from services.ingest.landing import land
from services.ingest.sql_connector import extract_sql
from services.mapping.engine import THRESHOLD, assigned_mapping, rule_id_for
from services.revocation import ALREADY_REVOKED, QUEUE_MESSAGE, clean_reason
from services.mapping.labels import PROFILE_LABELS
from services.rules.engine import requires_cfo
from services.text import coerce_number
from services.sources import register_source
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

FIXTURE_SOURCE_CLASS = {
    "anagrafica": "system_of_record",
    "ordini": "system_of_record",
    "righe": "system_of_record",
    "articoli": "system_of_record",
    "giacenze": "system_of_record",
    "fornitori": "system_of_record",
    "ambiguo": "external",
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


def ingest_csv(
    conn: psycopg.Connection,
    path: Path,
    source_name: str,
    actor: str,
    role: str,
    source_class: str,
    company_id: str | None = None,
) -> dict:
    _require_mapper(role)
    register_source(conn, source_name, source_class, company_id=company_id)
    return land(
        conn,
        source_name=source_name,
        extracted=read_csv(path),
        actor=actor,
        role=role,
        company_id=company_id,
    )


def ingest_excel(
    conn: psycopg.Connection,
    path: Path,
    source_name: str,
    actor: str,
    role: str,
    source_class: str,
    company_id: str | None = None,
) -> dict:
    _require_mapper(role)
    register_source(conn, source_name, source_class, company_id=company_id)
    return land(
        conn,
        source_name=source_name,
        extracted=read_excel(path),
        actor=actor,
        role=role,
        company_id=company_id,
    )


def ingest_document(
    conn: psycopg.Connection,
    path: Path,
    actor: str,
    role: str,
    model: Ontology,
    company_id: str | None = None,
) -> dict:
    del model
    _require_mapper(role)
    try:
        document = read_order_document(path)
    except ValueError as exc:
        raise AppError(400, str(exc)) from exc
    reasons = document_reasons(document["customer"], document["lines"], conn)
    landed = []
    for source_name, extracted in document["sources"]:
        register_source(conn, source_name, "external", company_id=company_id)
        landed.append({
            "source": source_name,
            **land(
                conn,
                source_name=source_name,
                extracted=extracted,
                actor=actor,
                role=role,
                company_id=company_id,
            ),
        })
        conn.execute(
            """
            UPDATE app.field_mappings
            SET confidence = 0.55
            WHERE source_name = %s AND valid_to IS NULL AND status = 'proposed'
              AND company_id IS NOT DISTINCT FROM %s::uuid
            """,
            (source_name, company_id),
        )
    quarantine_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.quarantine_documents (
          id, source_name, order_key, reasons, status, company_id
        )
        VALUES (%s, %s, %s, %s, 'open', %s)
        """,
        (quarantine_id, document["stem"], document["order_key"], Json(reasons), company_id),
    )
    return {
        "id": quarantine_id,
        "order_key": document["order_key"],
        "source_kind": document["source_kind"],
        "sources": landed,
        "reasons": reasons,
        "status": "open",
    }


def release_document(
    conn: psycopg.Connection,
    quarantine_id: str,
    role: str,
    model: Ontology,
    *,
    actor: str,
    company_id: str | None,
) -> dict:
    _require_mapper(role)
    if not company_id:
        raise AppError(403, USER_WITHOUT_COMPANY)
    row = _lock_quarantine(conn, quarantine_id, company_id)
    if row["reasons"]:
        raise AppError(409, "Il documento ha controlli aperti: " + "; ".join(row["reasons"]))
    if row["status"] != "open":
        raise AppError(409, "Il documento non è in quarantena")
    conn.execute(
        """
        UPDATE app.field_mappings
        SET status = 'confirmed'
        WHERE source_name LIKE %s AND status = 'proposed' AND valid_to IS NULL
          AND company_id IS NOT DISTINCT FROM %s::uuid
        """,
        (row["source_name"] + "%", company_id),
    )
    counts = refresh_canonical(conn, model)
    recommendations = run_agents(conn, actor=actor, role=role)
    conn.execute(
        """
        UPDATE app.quarantine_documents
        SET status = 'released', company_id = %s, actor = %s, decided_at = now()
        WHERE id = %s
        """,
        (company_id, actor, quarantine_id),
    )
    return {"id": quarantine_id, "status": "released", "canonical": counts, "recommendations": recommendations}


def reject_document(
    conn: psycopg.Connection,
    quarantine_id: str,
    role: str,
    *,
    actor: str,
    company_id: str | None,
    reason: str | None = None,
) -> dict:
    """Refuse a quarantined document. Another company's document is absent.

    The motive is required only once this company's open document is in
    hand. A missing document stays a 404, and a short motive is a 422.
    Spaces do not count toward the five characters.
    """
    _require_mapper(role)
    if not company_id:
        raise AppError(403, USER_WITHOUT_COMPANY)
    row = _lock_quarantine(conn, quarantine_id, company_id)
    if row["status"] != "open":
        raise AppError(409, "Il documento non è in quarantena")
    motive = _reject_reason(reason)
    conn.execute(
        """
        UPDATE app.quarantine_documents
        SET status = 'rejected', company_id = %s, actor = %s, reason = %s, decided_at = now()
        WHERE id = %s
        """,
        (company_id, actor, motive, quarantine_id),
    )
    return {"id": quarantine_id, "status": "rejected", "reason": motive}


def _reject_reason(reason: str | None) -> str:
    cleaned = (reason or "").strip()
    if len(cleaned) < 5 or len(cleaned) > 500:
        raise AppError(422, "Scrivi il motivo del rifiuto (almeno 5 caratteri).")
    return cleaned


def _lock_quarantine(conn: psycopg.Connection, quarantine_id: str, company_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM app.quarantine_documents WHERE id = %s FOR UPDATE",
        (quarantine_id,),
    ).fetchone()
    stored = None if row is None or row["company_id"] is None else str(row["company_id"])
    if row is None or (stored is not None and stored != company_id):
        raise AppError(404, "Documento in quarantena assente")
    return row


def ingest_sql(
    conn: psycopg.Connection,
    table: str,
    source_name: str,
    actor: str,
    role: str,
    source_class: str,
    company_id: str | None = None,
) -> dict:
    _require_mapper(role)
    register_source(conn, source_name, source_class, company_id=company_id)
    try:
        extracted = extract_sql(conn, table)
    except ValueError as exc:
        raise AppError(400, str(exc)) from exc
    return land(
        conn,
        source_name=source_name,
        extracted=extracted,
        actor=actor,
        role=role,
        company_id=company_id,
    )


def _mapping_row(conn: psycopg.Connection, mapping_id: str) -> dict:
    row = conn.execute(
        """
        SELECT id::text, source_name, source_field, target_type, target_property,
               confidence, profile, status, company_id::text AS company_id,
               origin, source_decision_id::text AS source_decision_id,
               batch_id::text AS batch_id
        FROM app.field_mappings
        WHERE id = %s AND valid_to IS NULL
        """,
        (mapping_id,),
    ).fetchone()
    if row is None:
        raise AppError(404, "Mapping assente")
    return row


def _corrected_property(model: Ontology, target_type: str, corrected: str | None) -> str | None:
    if corrected is None or str(corrected).strip() == "":
        return None
    value = str(corrected).strip()
    try:
        model.require_prop(target_type, value)
    except OntologyError as exc:
        raise AppError(422, str(exc)) from exc
    return value


def _suggestion_origin(row: dict) -> tuple[str, str | None]:
    origin = row.get("origin") or "rule"
    source_decision_id = row.get("source_decision_id")
    if origin != "learned" or not source_decision_id:
        return "rule", None
    return "learned", source_decision_id


def _record_mapping_decision(
    conn: psycopg.Connection,
    row: dict,
    *,
    decision: str,
    method: str,
    actor: str,
    role: str,
    corrected: str | None,
    company_id: str | None,
) -> str:
    decision_id = str(uuid.uuid4())
    origin, source_decision_id = _suggestion_origin(row)
    conn.execute(
        """
        INSERT INTO app.mapping_decisions (
          id, mapping_id, decision, confidence_method, actor, actor_role,
          source_name, source_field, target_property, target_property_corrected,
          score, confidence_rule, company_id, origin, source_decision_id, batch_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            decision_id,
            row["id"],
            decision,
            method,
            actor,
            role,
            row["source_name"],
            row["source_field"],
            row["target_property"],
            corrected,
            row["confidence"],
            rule_id_for(row["profile"], row["source_field"]),
            company_id,
            origin,
            source_decision_id,
            row.get("batch_id"),
        ),
    )
    return decision_id


def confirm_mapping(
    conn: psycopg.Connection,
    mapping_id: str,
    actor: str,
    role: str,
    model: Ontology,
    *,
    target_property_corrected: str | None = None,
    company_id: str | None = None,
) -> dict:
    _require_mapper(role)
    if target_property_corrected is not None and str(target_property_corrected).strip() != "":
        raise AppError(422, "La proprietà corretta si registra solo su un rifiuto")
    row = _mapping_row(conn, mapping_id)
    _require_visible_company(row.get("company_id"), company_id)
    model.require_prop(row["target_type"], row["target_property"])
    corrected = None
    conn.execute(
        "UPDATE app.field_mappings SET status = 'confirmed' WHERE id = %s",
        (mapping_id,),
    )
    decision_id = _record_mapping_decision(
        conn,
        row,
        decision="confirm",
        method="human",
        actor=actor,
        role=role,
        corrected=corrected,
        company_id=company_id,
    )
    return {"id": mapping_id, "status": "confirmed", "decision_id": decision_id}


def reject_mapping(
    conn: psycopg.Connection,
    mapping_id: str,
    role: str,
    *,
    actor: str,
    model: Ontology,
    target_property_corrected: str | None = None,
    company_id: str | None = None,
) -> dict:
    _require_mapper(role)
    row = _mapping_row(conn, mapping_id)
    _require_visible_company(row.get("company_id"), company_id)
    corrected = _corrected_property(model, row["target_type"], target_property_corrected)
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
    decision_id = _record_mapping_decision(
        conn,
        row,
        decision="reject",
        method="human",
        actor=actor,
        role=role,
        corrected=corrected,
        company_id=company_id,
    )
    return {"id": mapping_id, "status": "rejected", "decision_id": decision_id}


def confirm_confident(
    conn: psycopg.Connection,
    role: str,
    model: Ontology,
    *,
    actor: str,
    company_id: str | None = None,
) -> int:
    """Confirm strong rule suggestions. Learned rows stay proposed.

    Strong is the uncalibrated fallback 0.75, not a measured guarantee.
    """
    _require_mapper(role)
    rows = conn.execute(
        """
        SELECT id::text, source_name, source_field, target_type, target_property,
               confidence, profile, status, company_id::text AS company_id,
               origin, source_decision_id::text AS source_decision_id,
               batch_id::text AS batch_id
        FROM app.field_mappings
        WHERE status = 'proposed'
          AND valid_to IS NULL
          AND confidence >= %s
          AND (origin IS NULL OR origin = 'rule')
          AND (
            company_id IS NULL
            OR (%s::uuid IS NOT NULL AND company_id = %s::uuid)
          )
        """,
        (THRESHOLD, company_id, company_id),
    ).fetchall()
    count = 0
    for row in rows:
        model.require_prop(row["target_type"], row["target_property"])
        conn.execute(
            "UPDATE app.field_mappings SET status = 'confirmed' WHERE id = %s",
            (row["id"],),
        )
        # A decision row needs a company. The internal fixture load, which
        # has none, still confirms the mapping and does not write the row.
        if company_id:
            _record_mapping_decision(
                conn,
                row,
                decision="confirm",
                method="bulk_rule",
                actor=actor,
                role=role,
                corrected=None,
                company_id=company_id,
            )
        count += 1
    return count


def revoke_correction(
    conn: psycopg.Connection,
    decision_id: str,
    *,
    actor: str,
    role: str,
    company_id: str | None,
    reason: str | None = None,
) -> dict:
    """Append revoked. The revoked row stays. Same company as the actor.

    A learned correction still drops back to the rule on open suggestions.
    The decided mapping itself does not return to the queue.
    """
    _require_mapper(role)
    if not company_id:
        raise AppError(403, USER_WITHOUT_COMPANY)
    motive = clean_reason(reason)
    row = conn.execute(
        """
        SELECT id::text, mapping_id::text, decision, source_name, source_field,
               target_property, target_property_corrected, score, confidence_rule,
               company_id::text AS company_id, origin,
               source_decision_id::text AS source_decision_id,
               batch_id::text AS batch_id
        FROM app.mapping_decisions
        WHERE id = %s::uuid
          AND company_id = %s::uuid
        """,
        (decision_id, company_id),
    ).fetchone()
    if row is None or row["decision"] not in ("confirm", "reject"):
        raise AppError(404, "Decisione assente")
    correction = row["decision"] == "reject" and bool(row["target_property_corrected"])
    if correction and (row["score"] is None or not row["confidence_rule"]):
        raise AppError(422, "Correzione senza fotografia")
    already = conn.execute(
        """
        SELECT 1 FROM app.mapping_decisions
        WHERE revoked_decision_id = %s::uuid
        """,
        (decision_id,),
    ).fetchone()
    if already:
        if correction:
            raise AppError(409, "Correzione già annullata")
        raise AppError(409, ALREADY_REVOKED)
    revoked_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.mapping_decisions (
          id, mapping_id, decision, confidence_method, actor, actor_role,
          source_name, source_field, target_property, score, confidence_rule,
          company_id, origin, source_decision_id, batch_id, revoked_decision_id,
          reason
        )
        VALUES (%s, %s, 'revoked', 'human', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            revoked_id,
            row["mapping_id"],
            actor,
            role,
            row["source_name"],
            row["source_field"],
            row["target_property"],
            row["score"],
            row["confidence_rule"],
            company_id,
            row["origin"],
            row["source_decision_id"],
            row["batch_id"],
            decision_id,
            motive,
        ),
    )
    if correction:
        _reset_learned_mappings(conn, decision_id, company_id)
    record(
        conn,
        action="human_decision",
        actor=actor,
        role=role,
        entity_ref=decision_id,
        payload={
            "kind": "mapping_correction_revoked" if correction else "mapping_decision_revoked",
            "revoked_decision_id": decision_id,
            "decision_id": revoked_id,
            "company_id": company_id,
            "reason": motive,
        },
    )
    return {"id": revoked_id, "revoked_decision_id": decision_id, "decision": "revoked"}


def _reset_learned_mappings(conn: psycopg.Connection, decision_id: str, company_id: str) -> None:
    open_rows = conn.execute(
        """
        SELECT id::text, source_field, profile
        FROM app.field_mappings
        WHERE source_decision_id = %s::uuid
          AND company_id = %s::uuid
          AND status = 'proposed'
          AND valid_to IS NULL
        """,
        (decision_id, company_id),
    ).fetchall()
    for mapping in open_rows:
        target_type, target_property, confidence, _rule = assigned_mapping(
            mapping["profile"], mapping["source_field"]
        )
        conn.execute(
            """
            UPDATE app.field_mappings
            SET target_type = %s,
                target_property = %s,
                confidence = %s,
                origin = 'rule',
                source_decision_id = NULL
            WHERE id = %s
            """,
            (target_type, target_property, confidence, mapping["id"]),
        )
    conn.execute(
        """
        UPDATE app.field_mappings
        SET origin = 'rule', source_decision_id = NULL
        WHERE source_decision_id = %s::uuid
          AND company_id = %s::uuid
        """,
        (decision_id, company_id),
    )


def record_table_type_decision(
    conn: psycopg.Connection,
    file_id: str,
    *,
    actor: str,
    role: str,
    company_id: str | None,
    decision: str,
    correct_type: str | None,
) -> dict:
    """Store a file-type decision. Proposed type and score come from the batch."""
    _require_mapper(role)
    if not company_id:
        raise AppError(403, USER_WITHOUT_COMPANY)
    if decision not in ("correct", "wrong", "not_sure"):
        raise AppError(422, "Decisione sul tipo non valida")
    if decision == "wrong":
        if correct_type not in PROFILE_LABELS:
            raise AppError(422, "Tipo corretto non ammesso")
    elif correct_type not in (None, ""):
        raise AppError(422, "Il tipo corretto si indica solo se il file non è di questo tipo")
    else:
        correct_type = None
    batch = conn.execute(
        """
        SELECT id::text, source_name, proposed_type, proposed_score, proposed_rule,
               company_id::text AS company_id
        FROM app.landing_batches
        WHERE id = %s::uuid
        """,
        (file_id,),
    ).fetchone()
    if batch is None or batch["company_id"] != company_id:
        raise AppError(404, "File assente")
    if decision == "wrong" and correct_type == batch["proposed_type"]:
        raise AppError(422, "Il tipo corretto è quello già proposto")
    if batch["proposed_type"] is None or batch["proposed_score"] is None or not batch["proposed_rule"]:
        raise AppError(422, "Tipo di file non fotografato")
    decision_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.table_type_decisions (
          id, company_id, file_id, proposed_type, score, rule_version, decision, correct_type,
          actor, actor_role
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            decision_id,
            company_id,
            file_id,
            batch["proposed_type"],
            batch["proposed_score"],
            batch["proposed_rule"],
            decision,
            correct_type,
            actor,
            role,
        ),
    )
    record(
        conn,
        action="human_decision",
        actor=actor,
        role=role,
        entity_ref=file_id,
        payload={
            "kind": "table_type_decision",
            "decision_id": decision_id,
            "file_id": file_id,
            "proposed_type": batch["proposed_type"],
            "decision": decision,
            "correct_type": correct_type,
            "company_id": company_id,
        },
    )
    return {
        "id": decision_id,
        "file_id": file_id,
        "proposed_type": batch["proposed_type"],
        "score": batch["proposed_score"],
        "rule_version": batch["proposed_rule"],
        "decision": decision,
        "correct_type": correct_type,
    }


def _require_visible_company(mapping_company: str | None, viewer_company: str | None) -> None:
    if mapping_company is None:
        return
    if viewer_company is None or mapping_company != viewer_company:
        raise AppError(404, "Mapping assente")


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
        from services.cash.catalog import ORDER_PROCESS_ID, ORDER_STEP_ID

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
            confidence_method="rule",
            confidence_rule=BASELINE_CONFIDENCE_RULE,
            requires_cfo=requires_cfo(result["amount"]),
            actor=actor,
            role=role,
            process_id=ORDER_PROCESS_ID,
            step_id=ORDER_STEP_ID,
        )
        created.append(recommendation_id)
    return created


def approve(
    conn: psycopg.Connection,
    recommendation_id: str,
    actor: str,
    role: str,
    evidence_hash: str,
    *,
    allow_missing_company: bool = False,
) -> dict:
    mark_approved(
        conn,
        recommendation_id,
        actor=actor,
        role=role,
        evidence_hash=evidence_hash,
        allow_missing_company=allow_missing_company,
    )
    payloads = dry_run(conn, recommendation_id, actor=actor, role=role)
    return {"id": recommendation_id, "status": "dry_run_ok", "dry_run": payloads}


def execute_recommendation(
    conn: psycopg.Connection, recommendation_id: str, actor: str, role: str, model: Ontology
) -> dict:
    if role not in ("admin", "approver"):
        raise AppError(403, "Solo un approvatore esegue azioni")
    return execute(conn, model, recommendation_id, actor=actor, role=role)


def load_fixtures(
    conn: psycopg.Connection,
    actor: str,
    role: str,
    model: Ontology,
    company_id: str | None = None,
) -> dict[str, Any]:
    _require_mapper(role)
    loaded = []
    for source_name, filename in FIXTURE_FILES.items():
        result = ingest_csv(
            conn,
            FIXTURES / filename,
            source_name,
            actor,
            role,
            FIXTURE_SOURCE_CLASS[source_name],
            company_id=company_id,
        )
        loaded.append({"source": source_name, **result})
    seed_src(conn)
    seed_price_list(conn)
    confirmed = confirm_confident(conn, role, model, actor=actor, company_id=company_id)
    counts = refresh_canonical(conn, model)
    recommendations = run_agents(conn, actor=actor, role=role)
    return {
        "sources": loaded,
        "confirmed_mappings": confirmed,
        "canonical": counts,
        "recommendations": recommendations,
    }


def seed_price_list(conn: psycopg.Connection) -> None:
    path = FIXTURES / "listino.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            product = (row.get("product_source_key") or "").strip()
            price = row.get("price")
            if not product or price in (None, ""):
                continue
            conn.execute(
                """
                INSERT INTO app.price_list (product_source_key, price)
                VALUES (%s, %s)
                ON CONFLICT (product_source_key) DO UPDATE SET price = EXCLUDED.price
                """,
                (product, price),
            )


def seed_src(conn: psycopg.Connection) -> None:
    for source_name, table in SRC_TABLES.items():
        conn.execute(f"DELETE FROM {table}")
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


ORPHAN_STOCK = "Solo l'amministratore IEOL può correggere una giacenza senza azienda"


def adjust_on_hand(
    conn: psycopg.Connection,
    model: Ontology,
    product_key: str,
    on_hand: int,
    company_id: str | None = None,
    role: str | None = None,
    ieol_admin: bool = False,
) -> list[str]:
    import json

    # Prefer this company's stock. A row written before companies existed
    # has company_id NULL: only the IEOL administrator may correct it.
    # Another company's row is not selected.
    row = conn.execute(
        """
        SELECT e.id::text, e.company_id::text AS owner_id
        FROM app.entities e
        JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL
        WHERE e.type = 'StockPosition'
          AND f.property = 'product_source_key' AND f.value = %s::jsonb
          AND (
            e.company_id IS NOT DISTINCT FROM %s::uuid
            OR e.company_id IS NULL
          )
        ORDER BY (e.company_id IS NOT DISTINCT FROM %s::uuid) DESC, e.business_key
        LIMIT 1
        """,
        (json.dumps(product_key), company_id, company_id),
    ).fetchone()
    if row is None:
        raise AppError(404, "Giacenza assente")
    del role
    if row["owner_id"] is None and not ieol_admin:
        raise AppError(403, ORPHAN_STOCK)
    prop = model.require_prop("StockPosition", "on_hand")
    _put_fact(
        conn,
        entity_id=row["id"],
        name="on_hand",
        value=coerce_number(on_hand),
        classification=prop.classification,
        origin="original",
        source="adjustment",
        confidence=None,
        source_class="manual",
        confidence_method="human",
    )
    materialize(conn, model)
    return expire_stale(conn)


def _require_mapper(role: str) -> None:
    if role not in ("admin", "mapper"):
        raise AppError(403, "Serve il ruolo mapper")


def acknowledge_drift(
    conn: psycopg.Connection,
    drift_id: str,
    role: str,
    *,
    actor: str,
    company_id: str | None,
) -> dict:
    _require_mapper(role)
    if not company_id:
        raise AppError(403, USER_WITHOUT_COMPANY)
    row = conn.execute(
        """
        SELECT id::text, status, company_id::text AS company_id
        FROM app.drift_reviews
        WHERE id = %s
        FOR UPDATE
        """,
        (drift_id,),
    ).fetchone()
    if row is None or (row["company_id"] is not None and row["company_id"] != company_id):
        raise AppError(404, "Drift assente")
    if row["status"] != "open":
        raise AppError(409, QUEUE_MESSAGE)
    conn.execute(
        """
        UPDATE app.drift_reviews
        SET status = 'acknowledged', company_id = %s, actor = %s
        WHERE id = %s
        """,
        (company_id, actor, drift_id),
    )
    return {"id": drift_id, "status": "acknowledged"}
