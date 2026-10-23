"""Tre vie: ordine, ricevimento e fattura fornitore.

La regola propone e non chiude. ``require_all_criteria`` è lo stesso
principio dell'abbinamento incassi: con la soglia a 1 un criterio falso
non diventa MATCH_OK. Quantità oltre il ricevuto, ricevimento assente,
ordine assente e duplicato non passano mai, qualunque sia la soglia.

Il prezzo usa la soglia ``min(price_rate × importo atteso della riga, price_abs)``,
estremi inclusi, contro il prezzo dell'ordine. I calcoli restano in Decimal
e si arrotondano con ROUND_HALF_UP solo al confronto, al centesimo.
Imposta e totale controllano la fattura: imponibile fatturato per aliquota,
e somma delle righe più imposta contro l'importo.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import psycopg
from psycopg.types.json import Json

from ontology.loader import Ontology
from services.audit import record
from services.cash.events import record_process_event
from services.companies import USER_WITHOUT_COMPANY
from services.disposition import MATCH3_DECIDABLE, card_disposition
from services.errors import AppError
from services.evidence import evidence_hmac
from services.mapping.corrections import company_id_for_user
from services.ontology_view import catalog_pair, type_label
from services.purchasing.tolerances import current_tolerances
from services.text import stable

RULE = "match3.baseline@v1"
SCORE_KIND = "punteggio di regola, non probabilità"
PROCESS_ID = "procure_to_pay"
STEP_ID = "three_way"
CRITERIA = ("unique", "order", "quantity", "price", "vat", "total")
_HARD = ("unique", "order", "quantity")
_OPEN = "received"
_CENT = Decimal("0.01")
_HUNDRED = Decimal("100")
_OUTCOMES = (
    "MATCH_OK",
    "QTY_NOT_RECEIVED",
    "QTY_OVER",
    "PRICE_OUT_OF_TOL",
    "VAT_MISMATCH",
    "TOTAL_MISMATCH",
    "NO_PO",
    "DUPLICATE_INVOICE",
)


@dataclass(frozen=True)
class Match3Policy:
    """Stesso interruttore dell'abbinamento incassi. La soglia parte da 1."""

    rule: str = RULE
    score_kind: str = SCORE_KIND
    require_all_criteria: bool = True
    threshold: float = 1.0


@dataclass
class WayLine:
    product: str
    quantity: Decimal
    unit_price: Decimal
    line_amount: Decimal | None = None
    vat_rate: Decimal = Decimal("0")


@dataclass
class WayDocument:
    lines: list[WayLine] = field(default_factory=list)
    total: Decimal = Decimal("0")
    number: str = ""
    declared_tax: dict[str, Decimal] | None = None


@dataclass
class _OrderProduct:
    quantity: Decimal
    unit_price: Decimal
    vat_rate: Decimal
    price_conflict: bool
    rate_conflict: bool


@dataclass
class Match3Result:
    outcome: str
    criteria: dict[str, bool]
    failed: list[str]
    score: float
    comparison: dict[str, Any]


def default_policy() -> Match3Policy:
    return Match3Policy()


