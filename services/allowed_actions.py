"""One function per card. The page shows a button only when its name is here.

The routes keep their own checks. The tests verify that this list and those
checks coincide: a name left out is a 403, and a name included is not refused
because of the role. A viewer without a company receives an empty list.
"""

from __future__ import annotations

from typing import Any, Callable

from services.ontology_view import is_incoming

MAPPER = frozenset({"admin", "mapper"})
APPROVER = frozenset({"admin", "approver"})

CardFn = Callable[..., list[str]]


def _open_decision(item: dict[str, Any] | None) -> bool:
    history = (item or {}).get("history") or []
    return any(entry and entry.get("id") and not entry.get("revoked") for entry in history)


def _status(item: dict[str, Any] | None, default: str = "") -> str:
    return str((item or {}).get("status") or default)


def quarantine_actions(
    role: str,
    item: dict[str, Any] | None = None,
    *,
    company: bool = True,
) -> list[str]:
    """Release and reject while the document is open. Revoke after a decision."""
    if not company or role not in MAPPER:
        return []
    actions: list[str] = []
    if _status(item, "open") == "open":
        actions.extend(["release", "reject"])
    if _open_decision(item):
        actions.append("revoke")
    return actions


def drift_actions(
    role: str,
    item: dict[str, Any] | None = None,
    *,
    company: bool = True,
) -> list[str]:
    """Acknowledge an open column change. Revoke after it was marked seen."""
    if not company or role not in MAPPER:
        return []
    actions: list[str] = []
    if _status(item, "open") == "open":
        actions.append("acknowledge")
    if _open_decision(item):
        actions.append("revoke")
    return actions


def identity_actions(
    role: str,
    item: dict[str, Any] | None = None,
    *,
    company: bool = True,
) -> list[str]:
    """Confirm or separate an open identity. Revoke after that decision."""
    if not company or role not in MAPPER:
        return []
    actions: list[str] = []
    if _status(item, "open") == "open":
        actions.extend(["confirm", "reject"])
    if _open_decision(item):
        actions.append("revoke")
    return actions


def mapping_actions(
    role: str,
    item: dict[str, Any] | None = None,
    *,
    company: bool = True,
) -> list[str]:
    """Confirm or reject a proposed column. Revoke a learned correction."""
    if not company or role not in MAPPER:
        return []
    item = item or {}
    actions: list[str] = []
    if _status(item) == "proposed":
        actions.extend(["confirm", "reject"])
    learned = item.get("origin") == "learned" and item.get("decision_id")
    if learned or _open_decision(item):
        actions.append("revoke")
    return actions


def recommendation_actions(
    role: str,
    item: dict[str, Any] | None = None,
    *,
    company: bool = True,
) -> list[str]:
    """Approve, change or reject an open order action. Execute after the dry-run."""
    if not company or role not in APPROVER:
        return []
    item = item or {}
    if item.get("executed"):
        return []
    actions: list[str] = []
    if _status(item) in {"detected", "proposed", "modified"}:
        actions.extend(["approve", "modify", "reject"])
    if _status(item) == "dry_run_ok":
        actions.append("execute")
    if _open_decision(item):
        actions.append("revoke")
    return actions


def payment_actions(
    role: str,
    item: dict[str, Any] | None = None,
    *,
    company: bool = True,
) -> list[str]:
    """Reject a proposed match. Confirm and correct only an incoming payment.

    ``is_incoming`` is the same rule as the balance and the measure, so
    Conferma and Correggi stay off an outgoing payment. Revoke comes after
    a decision.
    """
    if not company or role not in APPROVER:
        return []
    actions: list[str] = []
    if _status(item) == "proposed":
        payment = (item or {}).get("payment")
        subject = payment if isinstance(payment, dict) else item
        if is_incoming(subject):
            actions.extend(["confirm", "correct"])
        actions.append("reject")
    if _open_decision(item):
        actions.append("revoke")
    return actions


def page_actions(role: str, *, company: bool = True) -> list[str]:
    """Buttons of the Dati page: upload, confirm strong suggestions, rebuild.

    The page shows a button only when its name is in this list.
    """
    if not company or role not in MAPPER:
        return []
    return ["upload", "confirm_confident", "materialize"]


def file_actions(
    role: str,
    item: dict[str, Any] | None = None,
    *,
    company: bool = True,
) -> list[str]:
    """Say whether a file is the proposed type. The same role covers every answer."""
    del item
    if not company or role not in MAPPER:
        return []
    return ["correct", "wrong", "not_sure"]


def customer_actions(
    role: str,
    item: dict[str, Any] | None = None,
    *,
    company: bool = True,
) -> list[str]:
    """Only an administrator with a company may reveal a personal value."""
    del item
    if not company or role != "admin":
        return []
    return ["unmask"]


CARD_ACTIONS: dict[str, CardFn] = {
    "quarantine": quarantine_actions,
    "drift": drift_actions,
    "identity": identity_actions,
    "mapping": mapping_actions,
    "recommendation": recommendation_actions,
    "payment": payment_actions,
    "file": file_actions,
    "customer": customer_actions,
}


def stamp(rows: list[dict[str, Any]], role: str, fn: CardFn, *, company: bool) -> list[dict[str, Any]]:
    """Attach ``allowed_actions`` from the one function for this card type."""
    for row in rows:
        row["allowed_actions"] = fn(role, row, company=company)
    return rows
