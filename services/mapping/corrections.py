"""Learned mapping suggestions from append-only decisions.

Migration 0007 said ``app.mapping_decisions`` is not a dataset the agent reads
in order to propose. Migration 0010 changes that rule; this module does not
bypass it. The mapping review reads the table only to propose a learned
suggestion for the same company, the same source system, and the same
normalized header. A NULL company_id is never a suggestion. The order agent
does not read the table.

``company_id`` is the authenticated user's company. Callers pass the value
loaded from ``app.users``. It is not a request field.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from services.mapping.headers import normalize_header
from services.mapping.labels import PROFILE_LABELS, profile_label

ROME = ZoneInfo("Europe/Rome")
STRONG_NOTE = "Indicazione automatica, da verificare"
RULE_ORIGIN_TEXT = "Da regola"


def company_id_for_user(conn: psycopg.Connection, username: str) -> str | None:
    """Company of the authenticated user. Never taken from the client."""
    row = conn.execute(
        "SELECT company_id::text AS company_id FROM app.users WHERE username = %s",
        (username,),
    ).fetchone()
    if row is None:
        return None
    return row["company_id"]


def rome_day(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ROME).strftime("%d/%m/%Y")


def suggestion_band(origin: str, confidence: Any) -> tuple[str, str]:
    """Strong means the uncalibrated fallback 0.75. It is not a guarantee."""
    from services.mapping.engine import THRESHOLD

    if origin == "learned":
        return "verificare", ""
    try:
        score = float(confidence)
    except (TypeError, ValueError):
        return "verificare", ""
    if score >= THRESHOLD:
        return "forte", STRONG_NOTE
    return "verificare", ""


def active_correction(
    conn: psycopg.Connection,
    *,
    company_id: str | None,
    source_name: str,
    header: str,
) -> dict[str, Any] | None:
    """Latest human correction for this company, source, and normalized header.

    NULL company rows are not read. A revoked correction, or a later reject of
    a suggestion learned from it, stops applying.
    """
    if not company_id or not source_name or not header:
        return None
    rows = conn.execute(
        """
        SELECT d.id::text AS id, d.decision, d.confidence_method, d.source_field,
               d.target_property_corrected, d.actor, d.decided_at, d.origin,
               d.source_decision_id::text AS source_decision_id,
               d.revoked_decision_id::text AS revoked_decision_id,
               m.target_type
        FROM app.mapping_decisions d
        JOIN app.field_mappings m ON m.id = d.mapping_id
        WHERE d.company_id = %s::uuid
          AND d.company_id IS NOT NULL
          AND d.source_name = %s
        ORDER BY d.decided_at, d.id
        """,
        (company_id, source_name),
    ).fetchall()
    scoped = [
        row for row in rows
        if normalize_header(row["source_field"] or "") == header
    ]
    revoked = {
        row["revoked_decision_id"]
        for row in scoped
        if row["decision"] == "annullata" and row["revoked_decision_id"]
    }
    rejected = {
        row["source_decision_id"]
        for row in scoped
        if row["decision"] == "reject"
        and row["origin"] == "learned"
        and row["source_decision_id"]
    }
    corrections = [
        row for row in scoped
        if row["decision"] == "reject"
        and row["confidence_method"] == "human"
        and row["target_property_corrected"]
        and row["id"] not in revoked
        and row["id"] not in rejected
    ]
    if not corrections:
        return None
    corrections.sort(key=lambda row: (row["decided_at"], row["id"]))
    return corrections[-1]


def present_origin(
    mapping: dict[str, Any],
    decision: dict[str, Any] | None,
    *,
    viewer_company: str | None,
) -> dict[str, Any]:
    """Legacy NULL company rows are always 'Da regola', with nothing to revoke."""
    band, note = suggestion_band("rule", mapping.get("confidence"))
    plain = {
        "origin": "rule",
        "decision_id": None,
        "actor": None,
        "decided_at": None,
        "origin_text": RULE_ORIGIN_TEXT,
        "band": band,
        "band_note": note,
    }
    if mapping.get("company_id") is None:
        return plain
    if mapping.get("origin") != "learned" or not mapping.get("source_decision_id"):
        return plain
    if decision is None or decision.get("company_id") != viewer_company:
        return plain
    if viewer_company is None or mapping.get("company_id") != viewer_company:
        return plain
    decided_at = decision.get("decided_at")
    actor = decision.get("actor") or ""
    if decided_at is None or actor == "":
        return plain
    day = rome_day(decided_at)
    learned_band, learned_note = suggestion_band("learned", mapping.get("confidence"))
    return {
        "origin": "learned",
        "decision_id": mapping["source_decision_id"],
        "actor": actor,
        "decided_at": decided_at,
        "origin_text": f"Da una correzione di {actor} del {day}",
        "band": learned_band,
        "band_note": learned_note,
    }


def learned_metric(conn: psycopg.Connection) -> dict[str, int]:
    """Confirmed, rejected, and revoked learned suggestions. Separate from the rule."""
    row = conn.execute(
        """
        SELECT
          count(*) FILTER (WHERE origin = 'learned' AND decision = 'confirm') AS confirmed,
          count(*) FILTER (WHERE origin = 'learned' AND decision = 'reject') AS rejected,
          count(*) FILTER (WHERE decision = 'annullata') AS revoked
        FROM app.mapping_decisions
        """
    ).fetchone()
    return {
        "confirmed": int(row["confirmed"]),
        "rejected": int(row["rejected"]),
        "revoked": int(row["revoked"]),
    }


def file_type_metric(conn: psycopg.Connection) -> dict[str, int]:
    """``non_so`` is an error of the suggestion and is not a learning example.

    A ``sbagliato`` with a type from the closed list is a learning example.
    It is still an error of the suggestion that was shown.
    """
    row = conn.execute(
        """
        SELECT
          count(*) FILTER (WHERE decision = 'corretto') AS corretto,
          count(*) FILTER (WHERE decision = 'sbagliato') AS sbagliato,
          count(*) FILTER (WHERE decision = 'non_so') AS non_so
        FROM app.table_type_decisions
        """
    ).fetchone()
    sbagliato = int(row["sbagliato"])
    non_so = int(row["non_so"])
    return {
        "corretto": int(row["corretto"]),
        "sbagliato": sbagliato,
        "non_so": non_so,
        "errors": sbagliato + non_so,
        "learning_examples": sbagliato,
    }


def table_file_choice_codes() -> list[dict[str, str]]:
    choices = [{"code": code, "label": label} for code, label in PROFILE_LABELS.items()]
    choices.append({"code": "non_so", "label": "Non so"})
    return choices


def proposed_label(profile: str | None) -> str:
    return profile_label(profile)