def cents(value: Decimal) -> Decimal:
    """Round half up to the cent. Call this only at the comparison."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def rule_score(criteria: dict[str, bool]) -> float:
    hits = sum(1 for key in CRITERIA if criteria.get(key))
    if hits == len(CRITERIA):
        return 1.0
    return hits / len(CRITERIA)


def _accepted(criteria: dict[str, bool], policy: Match3Policy) -> bool:
    score = rule_score(criteria)
    if score < policy.threshold:
        return False
    if any(not criteria.get(key) for key in _HARD):
        return False
    if policy.require_all_criteria and not all(criteria.get(key) for key in CRITERIA):
        return False
    return True


def _money(value: Decimal) -> str:
    return format(cents(value), "f")


def _qty(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _rate_key(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _line_amount(line: WayLine) -> Decimal:
    if line.line_amount is not None:
        return line.line_amount
    return line.unit_price * line.quantity


def _by_product(lines: list[WayLine]) -> dict[str, _OrderProduct]:
    """Sum the order quantity of one article. Different prices are a conflict."""
    found: dict[str, _OrderProduct] = {}
    for line in lines:
        current = found.get(line.product)
        if current is None:
            found[line.product] = _OrderProduct(
                line.quantity, line.unit_price, line.vat_rate, False, False
            )
            continue
        found[line.product] = _OrderProduct(
            current.quantity + line.quantity,
            current.unit_price,
            current.vat_rate,
            current.price_conflict or current.unit_price != line.unit_price,
            current.rate_conflict or current.vat_rate != line.vat_rate,
        )
    return found


def _received(lines: list[WayLine]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for line in lines:
        totals[line.product] = totals.get(line.product, Decimal("0")) + line.quantity
    return totals


def evaluate(
    invoice: WayDocument,
    order: WayDocument | None,
    receipts: list[WayLine],
    tolerances: dict[str, Any],
    *,
    duplicate: bool = False,
    already_invoiced: dict[str, Decimal] | None = None,
    policy: Match3Policy | None = None,
) -> Match3Result:
    """One outcome for the invoice. Nothing is written and nothing is closed.

    Price stays on the order, line by line. Tax and total check the invoice
    itself. Quantity is the sum per article, after earlier invoices on the
    same order. A receipt shorter than the order is not MATCH_OK when
    ``partial_receipt`` is false.
    """
    chosen = policy or default_policy()
    rate = Decimal(tolerances["price_rate"])
    price_abs = Decimal(tolerances["price_abs"])
    vat_abs = Decimal(tolerances["vat_abs"])
    total_abs = Decimal(tolerances["total_abs"])
    partial_ok = bool(tolerances.get("partial_receipt", True))
    order_lines = _by_product(order.lines) if order is not None else {}
    received = _received(receipts)
    prior = already_invoiced or {}
    invoiced_qty = _received(invoice.lines)
    quantity_missing = False
    quantity_over = False
    quantity_short = False
    available: dict[str, Decimal] = {}
    for product, qty in invoiced_qty.items():
        ordered = order_lines.get(product)
        if product not in received:
            quantity_missing = True
            available[product] = Decimal("0")
        else:
            remaining = received[product] - prior.get(product, Decimal("0"))
            available[product] = remaining
            if qty > remaining:
                quantity_over = True
            if ordered is not None and not partial_ok and received[product] < ordered.quantity:
                quantity_short = True
    price_fail = False
    rate_mismatch = False
    price_conflicts: list[str] = []
    bases: dict[str, Decimal] = {}
    shown_lines = []
    for line in invoice.lines:
        ordered = order_lines.get(line.product)
        got_qty = available.get(line.product, Decimal("0"))
        actual = _line_amount(line)
        if ordered is None:
            expected_line = Decimal("0")
            threshold = Decimal("0")
            if order is not None:
                price_fail = True
            order_rate = line.vat_rate
        else:
            expected_line = ordered.unit_price * line.quantity
            threshold = min(rate * expected_line, price_abs)
            if ordered.price_conflict:
                price_fail = True
                if line.product not in price_conflicts:
                    price_conflicts.append(line.product)
            elif cents(abs(actual - expected_line)) > cents(threshold):
                price_fail = True
            order_rate = ordered.vat_rate
            if ordered.rate_conflict or line.vat_rate != order_rate:
                rate_mismatch = True
        bucket = _rate_key(line.vat_rate)
        bases[bucket] = bases.get(bucket, Decimal("0")) + actual
        ordered_qty = ordered.quantity if ordered is not None else Decimal("0")
        shown_lines.append(
            {
                "product": line.product,
                "ordered_qty": _qty(ordered_qty),
                "received_qty": _qty(got_qty),
                "invoiced_qty": _qty(line.quantity),
                "expected_line": _money(expected_line),
                "invoiced_line": _money(actual),
                "threshold": _money(threshold),
                "vat_rate": _rate_key(line.vat_rate),
                "order_vat_rate": _rate_key(order_rate),
            }
        )
    computed_tax = {key: base * Decimal(key) / _HUNDRED for key, base in bases.items()}
    tax_fail = rate_mismatch or order is None
    if order is not None and invoice.declared_tax is not None:
        declared = _declared(invoice.declared_tax)
        for key in set(computed_tax) | set(declared):
            left = computed_tax.get(key, Decimal("0"))
            right = declared.get(key, Decimal("0"))
            if cents(abs(left - right)) > cents(vat_abs):
                tax_fail = True
    expected_total = sum(bases.values(), Decimal("0")) + sum(computed_tax.values(), Decimal("0"))
    total_fail = order is None or cents(abs(invoice.total - expected_total)) > cents(total_abs)
    invoice_tax = computed_tax
    criteria = {
        "unique": not duplicate,
        "order": order is not None,
        "quantity": not quantity_missing and not quantity_over and not quantity_short,
        "price": order is not None and not price_fail,
        "vat": order is not None and not tax_fail,
        "total": not total_fail,
    }
    if quantity_missing:
        quantity_code = "QTY_NOT_RECEIVED"
    elif quantity_over:
        quantity_code = "QTY_OVER"
    else:
        quantity_code = "QTY_NOT_RECEIVED"
    outcome = _outcome(criteria, quantity_code, chosen)
    comparison = {
        "lines": shown_lines,
        "expected_tax": [_tax_row(key, amount) for key, amount in sorted(computed_tax.items())],
        "invoice_tax": [_tax_row(key, amount) for key, amount in sorted(computed_tax.items())],
        "expected_total": _money(expected_total),
        "invoice_total": _money(invoice.total),
        "total_tolerance": _money(total_abs),
        "vat_tolerance": _money(vat_abs),
    }
    if price_conflicts:
        comparison["price_conflicts"] = price_conflicts
    failed = [key for key in CRITERIA if not criteria[key]]
    return Match3Result(
        outcome=outcome,
        criteria=criteria,
        failed=failed,
        score=rule_score(criteria),
        comparison=comparison,
    )


def _tax_row(rate: str, amount: Decimal) -> dict[str, str]:
    return {"rate": rate, "amount": _money(amount)}


def _declared(raw: dict[str, Decimal]) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}
    for key, value in raw.items():
        found[_rate_key(Decimal(str(key)))] = Decimal(str(value))
    return found


def _outcome(criteria: dict[str, bool], quantity_code: str, policy: Match3Policy) -> str:
    if not criteria["unique"]:
        return "DUPLICATE_INVOICE"
    if not criteria["order"]:
        return "NO_PO"
    if not criteria["quantity"]:
        return quantity_code
    if _accepted(criteria, policy):
        return "MATCH_OK"
    if not criteria["price"]:
        return "PRICE_OUT_OF_TOL"
    if not criteria["vat"]:
        return "VAT_MISMATCH"
    if not criteria["total"]:
        return "TOTAL_MISMATCH"
    return "MATCH_OK"


def propose_company(
    conn: psycopg.Connection,
    ontology: Ontology,
    company_id: str,
    *,
    policy: Match3Policy | None = None,
) -> list[str]:
    """Store one proposal per supplier invoice that does not have one yet.

    The invoice status stays as it was. Only a synthetic invoice is proposed.
    """
    if not company_id:
        raise AppError(422, "Tre vie senza azienda")
    chosen = policy or default_policy()
    tolerances = current_tolerances(conn, company_id)
    rows = conn.execute(
        """
        SELECT id::text, business_key
        FROM app.entities
        WHERE type = 'SupplierInvoice' AND valid_to IS NULL AND company_id = %s::uuid
        ORDER BY business_key
        """,
        (company_id,),
    ).fetchall()
    created = []
    for row in rows:
        existing = conn.execute(
            """
            SELECT id::text FROM app.match3_proposals
            WHERE company_id = %s::uuid AND supplier_invoice_entity_id = %s::uuid
            """,
            (company_id, row["id"]),
        ).fetchone()
        if existing is not None:
            continue
        proposal_id = _store_proposal(conn, ontology, company_id, row, tolerances, chosen)
        created.append(proposal_id)
    return created


def _store_proposal(conn, ontology: Ontology, company_id: str, row, tolerances, policy: Match3Policy) -> str:
    invoice_facts = _facts(conn, row["id"])
    number = invoice_facts.get("invoice_number") or ""
    order_key = invoice_facts.get("order_source_key") or ""
    order_id = _find(conn, company_id, "PurchaseOrder", order_key) if order_key else None
    order = _order(conn, company_id, order_id, order_key) if order_id else None
    receipts = _receipts(conn, company_id, order_key) if order_key else []
    invoice = WayDocument(
        lines=_invoice_lines(conn, company_id, row["business_key"]),
        total=_decimal(invoice_facts.get("amount")),
        number=number,
    )
    duplicate = _duplicate(conn, company_id, row["business_key"], row["id"])
    prior = _prior_invoiced(conn, company_id, order_key, row["id"], invoice_facts) if order_key else {}
    result = evaluate(
        invoice,
        order,
        receipts,
        tolerances,
        duplicate=duplicate,
        already_invoiced=prior,
        policy=policy,
    )
    if result.outcome not in _OUTCOMES:
        raise AppError(500, "Esito delle tre vie sconosciuto")
    entity_ids = [row["id"]]
    if order_id:
        entity_ids.append(order_id)
    fact_ids = _fact_ids(conn, entity_ids)
    digest = evidence_hmac(conn, fact_ids)
    source = _source_name(conn, row["id"])
    proposal_id = str(uuid.uuid4())
    evidence = {"facts": fact_ids, "comparison": result.comparison}
    conn.execute(
        """
        INSERT INTO app.match3_proposals (
          id, company_id, supplier_invoice_entity_id, purchase_order_entity_id, outcome,
          source_name, label_origin, criteria, score, confidence_method, confidence_rule,
          evidence, evidence_hash, process_id, step_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'synthetic', %s, %s, 'rule', %s, %s, %s, %s, %s)
        """,
        (
            proposal_id,
            company_id,
            row["id"],
            order_id,
            result.outcome,
            source,
            Json(result.criteria),
            result.score,
            policy.rule,
            Json(evidence),
            digest,
            PROCESS_ID,
            STEP_ID,
        ),
    )
    issued = invoice_facts.get("issued_at") or "proposta"
    record_process_event(
        conn,
        ontology,
        company_id=company_id,
        subject_id=row["id"],
        event_type="three_way_proposed",
        summary="Tre vie proposte, fattura non chiusa",
        occurred_at=issued,
        process_id=PROCESS_ID,
        step_id=STEP_ID,
        source=source,
        source_class="external",
    )
    return proposal_id


def confirm_proposal(
    conn: psycopg.Connection,
    ontology: Ontology,
    proposal_id: str,
    *,
    actor: str,
    role: str,
    evidence_hash: str,
) -> dict[str, Any]:
    company_id = _company(conn, actor)
    proposal = _proposal(conn, proposal_id, company_id)
    _fresh(proposal, evidence_hash)
    _refuse_if_decided(conn, proposal_id)
    decision_id = _insert(
        conn,
        proposal,
        decision="confirm",
        actor=actor,
        role=role,
        company_id=company_id,
        failed=[],
        reason="",
    )
    _event(conn, ontology, proposal, company_id, "three_way_confirmed", "Tre vie confermate da una persona")
    _audit(conn, actor, role, decision_id, "confirm", proposal_id)
    _assert_still_open(conn, proposal["supplier_invoice_entity_id"])
    return {"id": decision_id, "decision": "confirm", "failed_criteria": []}


def correct_proposal(
    conn: psycopg.Connection,
    ontology: Ontology,
    proposal_id: str,
    *,
    actor: str,
    role: str,
    evidence_hash: str,
    reason: str,
) -> dict[str, Any]:
    company_id = _company(conn, actor)
    proposal = _proposal(conn, proposal_id, company_id)
    _fresh(proposal, evidence_hash)
    _refuse_if_decided(conn, proposal_id)
    motive = _motive(reason)
    failed = list(proposal["failed"])
    decision_id = _insert(
        conn,
        proposal,
        decision="correct",
        actor=actor,
        role=role,
        company_id=company_id,
        failed=failed,
        reason=motive,
    )
    _event(conn, ontology, proposal, company_id, "three_way_corrected", "Eccezione corretta da una persona")
    _audit(conn, actor, role, decision_id, "correct", proposal_id)
    _assert_still_open(conn, proposal["supplier_invoice_entity_id"])
    return {"id": decision_id, "decision": "correct", "failed_criteria": failed}


def reject_proposal(
    conn: psycopg.Connection,
    ontology: Ontology,
    proposal_id: str,
    *,
    actor: str,
    role: str,
    evidence_hash: str,
    reason: str,
) -> dict[str, Any]:
    company_id = _company(conn, actor)
    proposal = _proposal(conn, proposal_id, company_id)
    _fresh(proposal, evidence_hash)
    _refuse_if_decided(conn, proposal_id)
    motive = _motive(reason)
    failed = list(proposal["failed"])
    decision_id = _insert(
        conn,
        proposal,
        decision="reject",
        actor=actor,
        role=role,
        company_id=company_id,
        failed=failed,
        reason=motive,
    )
    _event(conn, ontology, proposal, company_id, "three_way_rejected", "Eccezione rifiutata da una persona")
    _audit(conn, actor, role, decision_id, "reject", proposal_id)
    _assert_still_open(conn, proposal["supplier_invoice_entity_id"])
    return {"id": decision_id, "decision": "reject", "failed_criteria": failed}


def revoke_decision(
    conn: psycopg.Connection,
    decision_id: str,
    *,
    actor: str,
    role: str,
    reason: str,
) -> dict[str, Any]:
    company_id = _company(conn, actor)
    motive = _motive(reason)
    row = conn.execute(
        """
        SELECT id::text, company_id::text AS company_id, decision
        FROM app.match3_decisions
        WHERE id = %s::uuid
        """,
        (decision_id,),
    ).fetchone()
    if row is None or row["company_id"] != company_id:
        raise AppError(404, "Decisione assente")
    if row["decision"] == "revoked":
        raise AppError(409, "La revoca non si revoca")
    new_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.match3_decisions (
          id, proposal_id, supplier_invoice_entity_id, company_id, decision, actor, actor_role,
          reason, revokes_decision_id, source_name
        )
        SELECT %s, proposal_id, supplier_invoice_entity_id, company_id, 'revoked', %s, %s,
               %s, id, source_name
        FROM app.match3_decisions
        WHERE id = %s::uuid
        """,
        (new_id, actor, role, motive, decision_id),
    )
    _audit(conn, actor, role, new_id, "revoked", decision_id)
    return {"id": new_id, "decision": "revoked", "revokes": decision_id}


