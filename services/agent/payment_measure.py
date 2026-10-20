"""Precision, recall and the share of payments left unmatched.

Labels are human confirmations and corrections only. The latest decision
of a proposal counts. A revocation is a later decision and is counted
apart, as for mappings. Synthetic files stay out. The result stays
``non misurata`` until 30 human cases and 3 distinct source names.
Two companies that share a source name count as one source.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg

from services.agent.payment_match import RULE, SCORE_KIND, money

MIN_MEASURED_CASES = 30
MIN_MEASURED_SOURCES = 3
UNMEASURED = "non misurata"


def measure_payment_matches(conn: psycopg.Connection) -> dict[str, Any]:
    proposals = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id::text, company_id::text AS company_id, payment_entity_id::text,
                   invoice_entity_id::text, source_name, label_origin,
                   allocated_amount
            FROM app.payment_proposals
            """
        ).fetchall()
    ]
    decisions = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id::text, proposal_id::text, payment_entity_id::text,
                   company_id::text AS company_id, decision,
                   invoice_entity_id::text, allocated_amount, source_name,
                   revokes_decision_id::text, decided_at
            FROM app.payment_decisions
            ORDER BY decided_at, id
            """
        ).fetchall()
    ]
    payments = [
        dict(row)
        for row in conn.execute(
            """
            SELECT e.id::text, d.company_id::text AS company_id,
                   d.source_name, d.label_origin
            FROM app.entities e
            JOIN app.cash_documents d ON d.entity_id = e.id
            WHERE e.type = 'Payment' AND e.valid_to IS NULL
            """
        ).fetchall()
    ]
    return score_cases(proposals, decisions, payments)


def score_cases(
    proposals: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    payments: list[dict[str, Any]],
) -> dict[str, Any]:
    by_proposal = {item["id"]: item for item in proposals}
    by_decision = {item["id"]: item for item in decisions}
    payments_by_id = {item["id"]: item for item in payments}
    revoked_ids = {
        item["revokes_decision_id"]
        for item in decisions
        if item.get("decision") == "revoked" and item.get("revokes_decision_id")
    }
    latest: dict[str, dict[str, Any]] = {}
    for item in decisions:
        if item.get("decision") == "revoked":
            continue
        if item["id"] in revoked_ids:
            continue
        key = item.get("proposal_id") or f"payment:{item.get('payment_entity_id')}"
        latest[key] = item
    true_positive = 0
    false_positive = 0
    false_negative = 0
    sources: set[str] = set()
    missing_source = 0
    used = 0
    for item in latest.values():
        proposal = by_proposal.get(item.get("proposal_id") or "")
        if _label_origin(item, by_proposal, payments_by_id) == "synthetic":
            continue
        source_name = (item.get("source_name") or (proposal or {}).get("source_name") or "").strip()
        if source_name == "":
            missing_source += 1
        else:
            sources.add(source_name)
        used += 1
        if proposal is None:
            if item["decision"] == "correct":
                false_negative += 1
            continue
        same_invoice = item.get("invoice_entity_id") in (None, proposal["invoice_entity_id"])
        same_amount = _same_amount(item.get("allocated_amount"), proposal["allocated_amount"])
        if item["decision"] == "confirm" or (
            item["decision"] == "correct" and same_invoice and same_amount
        ):
            true_positive += 1
            continue
        false_positive += 1
        if item["decision"] == "correct" and not (same_invoice and same_amount):
            false_negative += 1
    declared_payments = [item for item in payments if (item.get("label_origin") or "rule") != "synthetic"]
    proposed_ids = {
        item["payment_entity_id"]
        for item in proposals
        if item.get("label_origin") != "synthetic"
    }
    unmatched = [
        item for item in declared_payments if item["id"] not in proposed_ids
    ]
    total_payments = len(declared_payments)
    unmatched_share = (len(unmatched) / total_payments) if total_payments else None
    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    measured = used >= MIN_MEASURED_CASES and len(sources) >= MIN_MEASURED_SOURCES
    revocations = 0
    for item in decisions:
        if item.get("decision") != "revoked":
            continue
        target = by_decision.get(item.get("revokes_decision_id") or "")
        if target is None:
            continue
        if _label_origin(target, by_proposal, payments_by_id) == "synthetic":
            continue
        revocations += 1
    return {
        "rule": RULE,
        "score_kind": SCORE_KIND,
        "label": "misurata" if measured else UNMEASURED,
        "provisional": not measured,
        "measured": measured,
        "human_cases": used,
        "source_count": len(sources),
        "source_names": sorted(sources),
        "missing_source": missing_source,
        "minimum_cases": MIN_MEASURED_CASES,
        "minimum_sources": MIN_MEASURED_SOURCES,
        "revocations": revocations,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "unmatched_share": unmatched_share,
        "unmatched_payments": len(unmatched),
        "payments": total_payments,
    }


def _label_origin(
    item: dict[str, Any],
    by_proposal: dict[str, dict[str, Any]],
    payments_by_id: dict[str, dict[str, Any]],
) -> str:
    """Synthetic files stay out. A missing proposal uses the payment's origin."""
    proposal = by_proposal.get(item.get("proposal_id") or "")
    if proposal is not None:
        return str(proposal.get("label_origin") or "rule")
    payment = payments_by_id.get(item.get("payment_entity_id") or "")
    if payment is not None:
        return str(payment.get("label_origin") or "rule")
    return "rule"


def _same_amount(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return money(left) == money(right)


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator
