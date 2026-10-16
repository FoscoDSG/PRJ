"""Normalize a source column name before profile lookup and synonym match.

The result is a lookup key. It is not a probability and it does not change
the stored source field.
"""

from __future__ import annotations

import re
import unicodedata

_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_SEPARATORS = re.compile(r"[_\-/\.\s]+")


def normalize_header(value: str) -> str:
    """Lowercase, strip accents, split camelCase, unify separators to one space."""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = _CAMEL_BOUNDARY.sub(r"\1 \2", text)
    text = _ACRONYM_BOUNDARY.sub(r"\1 \2", text)
    text = text.lower()
    text = _SEPARATORS.sub(" ", text)
    return " ".join(text.split())