def for_invoice(conn: psycopg.Connection, invoice_id: str, company_id: str, model: Ontology) -> dict[str, Any] | None:
    """The latest proposal on this supplier invoice, or nothing."""
    row = conn.execute(
        """
        SELECT id::text
        FROM app.match3_proposals
        WHERE company_id = %s::uuid AND supplier_invoice_entity_id = %s::uuid
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (company_id, invoice_id),
    ).fetchone()
    if row is None:
        return None
    cards = inbox_cards(conn, company_id, model)
    return next((item for item in cards if item["id"] == row["id"]), None)


def inbox_cards(conn: psycopg.Connection, company_id: str, model: Ontology) -> list[dict[str, Any]]:
    """Proposals of this company. Another company is not in the result."""
    rows = conn.execute(
        """
        SELECT id::text, supplier_invoice_entity_id::text, outcome, criteria, score,
               confidence_rule, evidence, evidence_hash, source_name, process_id, step_id, created_at
        FROM app.match3_proposals
        WHERE company_id = %s::uuid
        ORDER BY created_at DESC
        """,
        (company_id,),
    ).fetchall()
    if not rows:
        return []
    decisions = conn.execute(
        """
        SELECT id::text, proposal_id::text, decision, actor, decided_at, failed_criteria, reason,
               revokes_decision_id::text
        FROM app.match3_decisions
        WHERE company_id = %s::uuid
        ORDER BY decided_at, id
        """,
        (company_id,),
    ).fetchall()
    by_proposal: dict[str, list[dict]] = {}
    for row in decisions:
        if row["proposal_id"]:
            by_proposal.setdefault(row["proposal_id"], []).append(dict(row))
    revoked = {row["revokes_decision_id"] for row in decisions if row["revokes_decision_id"]}
    items = []
    for row in rows:
        facts = _facts(conn, row["supplier_invoice_entity_id"])
        number = facts.get("invoice_number") or ""
        process, step = catalog_pair(model, row["process_id"], row["step_id"])
        history = []
        latest = None
        for decision in by_proposal.get(row["id"], []):
            if decision["decision"] == "revoked":
                continue
            history.append(
                {
                    "id": decision["id"],
                    "decision": decision["decision"],
                    "actor": decision["actor"],
                    "decided_at": _when(decision["decided_at"]),
                    "revoked": decision["id"] in revoked,
                    "failed_criteria": decision.get("failed_criteria") or [],
                }
            )
            if decision["id"] not in revoked:
                latest = decision
        status = "proposed"
        criteria = dict(row["criteria"] or {})
        failed = [key for key in CRITERIA if criteria.get(key) is False]
        if latest is not None:
            status = {"confirm": "confirmed", "correct": "corrected", "reject": "rejected"}.get(
                latest["decision"], "proposed"
            )
            failed = list(latest.get("failed_criteria") or [])
        evidence = row["evidence"] if isinstance(row["evidence"], dict) else {}
        comparison = evidence.get("comparison") if isinstance(evidence.get("comparison"), dict) else {}
        items.append(
            {
                "id": row["id"],
                "kind": "three_way",
                "invoice_id": row["supplier_invoice_entity_id"],
                "invoice_number": number,
                "headline": f"{type_label('SupplierInvoice')} {number}".strip(),
                "problem": "Proposta delle tre vie. La fattura fornitore non viene chiusa.",
                "outcome": row["outcome"],
                "comparison": comparison,
                "criteria": {key: bool(criteria.get(key)) for key in CRITERIA},
                "failed_criteria": failed,
                "evidence": [],
                "evidence_hash": row["evidence_hash"],
                "actions": [],
                "expected_impact": "La fattura fornitore resta aperta finché una persona non decide.",
                "risk": "low",
                "confidence": stable(row["score"]),
                "confidence_method": "rule",
                "confidence_rule": row["confidence_rule"],
                "status": status,
                "requires_cfo": False,
                "created_at": _when(row["created_at"]),
                "blocked_reason": None,
                "preview": {"formula": "La regola non chiude la fattura fornitore.", "payloads": []},
                "dry_run": None,
                "history": history,
                "executed": False,
                "executed_message": "",
                "process": process,
                "step": step,
                "disposition": card_disposition(status, MATCH3_DECIDABLE),
            }
        )
    return items


def _company(conn, actor: str) -> str:
    company_id = company_id_for_user(conn, actor)
    if not company_id:
        raise AppError(403, USER_WITHOUT_COMPANY)
    return company_id


def _proposal(conn, proposal_id: str, company_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id::text, company_id::text AS company_id, supplier_invoice_entity_id::text,
               outcome, source_name, evidence_hash, criteria, label_origin
        FROM app.match3_proposals
        WHERE id = %s::uuid
        """,
        (proposal_id,),
    ).fetchone()
    if row is None or row["company_id"] != company_id:
        raise AppError(404, "Proposta assente")
    criteria = dict(row["criteria"] or {})
    found = dict(row)
    found["failed"] = [key for key in CRITERIA if criteria.get(key) is False]
    return found


