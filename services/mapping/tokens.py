"""Single-word matches against an independent schema.org vocabulary.

Scores are rule scores and stay below every synonym score. The file was added
after the first hold-out measure. It does not contain Shopify or FatturaPA names.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from services.mapping.headers import normalize_header

ROOT = Path(__file__).resolve().parents[2]
TOKENS_PATH = ROOT / "ontology" / "tokens.yaml"
TOKEN_RULE = "mapping.token@v1"


@dataclass(frozen=True)
class Token:
    term: str
    normalized: str
    source: str
    profile: str
    target_type: str
    target_property: str
    score: float


@dataclass(frozen=True)
class UnmappedToken:
    term: str
    normalized: str
    source: str
    reason: str


@dataclass(frozen=True)
class TokenBook:
    version: str
    rule: str
    score_kind: str
    added_after_holdout: bool
    entries: tuple[Token, ...]
    unmapped: tuple[UnmappedToken, ...]


def _source(value: str, term: str) -> str:
    source = str(value)
    folded = source.casefold()
    if not source.startswith("doc:https://schema.org/"):
        raise ValueError(f"Il token {term} non cita schema.org")
    if any(name in folded for name in ("shopify", "fatturapa", "fattura-pa")):
        raise ValueError(f"Origine token non ammessa: {source}")
    return source


def _load() -> TokenBook:
    raw = yaml.safe_load(TOKENS_PATH.read_text(encoding="utf-8"))
    if raw.get("rule") != TOKEN_RULE:
        raise ValueError(f"Regola token inattesa: {raw.get('rule')}")
    if raw.get("added_after_holdout") is not True:
        raise ValueError("Il vocabolario token va dichiarato successivo alla prima misura")
    score_kind = str(raw.get("score_kind") or "")
    folded = score_kind.casefold()
    if "punteggio di regola" not in folded:
        raise ValueError("I token devono dichiarare punteggio di regola")
    if "probabil" in folded and "non probabil" not in folded:
        raise ValueError("Il punteggio dei token non è una probabilità")
    entries = []
    for item in raw.get("entries") or []:
        term = str(item["term"])
        score = float(item["score"])
        if not 0 < score <= 1:
            raise ValueError(f"Punteggio di regola fuori scala: {term}")
        entries.append(
            Token(
                term=term,
                normalized=normalize_header(term),
                source=_source(item["source"], term),
                profile=str(item["profile"]),
                target_type=str(item["target_type"]),
                target_property=str(item["target_property"]),
                score=score,
            )
        )
    unmapped = tuple(
        UnmappedToken(
            term=str(item["term"]),
            normalized=normalize_header(item["term"]),
            source=_source(item["source"], str(item["term"])),
            reason=str(item["reason"]),
        )
        for item in raw.get("unmapped") or []
    )
    from services.mapping.synonyms import synonym_book

    synonym_scores = [entry.score for entry in synonym_book().entries]
    if synonym_scores and any(entry.score >= min(synonym_scores) for entry in entries):
        raise ValueError("Un punteggio token deve restare sotto tutti i sinonimi")
    return TokenBook(
        version=str(raw["version"]),
        rule=TOKEN_RULE,
        score_kind=score_kind,
        added_after_holdout=True,
        entries=tuple(entries),
        unmapped=unmapped,
    )


@lru_cache(maxsize=1)
def token_book() -> TokenBook:
    return _load()


def _choose(entries: list[Token]) -> Token | None:
    """One target, or nothing. Two words must not collapse onto the higher score."""
    if not entries:
        return None
    targets = {(entry.target_type, entry.target_property) for entry in entries}
    if len(targets) != 1:
        return None
    return max(entries, key=lambda entry: entry.score)


def _hits(entries: tuple[Token, ...] | list[Token], normalized: str) -> list[Token]:
    phrase = [entry for entry in entries if entry.normalized == normalized]
    if phrase:
        return phrase
    words = set(normalized.split())
    return [
        entry
        for entry in entries
        if len(entry.normalized.split()) == 1 and entry.normalized in words
    ]


def lookup_token(profile: str, normalized: str) -> Token | None:
    """Token for this profile. A whole phrase wins over a single word inside it."""
    pool = [entry for entry in token_book().entries if entry.profile == profile]
    return _choose(_hits(pool, normalized))


def unique_token(normalized: str) -> Token | None:
    """When the table type is unknown, use a token only if one target wins."""
    return _choose(_hits(token_book().entries, normalized))
