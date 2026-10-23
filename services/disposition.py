"""One open/closed rule for every queue.

A card is open only when a person can still record a decision today.
The order queue and the payment queue both call ``card_disposition``
with the states their engine actually accepts.

An order recommendation accepts a decision in ``detected``, ``proposed``
and ``modified``. After a revocation its state is ``revoked``, and the
engine refuses another decision, so the card is closed and is not counted.

A payment match accepts a decision only while it is ``proposed``.
Revoking the last decision puts it back there, and a new decision is
accepted.
"""

from __future__ import annotations

# States in which the order engine still accepts approve, modify or reject.
ORDER_DECIDABLE = frozenset({"detected", "proposed", "modified"})
# A payment card shows the decision buttons only while it is proposed.
PAYMENT_DECIDABLE = frozenset({"proposed"})
# A three-way card accepts confirm, correct or reject only while it is proposed.
MATCH3_DECIDABLE = frozenset({"proposed"})


def card_disposition(status: str, decidable: frozenset[str]) -> str:
    """``open`` or ``closed``.

    ``decidable`` is the set of states in which this queue accepts a
    decision today. The same function serves every queue.
    """
    if str(status or "") in decidable:
        return "open"
    return "closed"
