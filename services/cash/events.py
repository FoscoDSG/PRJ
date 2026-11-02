"""A dated Event for every process state change."""

from __future__ import annotations

import psycopg

from ontology.loader import Ontology, OntologyError
from services.canonical.store import _ensure_entity, _write_facts
from services.graph.projection import _edge


def record_process_event(
    conn: psycopg.Connection,
    ontology: Ontology,
    *,
    company_id: str,
    subject_id: str,
    event_type: str,
    summary: str,
    occurred_at: str,
    process_id: str,
    step_id: str,
    source: str,
    source_class: str,
) -> str:
    process = ontology.process(process_id)
    if process is None or process.step(step_id) is None:
        raise OntologyError(f"Passo sconosciuto: {process_id}.{step_id}")
    business_key = f"{process_id}|{step_id}|{subject_id}|{occurred_at}|{event_type}"
    entity_id = _ensure_entity(conn, "Event", business_key, company_id)
    _write_facts(
        conn,
        ontology,
        entity_id,
        "Event",
        {
            "event_type": event_type,
            "summary": summary,
            "occurred_at": occurred_at,
            "process_id": process_id,
            "step_id": step_id,
        },
        source,
        {},
        source_classes={
            "event_type": source_class,
            "summary": source_class,
            "occurred_at": source_class,
            "process_id": source_class,
            "step_id": source_class,
        },
    )
    _edge(conn, subject_id, "triggered", entity_id)
    return entity_id
