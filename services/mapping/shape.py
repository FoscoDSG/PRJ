"""Recognize a table from its shape.

Roles come from headers the synonym and token rules already know, and from
the shape of the cell values. Scores are hand-set rule scores, not
probabilities. Shopify and FatturaPA column names are not inputs to these rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from services.mapping.headers import normalize_header

ROOT = Path(__file__).resolve().parents[2]
SHAPE_PATH = ROOT / "ontology" / "table_shape.yaml"
TABLE_SHAPE_RULE = "mapping.table_shape@v1"
_AMBIGUOUS_SCORE = 0.40

# Ontology property -> role. The role is not a source-column name.
_PROPERTY_ROLES = {
    ("OrderLine", "quantity"): "quantity",
    ("OrderLine", "unit_price"): "price",
    ("Product", "cost"): "price",
    ("Order", "amount"): "amount",
    ("Order", "ordered_at"): "date",
    ("Shipment", "shipped_at"): "date",
    ("Customer", "email"): "email",
    ("Customer", "vat"): "vat",
    ("Product", "source_key"): "product_code",
    ("OrderLine", "product_source_key"): "product_code",
    ("Supplier", "product_source_key"): "product_code",
    ("StockPosition", "product_source_key"): "product_code",
    ("StockPosition", "on_hand"): "stock_qty",
    ("Supplier", "delay_days"): "delay",
    ("Shipment", "source_key"): "tracking",
}

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_VAT = re.compile(r"^(?:IT)?\d{11}$", re.IGNORECASE)
_DATE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})(?:[T\s].*)?$")
_AMOUNT = re.compile(r"^[€$£]?\d+[.,]\d{2}$")
_PHONE = re.compile(r"^\+?\d[\d\s\-()]{7,}\d$")


@dataclass(frozen=True)
class ShapeRule:
    profile: str
    score: float
    requires: tuple[str, ...]
    absent: tuple[str, ...]
    at_least: tuple[str, ...]
    minimum: int


@dataclass(frozen=True)
class TableShape:
    profile: str
    score: float
    rule: str
    accepted: bool
    score_kind: str


def _load_rules() -> tuple[str, tuple[ShapeRule, ...]]:
    raw = yaml.safe_load(SHAPE_PATH.read_text(encoding="utf-8"))
    if raw.get("rule") != TABLE_SHAPE_RULE:
        raise ValueError(f"Regola forma tabella inattesa: {raw.get('rule')}")
    score_kind = str(raw.get("score_kind") or "")
    folded = score_kind.casefold()
    if "punteggio di regola" not in folded:
        raise ValueError("La forma tabella deve dichiarare punteggio di regola")
    if "probabil" in folded and "non probabil" not in folded:
        raise ValueError("Il punteggio della forma tabella non è una probabilità")
    rules = []
    for item in raw.get("rules") or []:
        score = float(item["score"])
        if not 0 < score <= 1:
            raise ValueError("Punteggio di regola fuori scala")
        rules.append(
            ShapeRule(
                profile=str(item["profile"]),
                score=score,
                requires=tuple(item.get("requires") or []),
                absent=tuple(item.get("absent") or []),
                at_least=tuple(item.get("at_least") or []),
                minimum=int(item.get("minimum") or 0),
            )
        )
    return score_kind, tuple(rules)


@lru_cache(maxsize=1)
def shape_rules() -> tuple[str, tuple[ShapeRule, ...]]:
    return _load_rules()


def _header_roles(column: str) -> set[str]:
    from services.mapping.engine import PROFILES
    from services.mapping.synonyms import synonym_book
    from services.mapping.tokens import token_book

    normalized = normalize_header(column)
    if not normalized:
        return set()
    roles: set[str] = set()
    for columns in PROFILES.values():
        for key, (type_name, prop, _score) in columns.items():
            if key == column or normalize_header(key) == normalized:
                role = _PROPERTY_ROLES.get((type_name, prop))
                if role:
                    roles.add(role)
    for entry in (*synonym_book().entries, *token_book().entries):
        if entry.normalized != normalized:
            continue
        role = _PROPERTY_ROLES.get((entry.target_type, entry.target_property))
        if role:
            roles.add(role)
    return roles


def _value_roles(values: list[object]) -> set[str]:
    sample = [str(value).strip() for value in values if value not in (None, "")]
    if len(sample) < 2:
        return set()
    roles: set[str] = set()
    if sum(bool(_EMAIL.match(value)) for value in sample) >= 2:
        roles.add("email")
    compact = [re.sub(r"\s+", "", value).upper() for value in sample]
    if sum(bool(_VAT.match(value)) for value in compact) >= 2:
        roles.add("vat")
    if sum(bool(_DATE.match(value)) for value in sample) >= 2:
        roles.add("date")
    if sum(bool(_AMOUNT.match(value.replace(" ", ""))) for value in sample) >= 2:
        roles.add("amount")
    if sum(bool(_PHONE.match(value)) and not _VAT.match(re.sub(r"\s+", "", value)) for value in sample) >= 2:
        roles.add("phone")
    return roles


def _repeating_key(columns: list[str], rows: list[dict]) -> bool:
    if len(rows) < 2:
        return False
    for column in columns:
        values = [str(row.get(column)).strip() for row in rows if row.get(column) not in (None, "")]
        if len(values) < 2 or len(set(values)) >= len(values):
            continue
        distinct = len(set(values))
        for other in columns:
            if other == column:
                continue
            others = [str(row.get(other)).strip() for row in rows if row.get(other) not in (None, "")]
            if len(set(others)) > distinct:
                return True
    return False


def _present_roles(columns: list[str], rows: list[dict]) -> set[str]:
    roles: set[str] = set()
    for column in columns:
        header_roles = _header_roles(column)
        roles |= header_roles
        values = [row.get(column) for row in rows]
        value_roles = _value_roles(values)
        if "price" in header_roles or "quantity" in header_roles:
            value_roles.discard("amount")
        roles |= value_roles
    if _repeating_key(columns, rows):
        roles.add("repeating_key")
    return roles


def _matches(rule: ShapeRule, roles: set[str]) -> bool:
    if any(role not in roles for role in rule.requires):
        return False
    if any(role in roles for role in rule.absent):
        return False
    if rule.at_least and sum(role in roles for role in rule.at_least) < rule.minimum:
        return False
    return True


def recognize_table(columns: list[str], rows: list[dict] | None = None) -> TableShape:
    """Best shape, or unknown when the file is ambiguous or below the rule score."""
    from services.mapping.engine import THRESHOLD

    score_kind, rules = shape_rules()
    roles = _present_roles(list(columns), list(rows or []))
    matched = [rule for rule in rules if _matches(rule, roles)]
    profiles = {rule.profile for rule in matched}
    if len(profiles) != 1:
        return TableShape("unknown", _AMBIGUOUS_SCORE if matched else 0.0, TABLE_SHAPE_RULE, False, score_kind)
    chosen = max((rule for rule in matched if rule.profile in profiles), key=lambda rule: rule.score)
    accepted = chosen.score >= THRESHOLD
    return TableShape(
        chosen.profile if accepted else "unknown",
        chosen.score,
        TABLE_SHAPE_RULE,
        accepted,
        score_kind,
    )
