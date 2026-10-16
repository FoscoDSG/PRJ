"""Hand-scored header synonyms. Scores are rule scores, not probabilities."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from services.mapping.headers import normalize_header

ROOT = Path(__file__).resolve().parents[2]
SYNONYMS_PATH = ROOT / "ontology" / "synonyms.yaml"
SYNONYM_RULE = "mapping.synonym@v1"
ALLOWED_SOURCES = frozenset({
    "file:res_partner.xlsx",
    "file:product_template.xls",
    "file:product_purchase.xls",
    "file:odoo/addons/base/i18n/it.po",
    "file:addons/product/i18n/it.po",
    "file:addons/stock/i18n/it.po",
    "doc:https://knowledge.hubspot.com/properties/hubspots-default-contact-properties",
    "doc:https://knowledge.hubspot.com/properties/hubspot-crm-default-company-properties",
    "doc:https://knowledge.hubspot.com/properties/hubspots-default-deal-properties",
    "doc:https://knowledge.hubspot.com/it/records/search-your-crm",
    "doc:https://www.danea.it/software/easyfatt/xml/documenti/",
    "doc:https://schema.org/sku",
    "doc:https://schema.org/price",
})
_NAME_PARTS = frozenset({"given_name", "family_name"})


@dataclass(frozen=True)
class Synonym:
    header: str
    normalized: str
    language: str
    source: str
    profile: str
    target_type: str
    target_property: str
    score: float


@dataclass(frozen=True)
class UncoveredProperty:
    target_type: str
    target_property: str
    reason: str


@dataclass(frozen=True)
class NamePart:
    """A column that is only one piece of a person's name.

    Person.given_name and Person.family_name are not in the ontology, so the
    column stays under the review threshold and is not written onto full_name.
    """

    header: str
    normalized: str
    language: str
    source: str
    profile: str
    part: str


@dataclass(frozen=True)
class SynonymBook:
    version: str
    rule: str
    score_kind: str
    entries: tuple[Synonym, ...]
    name_parts: tuple[NamePart, ...]
    uncovered: tuple[UncoveredProperty, ...]


def _load() -> SynonymBook:
    raw = yaml.safe_load(SYNONYMS_PATH.read_text(encoding="utf-8"))
    if raw.get("rule") != SYNONYM_RULE:
        raise ValueError(f"Regola sinonimi inattesa: {raw.get('rule')}")
    score_kind = str(raw.get("score_kind") or "")
    folded = score_kind.casefold()
    if "punteggio di regola" not in folded:
        raise ValueError("I sinonimi devono dichiarare punteggio di regola")
    if "probabil" in folded and "non probabil" not in folded:
        raise ValueError("Il punteggio dei sinonimi non è una probabilità")
    def _source(value: str, header: str) -> str:
        source = str(value)
        folded = source.casefold()
        if not (source.startswith("file:") or source.startswith("doc:")):
            raise ValueError(f"Origine senza file: o doc:: {header}")
        if any(name in folded for name in ("shopify", "fatturapa", "fattura-pa", "woocommerce", "magento")):
            raise ValueError(f"Origine non ammessa: {source}")
        if source not in ALLOWED_SOURCES:
            raise ValueError(f"Fonte sinonimo non ammessa: {source}")
        return source

    entries = []
    for item in raw.get("entries") or []:
        header = str(item["header"])
        language = item["language"]
        if language not in ("it", "en"):
            raise ValueError(f"Lingua sinonimo non ammessa: {language}")
        score = float(item["score"])
        if not 0 < score <= 1:
            raise ValueError(f"Punteggio di regola fuori scala: {header}")
        entries.append(
            Synonym(
                header=header,
                normalized=normalize_header(header),
                language=language,
                source=_source(item["source"], header),
                profile=str(item["profile"]),
                target_type=str(item["target_type"]),
                target_property=str(item["target_property"]),
                score=score,
            )
        )
    name_parts = []
    for item in raw.get("name_parts") or []:
        header = str(item["header"])
        part = str(item["part"])
        if part not in _NAME_PARTS:
            raise ValueError(f"Parte di nome sconosciuta: {header}")
        language = item["language"]
        if language not in ("it", "en"):
            raise ValueError(f"Lingua non ammessa: {header}")
        name_parts.append(
            NamePart(
                header=header,
                normalized=normalize_header(header),
                language=language,
                source=_source(item["source"], header),
                profile=str(item.get("profile") or "customer"),
                part=part,
            )
        )
    part_keys = {(item.profile, item.normalized) for item in name_parts}
    for entry in entries:
        if (entry.profile, entry.normalized) in part_keys:
            raise ValueError(f"Una parte di nome non può essere un sinonimo: {entry.header}")
        if entry.target_property == "full_name" and entry.normalized in {item.normalized for item in name_parts}:
            raise ValueError(f"Una parte di nome non può scrivere full_name: {entry.header}")
    uncovered = tuple(
        UncoveredProperty(
            target_type=str(item["target_type"]),
            target_property=str(item["target_property"]),
            reason=str(item["reason"]),
        )
        for item in raw.get("uncovered") or []
    )
    return SynonymBook(
        version=str(raw["version"]),
        rule=SYNONYM_RULE,
        score_kind=score_kind,
        entries=tuple(entries),
        name_parts=tuple(name_parts),
        uncovered=uncovered,
    )


@lru_cache(maxsize=1)
def synonym_book() -> SynonymBook:
    return _load()


def synonym_headers() -> set[str]:
    return {entry.normalized for entry in synonym_book().entries}


def lookup_synonym(profile: str, normalized: str) -> Synonym | None:
    """Best rule-score synonym for this profile. Ambiguous targets do not match."""
    hits = [
        entry
        for entry in synonym_book().entries
        if entry.normalized == normalized and entry.profile == profile
    ]
    if not hits:
        return None
    best = max(entry.score for entry in hits)
    chosen = [entry for entry in hits if entry.score == best]
    targets = {(entry.target_type, entry.target_property) for entry in chosen}
    if len(targets) != 1:
        return None
    return chosen[0]