def _fresh(proposal: dict[str, Any], evidence_hash: str) -> None:
    if evidence_hash != proposal["evidence_hash"]:
        raise AppError(409, "Le evidenze sono cambiate")


def _refuse_if_decided(conn, proposal_id: str) -> None:
    row = conn.execute(
        """
        SELECT d.decision
        FROM app.match3_decisions d
        WHERE d.proposal_id = %s::uuid
          AND d.decision <> 'revoked'
          AND NOT EXISTS (
            SELECT 1 FROM app.match3_decisions later
            WHERE later.revokes_decision_id = d.id
          )
        ORDER BY d.decided_at DESC, d.id DESC
        LIMIT 1
        """,
        (proposal_id,),
    ).fetchone()
    if row is not None:
        raise AppError(409, "La proposta ha già una decisione")


def _motive(reason: str) -> str:
    text = (reason or "").strip()
    if len(text) < 5:
        raise AppError(422, "Scrivi un motivo di almeno 5 caratteri")
    return text


def _insert(conn, proposal, *, decision, actor, role, company_id, failed, reason) -> str:
    decision_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO app.match3_decisions (
          id, proposal_id, supplier_invoice_entity_id, company_id, decision, actor, actor_role,
          failed_criteria, reason, source_name
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            decision_id,
            proposal["id"],
            proposal["supplier_invoice_entity_id"],
            company_id,
            decision,
            actor,
            role,
            Json(failed),
            reason,
            proposal["source_name"],
        ),
    )
    return decision_id


