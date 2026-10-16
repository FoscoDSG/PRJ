"""Map source columns onto the ontology. Ambiguous columns stay below the review threshold."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Json

from services.audit import record
from services.mapping.headers import normalize_header
from services.mapping.synonyms import SYNONYM_RULE, lookup_synonym
from services.mapping.tokens import TOKEN_RULE, lookup_token, unique_token
from services.text import digest

THRESHOLD = 0.75
# Rule score when the header type and the shape type name two different tables.
# It stays under the threshold. It is not a probability.
DISAGREEMENT_SCORE = 0.40
HEADER_SIMILARITY_RULE = "mapping.header_similarity@v1"
NORMALIZED_HEADER_RULE = "mapping.normalized_header@v1"
SYNONYM_HEADER_RULE = SYNONYM_RULE
UNKNOWN_HEADER_RULE = "mapping.unknown_header@v1"

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

# Signals are normalized header keys. Italian names stay; English and
# variant names come from Odoo 17, HubSpot and Danea, not from the hold-out.
SIGNALS: dict[str, set[str]] = {
    "supplier": {
        "ritardo giorni", "forn", "delivery lead time", "delay", "vendor product code",
    },
    "stock": {"giacenza", "quantity on hand", "qty available"},
    "order_line": {"qty", "quantita", "quantita prodotti", "quantity"},
    "order": {
        "importo", "ord head", "amount", "deal name", "dealname",
        "total without tax", "numero documento",
    },
    "customer": {
        "ragione sociale", "piva", "partita iva", "vat", "tax id",
        "customer vat code", "is a company",
    },
    "product": {
        "descrizione", "costo", "default code", "internal reference",
        "standard price", "cost",
    },
    "shipment": {"tracking number", "numero di tracciatura spedizione", "transport date time"},
}

_ORDER_AMOUNT = {"importo", "amount", "total without tax"}
_SUPPLIER_REQUIRED = {"ritardo giorni", "forn", "delivery lead time", "delay", "vendor product code"}


def _present_headers(columns: list[str]) -> set[str]:
    return {normalize_header(column) for column in columns if column and str(column).strip()}


@dataclass(frozen=True)
class TableDecision:
    """File type after headers and shape are read together.

    ``score`` is a rule score, not a probability. ``header_profile`` and
    ``shape_profile`` are the two readings before they are combined.
    """

    profile: str
    score: float
    accepted: bool
    rule: str
    score_kind: str
    header_profile: str
    shape_profile: str


def _profile_from_headers(columns: list[str]) -> str:
    present = _present_headers(columns)
    for name in ("supplier", "stock", "order_line", "order", "customer", "product", "shipment"):
        if not (SIGNALS[name] & present):
            continue
        if name == "stock" and "giacenza" not in present and (SIGNALS["product"] & present):
            continue
        if name == "order" and not (_ORDER_AMOUNT & present):
            continue
        if name == "supplier" and not (_SUPPLIER_REQUIRED & present):
            continue
        return name
    return "unknown"


def _under_threshold(score: float) -> float:
    if score >= THRESHOLD:
        return DISAGREEMENT_SCORE
    return score


def classify_table(columns: list[str], rows: list[dict[str, Any]] | None = None) -> TableDecision:
    """Combine the header type and the shape type.

    Order lines are not accepted for a product catalog. When the two readings
    name different types, the combined score drops below the threshold and the
    file stays unrecognized.
    """
    from services.mapping.shape import TABLE_SHAPE_RULE, catalog_signal, recognize_table

    header = _profile_from_headers(columns)
    shape = recognize_table(columns, rows)
    header_type = None if header == "unknown" else header
    shape_type = shape.profile if shape.accepted else None
    if header_type and shape_type and header_type != shape_type:
        return TableDecision(
            "unknown",
            DISAGREEMENT_SCORE,
            False,
            TABLE_SHAPE_RULE,
            shape.score_kind,
            header,
            shape.profile,
        )
    catalog = catalog_signal(columns, rows)
    if catalog and header_type == "order_line":
        header_type = None
    if catalog and shape_type == "order_line":
        shape_type = None
    if header_type and shape_type:
        accepted = shape.score >= THRESHOLD and shape.profile != "unknown"
        return TableDecision(
            shape.profile,
            shape.score,
            accepted,
            shape.rule,
            shape.score_kind,
            header,
            shape.profile,
        )
    if shape_type:
        return TableDecision(
            shape.profile,
            shape.score,
            True,
            shape.rule,
            shape.score_kind,
            header,
            shape.profile,
        )
    if header_type:
        return TableDecision(
            header_type,
            shape.score,
            shape.score >= THRESHOLD,
            shape.rule,
            shape.score_kind,
            header,
            shape.profile,
        )
    return TableDecision(
        "unknown",
        _under_threshold(shape.score),
        False,
        shape.rule,
        shape.score_kind,
        header,
        shape.profile,
    )


def detect_profile(columns: list[str], rows: list[dict[str, Any]] | None = None) -> str:
    if rows is None:
        return _profile_from_headers(columns)
    return classify_table(columns, rows).profile


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
    profile = detect_profile(columns, rows)
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
        target_type, target_property, confidence, _rule = assigned_mapping(profile, column)
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


def _profile_by_normalized_key(profile: str, normalized: str) -> tuple[str, str, float] | None:
    found = []
    for key, value in PROFILES.get(profile, {}).items():
        if normalize_header(key) == normalized:
            found.append(value)
    if not found:
        return None
    targets = {(item[0], item[1]) for item in found}
    if len(targets) != 1:
        return None
    return max(found, key=lambda item: item[2])


def _unique_across_profiles(normalized: str) -> tuple[str, str, float, str] | None:
    """When the table type is unknown, use a normalized key only if one target wins."""
    found: list[tuple[str, str, float]] = []
    for profile in PROFILES:
        match = _profile_by_normalized_key(profile, normalized)
        if match is not None:
            found.append(match)
    targets = {(item[0], item[1]) for item in found}
    if len(targets) != 1:
        return None
    target_type, target_property, confidence = max(found, key=lambda item: item[2])
    return target_type, target_property, confidence, NORMALIZED_HEADER_RULE


def assigned_mapping(profile: str, column: str) -> tuple[str, str, float, str]:
    """Target and rule id for one source column. The rule id includes its version.

    Scores are rule scores, not probabilities. An exact Italian key keeps
    mapping.header_similarity@v1. A normalized form of that key uses
    mapping.normalized_header@v1. A synonym from the versioned dictionary
    uses mapping.synonym@v1. A schema.org word that is still free uses
    mapping.token@v1, with a score below the synonyms.
    """
    known = PROFILES.get(profile, {})
    if column in known:
        target_type, target_property, confidence = known[column]
        return target_type, target_property, confidence, HEADER_SIMILARITY_RULE
    normalized = normalize_header(column)
    if not normalized:
        return "Document", "title", 0.42, UNKNOWN_HEADER_RULE
    if profile in PROFILES:
        match = _profile_by_normalized_key(profile, normalized)
        if match is not None:
            target_type, target_property, confidence = match
            return target_type, target_property, confidence, NORMALIZED_HEADER_RULE
    elif profile in ("", "unknown"):
        unique = _unique_across_profiles(normalized)
        if unique is not None:
            return unique
    synonym = lookup_synonym(profile, normalized)
    if synonym is not None:
        return synonym.target_type, synonym.target_property, synonym.score, SYNONYM_HEADER_RULE
    if profile in ("", "unknown"):
        synonym = _unique_synonym(normalized)
        if synonym is not None:
            return synonym.target_type, synonym.target_property, synonym.score, SYNONYM_HEADER_RULE
    if profile in PROFILES:
        token = lookup_token(profile, normalized)
        if token is not None:
            return token.target_type, token.target_property, token.score, TOKEN_RULE
    elif profile in ("", "unknown"):
        token = unique_token(normalized)
        if token is not None:
            return token.target_type, token.target_property, token.score, TOKEN_RULE
    return "Document", "title", 0.42, UNKNOWN_HEADER_RULE


def _unique_synonym(normalized: str):
    from services.mapping.synonyms import synonym_book

    hits = [entry for entry in synonym_book().entries if entry.normalized == normalized]
    if not hits:
        return None
    best = max(entry.score for entry in hits)
    chosen = [entry for entry in hits if entry.score == best]
    targets = {(entry.target_type, entry.target_property) for entry in chosen}
    if len(targets) != 1:
        return None
    return chosen[0]


def rule_id_for(profile: str, column: str) -> str:
    return assigned_mapping(profile, column)[3]
