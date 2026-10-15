"""Operational rules. The first rule requires CFO approval above 50 000 €."""

from __future__ import annotations

RULES_VERSION = "rules.cfo_amount@v1"
CFO_THRESHOLD = 50_000


def requires_cfo(amount: int | float | None) -> bool:
    if amount is None:
        return False
    return amount > CFO_THRESHOLD
