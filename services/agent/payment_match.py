"""Deterministic match of an incoming payment to invoices.

The score is a rule score, not a probability. The rule never closes an
invoice. A proposal is stored only when the policy accepts it. The default
policy is that a wrong match is worse than a missing one: amount, customer
and reference must all coincide, otherwise the payment stays unmatched.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

RULE = "payment-match.baseline@v1"
SCORE_KIND = "punteggio di regola, non probabilità"
CRITERIA = ("amount", "customer", "reference")
# Complete coincidence. Not a probability.
COMPLETE_SCORE = 1.0
DEFAULT_THRESHOLD = 1.0


@dataclass(frozen=True)
class MatchPolicy:
    """One switch, plus a threshold that can change without rewriting the rule.

    ``require_all_criteria`` is the default the CTO chose: when it is true,
    a missing piece leaves the payment unmatched. ``threshold`` is the
    minimum rule score. It starts at 1, the score of a complete coincidence.
    """

    rule: str = RULE
    score_kind: str = SCORE_KIND
    require_all_criteria: bool = True
    threshold: float = DEFAULT_THRESHOLD


@dataclass
class InvoiceView:
    entity_id: str
    number: str
    residual: Decimal
    vat: str = ""
    name: str = ""


@dataclass
class PaymentView:
    entity_id: str
    source_key: str
    amount: Decimal
    vat: str = ""
    name: str = ""
    text: str = ""
    source_name: str = ""
    label_origin: str = "rule"
    paid_at: str = ""


@dataclass
class AllocationProposal:
    payment_id: str
    invoice_id: str
    invoice_number: str
    allocated_amount: Decimal
    criteria: dict[str, bool]
    score: float
    source_name: str
    label_origin: str


@dataclass
class MatchResult:
    proposals: list[AllocationProposal] = field(default_factory=list)
    unmatched_payment_ids: list[str] = field(default_factory=list)


def load_policy() -> MatchPolicy:
    """Read the two settings. Missing variables keep the strict default."""
    raw_threshold = os.environ.get("IEOL_PAYMENT_MATCH_THRESHOLD", "").strip()
    threshold = DEFAULT_THRESHOLD if raw_threshold == "" else float(raw_threshold)
    if not 0 < threshold <= 1:
        raise ValueError("Soglia di abbinamento fuori scala")
    flag = os.environ.get("IEOL_PAYMENT_MATCH_REQUIRE_ALL", "1").strip().lower()
    require_all = flag not in {"0", "false", "no"}
    return MatchPolicy(require_all_criteria=require_all, threshold=threshold)


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def vat_key(value: str | None) -> str:
    compact = re.sub(r"\s+", "", (value or "")).upper()
    if compact.startswith("IT") and len(compact) > 2:
        return compact[2:]
    return compact


def names_equal(left: str | None, right: str | None) -> bool:
    from services.identity.resolve import normalize_name

    a = normalize_name(left or "")
    b = normalize_name(right or "")
    return bool(a) and a == b


def contains_reference(text: str | None, number: str | None) -> bool:
    if not text or not number:
        return False
    pattern = re.compile(
        rf"(?<![0-9A-Za-z]){re.escape(number.strip())}(?![0-9A-Za-z])",
        re.IGNORECASE,
    )
    return bool(pattern.search(text))


def customer_match(payment: PaymentView, invoice: InvoiceView) -> bool:
    left = vat_key(payment.vat)
    right = vat_key(invoice.vat)
    if left and right:
        return left == right
    return names_equal(payment.name, invoice.name)


def rule_score(criteria: dict[str, bool]) -> float:
    hits = sum(1 for key in CRITERIA if criteria.get(key))
    if hits == len(CRITERIA):
        return COMPLETE_SCORE
    return hits / len(CRITERIA)


def accepted(criteria: dict[str, bool], policy: MatchPolicy) -> bool:
    score = rule_score(criteria)
    if score < policy.threshold:
        return False
    if policy.require_all_criteria and not all(criteria.get(key) for key in CRITERIA):
        return False
    return True


def select_matches(
    payments: list[PaymentView],
    invoices: list[InvoiceView],
    policy: MatchPolicy | None = None,
) -> MatchResult:
    """Propose allocations. Anything doubtful stays in ``unmatched_payment_ids``."""
    chosen_policy = policy or load_policy()
    remaining = {item.entity_id: money(item.residual) for item in invoices}
    proposals: list[AllocationProposal] = []
    unmatched: list[str] = []
    ordered = sorted(payments, key=lambda item: (item.paid_at, item.entity_id))
    for payment in ordered:
        picked = _choose(payment, invoices, remaining, chosen_policy)
        if not picked:
            unmatched.append(payment.entity_id)
            continue
        for item in picked:
            remaining[item.invoice_id] = money(remaining[item.invoice_id] - item.allocated_amount)
            proposals.append(item)
    return MatchResult(proposals=proposals, unmatched_payment_ids=unmatched)


def _choose(
    payment: PaymentView,
    invoices: list[InvoiceView],
    remaining: dict[str, Decimal],
    policy: MatchPolicy,
) -> list[AllocationProposal]:
    amount = money(payment.amount)
    if amount <= 0:
        return []
    candidates = []
    for invoice in invoices:
        left = remaining.get(invoice.entity_id, Decimal("0.00"))
        if left <= 0:
            continue
        if not customer_match(payment, invoice):
            continue
        if not contains_reference(payment.text, invoice.number):
            continue
        candidates.append(invoice)
    if not candidates:
        return []
    if len(candidates) == 1:
        invoice = candidates[0]
        left = remaining[invoice.entity_id]
        amount_ok = amount == left
        criteria = {"amount": amount_ok, "customer": True, "reference": True}
        if not accepted(criteria, policy):
            return []
        allocated = left if amount_ok else amount
        if allocated > left:
            return []
        return [_proposal(payment, invoice, allocated, criteria)]
    total = money(sum((remaining[item.entity_id] for item in candidates), Decimal("0.00")))
    # Several references: only the exact sum is unambiguous. A subset would be a guess.
    if total != amount:
        return []
    criteria = {"amount": True, "customer": True, "reference": True}
    if not accepted(criteria, policy):
        return []
    return [
        _proposal(payment, invoice, remaining[invoice.entity_id], criteria)
        for invoice in candidates
    ]


def _proposal(
    payment: PaymentView,
    invoice: InvoiceView,
    allocated: Decimal,
    criteria: dict[str, bool],
) -> AllocationProposal:
    return AllocationProposal(
        payment_id=payment.entity_id,
        invoice_id=invoice.entity_id,
        invoice_number=invoice.number,
        allocated_amount=money(allocated),
        criteria=dict(criteria),
        score=rule_score(criteria),
        source_name=payment.source_name,
        label_origin=payment.label_origin,
    )
