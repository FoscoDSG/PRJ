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
    "odoo17_res_partner",
    "odoo17_product_template",
    "odoo17_product_purchase",
    "hubspot_contacts",
    "hubspot_companies",
    "hubspot_deals",
    "danea_easyfatt_ordini",
})


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
class SynonymBook:
    version: str
    rule: str
    score_kind: str
    entries: tuple[Synonym, ...]
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
    entries = []
    for item in raw.get("entries") or []:
        source = item["source"]
        if source not in ALLOWED_SOURCES:
            raise ValueError(f"Fonte sinonimo non ammessa: {source}")
        language = item["language"]
        if language not in ("it", "en"):
            raise ValueError(f"Lingua sinonimo non ammessa: {language}")
        score = float(item["score"])
        if not 0 < score <= 1:
            raise ValueError(f"Punteggio di regola fuori scala: {item['header']}")
        entries.append(
            Synonym(
                header=str(item["header"]),
                normalized=normalize_header(item["header"]),
                language=language,
                source=source,
                profile=str(item["profile"]),
                target_type=str(item["target_type"]),
                target_property=str(item["target_property"]),
                score=score,
            )
        )
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
