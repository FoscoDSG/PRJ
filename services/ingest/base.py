"""Shared row shape for connectors."""

from __future__ import annotations

from typing import Any

from services.text import norm, stable


def rows_from_dicts(records: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    if not records:
        return [], []
    columns = list(records[0].keys())
    normalized = []
    for record in records:
        normalized.append({norm(str(key)): stable(value) for key, value in record.items()})
    return [norm(column) for column in columns], normalized