def _event(conn, ontology, proposal, company_id, event_type, summary) -> None:
    facts = _facts(conn, proposal["supplier_invoice_entity_id"])
    record_process_event(
        conn,
        ontology,
        company_id=company_id,
        subject_id=proposal["supplier_invoice_entity_id"],
        event_type=event_type,
        summary=summary,
        occurred_at=facts.get("issued_at") or "decisione",
        process_id=PROCESS_ID,
        step_id=STEP_ID,
        source=proposal["source_name"],
        source_class="manual",
    )


def _audit(conn, actor, role, decision_id, decision, proposal_id) -> None:
    record(
        conn,
        action="human_decision",
        actor=actor,
        role=role,
        entity_ref=decision_id,
        payload={
            "decision": decision,
            "proposal_id": proposal_id,
            "match3_decision_id": decision_id,
        },
    )


def _assert_still_open(conn, invoice_id: str) -> None:
    facts = _facts(conn, invoice_id)
    if facts.get("status") not in (None, "", _OPEN):
        raise AppError(409, "La fattura fornitore non si chiude")


def _facts(conn, entity_id: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT property, value #>> '{}' AS text
        FROM app.facts
        WHERE entity_id = %s::uuid AND valid_to IS NULL
        """,
        (entity_id,),
    ).fetchall()
    return {row["property"]: row["text"] or "" for row in rows}


def _decimal(value: str | None) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(value)


def _find(conn, company_id: str, type_name: str, key: str) -> str | None:
    if not key:
        return None
    row = conn.execute(
        """
        SELECT id::text FROM app.entities
        WHERE type = %s AND business_key = %s AND valid_to IS NULL AND company_id = %s::uuid
        """,
        (type_name, key, company_id),
    ).fetchone()
    return None if row is None else row["id"]


def _prior_invoiced(
    conn,
    company_id: str,
    order_key: str,
    invoice_id: str,
    invoice_facts: dict[str, str],
) -> dict[str, Decimal]:
    """Quantity already billed on this order by an earlier invoice that was not rejected."""
    rows = conn.execute(
        """
        SELECT e.id::text, e.business_key
        FROM app.entities e
        JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL AND f.property = 'order_source_key'
        WHERE e.type = 'SupplierInvoice' AND e.valid_to IS NULL AND e.company_id = %s::uuid
          AND f.value #>> '{}' = %s AND e.id <> %s::uuid
        """,
        (company_id, order_key, invoice_id),
    ).fetchall()
    current = _stamp(invoice_facts, invoice_id)
    totals: dict[str, Decimal] = {}
    for row in rows:
        facts = _facts(conn, row["id"])
        if _stamp(facts, row["id"]) >= current:
            continue
        if _rejected(conn, row["id"]):
            continue
        for line in _invoice_lines(conn, company_id, row["business_key"]):
            totals[line.product] = totals.get(line.product, Decimal("0")) + line.quantity
    return totals


def _stamp(facts: dict[str, str], entity_id: str) -> tuple[str, str, str]:
    return (facts.get("issued_at") or "", facts.get("invoice_number") or "", entity_id)


def _rejected(conn, invoice_id: str) -> bool:
    row = conn.execute(
        """
        SELECT d.decision
        FROM app.match3_decisions d
        JOIN app.match3_proposals p ON p.id = d.proposal_id
        WHERE p.supplier_invoice_entity_id = %s::uuid
          AND d.decision <> 'revoked'
          AND NOT EXISTS (
            SELECT 1 FROM app.match3_decisions later
            WHERE later.revokes_decision_id = d.id
          )
        ORDER BY d.decided_at DESC, d.id DESC
        LIMIT 1
        """,
        (invoice_id,),
    ).fetchone()
    return row is not None and row["decision"] == "reject"


def _duplicate(conn, company_id: str, key: str, invoice_id: str) -> bool:
    row = conn.execute(
        """
        SELECT count(*) AS n FROM app.entities
        WHERE type = 'SupplierInvoice' AND business_key = %s AND valid_to IS NULL
          AND company_id = %s::uuid AND id <> %s::uuid
        """,
        (key, company_id, invoice_id),
    ).fetchone()
    return int(row["n"]) > 0


def _order(conn, company_id: str, order_id: str, order_key: str) -> WayDocument:
    lines = []
    for entity_id in _children(conn, company_id, "PurchaseOrderLine", "order_source_key", order_key):
        facts = _facts(conn, entity_id)
        lines.append(_way_line(facts, amount_name="line_amount"))
    return WayDocument(lines=lines)


def _invoice_lines(conn, company_id: str, invoice_key: str) -> list[WayLine]:
    lines = []
    for entity_id in _children(conn, company_id, "SupplierInvoiceLine", "supplier_invoice_source_key", invoice_key):
        lines.append(_way_line(_facts(conn, entity_id), amount_name="line_amount"))
    return lines


def _receipts(conn, company_id: str, order_key: str) -> list[WayLine]:
    lines = []
    receipts = conn.execute(
        """
        SELECT id::text, business_key
        FROM app.entities
        WHERE type = 'GoodsReceipt' AND valid_to IS NULL AND company_id = %s::uuid
        """,
        (company_id,),
    ).fetchall()
    keys = []
    for receipt in receipts:
        facts = _facts(conn, receipt["id"])
        if facts.get("order_source_key") == order_key:
            keys.append(receipt["business_key"])
    for key in keys:
        for entity_id in _children(conn, company_id, "GoodsReceiptLine", "receipt_source_key", key):
            facts = _facts(conn, entity_id)
            lines.append(
                WayLine(
                    product=facts.get("product_source_key") or facts.get("source_key") or entity_id,
                    quantity=_decimal(facts.get("quantity")),
                    unit_price=Decimal("0"),
                )
            )
    return lines


def _children(conn, company_id: str, type_name: str, fact_name: str, fact_value: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT e.id::text
        FROM app.entities e
        JOIN app.facts f ON f.entity_id = e.id AND f.valid_to IS NULL AND f.property = %s
        WHERE e.type = %s AND e.valid_to IS NULL AND e.company_id = %s::uuid
          AND f.value #>> '{}' = %s
        ORDER BY e.business_key
        """,
        (fact_name, type_name, company_id, fact_value),
    ).fetchall()
    return [row["id"] for row in rows]


def _way_line(facts: dict[str, str], *, amount_name: str) -> WayLine:
    amount = facts.get(amount_name)
    return WayLine(
        product=facts.get("product_source_key") or facts.get("source_key") or "",
        quantity=_decimal(facts.get("quantity")),
        unit_price=_decimal(facts.get("unit_price")),
        line_amount=None if amount in (None, "") else _decimal(amount),
        vat_rate=_decimal(facts.get("vat_rate")),
    )


def _fact_ids(conn, entity_ids: list[str]) -> list[str]:
    rows = conn.execute(
        """
        SELECT id::text FROM app.facts
        WHERE entity_id = ANY(%s::uuid[]) AND valid_to IS NULL
        ORDER BY id
        """,
        (entity_ids,),
    ).fetchall()
    if not rows:
        raise AppError(422, "Evidenze assenti")
    return [row["id"] for row in rows]


def _source_name(conn, entity_id: str) -> str:
    row = conn.execute(
        """
        SELECT source FROM app.facts
        WHERE entity_id = %s::uuid AND valid_to IS NULL AND source IS NOT NULL
        LIMIT 1
        """,
        (entity_id,),
    ).fetchone()
    if row is None or not row["source"]:
        return "tre_vie"
    return str(row["source"])


def _when(value) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
