"""One open/closed rule for every queue.

A card is open when it has no human decision, or when its last human
decision is a revocation. Order exceptions and payment matches both call
``card_disposition``, so the counts on the page match the cards.
"""

from __future__ import annotations


def card_disposition(decisions: list[dict]) -> str:
    """``open`` or ``closed``.

    ``decisions`` are chronological and include revocation rows. The last
    row decides: no rows, or a last row whose decision is ``revoked``,
    means open. Any other last decision means closed.
    """
    if not decisions:
        return "open"
    last = decisions[-1]
    if str(last.get("decision") or "") == "revoked":
        return "open"
    return "closed"
