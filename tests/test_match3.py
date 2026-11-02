"""Tre vie: esiti, tolleranze, decisioni e scheda.

La regola propone e non chiude. Saldo e misura degli incassi restano quelli di prima.
"""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from ontology.loader import load_merged
from services.agent.payment_measure import measure_payment_matches
from services.canonical.store import _ensure_entity, _write_facts
from services.cash.balance import remaining_balance
from services.cash.documents import write_invoice
from services.db import app_connect, connect
from services.errors import AppError
from services.graph.projection import project_graph
from services.migrate import migration_violation
from services.ontology_view import describe_ontology
from services.purchasing.invoices import write_supplier_invoice
from services.purchasing.match3 import (
    Match3Policy,
    WayDocument,
    WayLine,
    default_policy,
    evaluate,
)
from services.purchasing.tolerances import record_tolerances
from tests.test_flow import give_company, login
from tests.test_order_to_cash import _assign

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _tolerances(**overrides):
    base = {
        "price_rate": Decimal("0.01"),
        "price_abs": Decimal("5"),
        "vat_abs": Decimal("0.01"),
        "total_abs": Decimal("0.05"),
    }
    base.update(overrides)
    return base


def _line(product, qty, unit, rate="22", amount=None):
    return WayLine(
        product=product,
        quantity=Decimal(str(qty)),
        unit_price=Decimal(str(unit)),
        line_amount=None if amount is None else Decimal(str(amount)),
        vat_rate=Decimal(str(rate)),
    )


def _invoice(lines, total, number="FT-1", declared_tax=None):
    tax = None
    if declared_tax is not None:
        tax = {str(key): Decimal(str(value)) for key, value in declared_tax.items()}
    return WayDocument(lines=lines, total=Decimal(str(total)), number=number, declared_tax=tax)


def _order(lines):
    return WayDocument(lines=lines)


def _matched(**kwargs):
    lines = kwargs.pop("lines", None)
    if lines is None:
        lines = [_line("ART-1", "10", "10", amount="100")]
    declared_tax = kwargs.pop("declared_tax", None)
    already = kwargs.pop("already", None)
    invoice = kwargs.pop("invoice", None) or _invoice(
        lines, kwargs.pop("total", "122"), declared_tax=declared_tax
    )
    order = kwargs.pop("order", _order([_line("ART-1", "10", "10", amount="100")]))
    receipts = kwargs.pop("receipts", [_line("ART-1", "10", "0", rate="0")])
    duplicate = kwargs.pop("duplicate", False)
    policy = kwargs.pop("policy", None)
    tolerances = _tolerances(**kwargs)
    return evaluate(
        invoice,
        order,
        receipts,
        tolerances,
        duplicate=duplicate,
        already_invoiced=already,
        policy=policy,
    )


def test_each_outcome_and_the_tolerance_edges():
    assert default_policy().require_all_criteria is True
    assert default_policy().threshold == 1
    ok = _matched()
    assert ok.outcome == "MATCH_OK"
    assert ok.failed == []
    assert ok.score == 1
    assert _matched(receipts=[]).outcome == "QTY_NOT_RECEIVED"
    assert _matched(receipts=[_line("ART-1", "4", "0", rate="0")], lines=[_line("ART-1", "5", "10", amount="50")], total="122").outcome == "QTY_OVER"
    assert _matched(order=None).outcome == "NO_PO"
    assert _matched(duplicate=True).outcome == "DUPLICATE_INVOICE"
    assert evaluate(
        _invoice([_line("ART-1", "5", "10", amount="50")], "122"),
        None,
        [],
        _tolerances(),
        duplicate=True,
    ).outcome == "DUPLICATE_INVOICE"
    over = _matched(
        receipts=[_line("ART-1", "4", "0", rate="0")],
        lines=[_line("ART-1", "5", "10", amount="50")],
        policy=Match3Policy(require_all_criteria=False, threshold=0),
    )
    assert over.outcome == "QTY_OVER"

    on_threshold = _matched(
        lines=[_line("ART-1", "1", "1000", rate="0", amount="1005")],
        order=_order([_line("ART-1", "1", "1000", rate="0", amount="1000")]),
        receipts=[_line("ART-1", "1", "0", rate="0")],
        total="1005",
    )
    assert on_threshold.outcome == "MATCH_OK"
    above = _matched(
        lines=[_line("ART-1", "1", "1000", rate="0", amount="1005.01")],
        order=_order([_line("ART-1", "1", "1000", rate="0", amount="1000")]),
        receipts=[_line("ART-1", "1", "0", rate="0")],
        total="1005.01",
    )
    assert above.outcome == "PRICE_OUT_OF_TOL"
    small_on = _matched(
        lines=[_line("ART-1", "1", "100", rate="0", amount="101")],
        order=_order([_line("ART-1", "1", "100", rate="0", amount="100")]),
        receipts=[_line("ART-1", "1", "0", rate="0")],
        total="101",
    )
    assert small_on.outcome == "MATCH_OK"
    small_above = _matched(
        lines=[_line("ART-1", "1", "100", rate="0", amount="101.01")],
        order=_order([_line("ART-1", "1", "100", rate="0", amount="100")]),
        receipts=[_line("ART-1", "1", "0", rate="0")],
        total="101.01",
    )
    assert small_above.outcome == "PRICE_OUT_OF_TOL"
    rounded_ok = _matched(
        lines=[_line("ART-1", "1", "33.335", rate="0", amount="33.669")],
        order=_order([_line("ART-1", "1", "33.335", rate="0", amount="33.335")]),
        receipts=[_line("ART-1", "1", "0", rate="0")],
        total="33.669",
    )
    assert rounded_ok.outcome == "MATCH_OK"
    rounded_over = _matched(
        lines=[_line("ART-1", "1", "33.335", rate="0", amount="33.670")],
        order=_order([_line("ART-1", "1", "33.335", rate="0", amount="33.335")]),
        receipts=[_line("ART-1", "1", "0", rate="0")],
        total="33.670",
    )
    assert rounded_over.outcome == "PRICE_OUT_OF_TOL"

    total_on = _matched(total="122.05")
    assert total_on.outcome == "MATCH_OK"
    total_over = _matched(total="122.06")
    assert total_over.outcome == "TOTAL_MISMATCH"

    partial = _matched(
        lines=[_line("ART-1", "4", "10", amount="40")],
        receipts=[_line("ART-1", "6", "0", rate="0")],
        total="48.80",
    )
    assert partial.outcome == "MATCH_OK"
    assert partial.comparison["lines"][0]["invoiced_qty"] == "4"
    assert partial.comparison["lines"][0]["received_qty"] == "6"


def test_vat_is_compared_by_rate_across_lines():
    order_lines = [
        _line("ART-1", "1", "100", amount="100"),
        _line("ART-2", "1", "100", amount="100"),
    ]
    receipts = [_line("ART-1", "1", "0", rate="0"), _line("ART-2", "1", "0", rate="0")]
    summed = _matched(
        lines=[
            _line("ART-1", "1", "100", amount="100.04"),
            _line("ART-2", "1", "100", amount="100.04"),
        ],
        order=_order(order_lines),
        receipts=receipts,
        total="244.0976",
        declared_tax={"22": "44.00"},
    )
    assert summed.outcome == "VAT_MISMATCH"
    assert summed.criteria["price"] is True
    assert summed.criteria["total"] is True
    offset = _matched(
        lines=[
            _line("ART-1", "1", "100", amount="100.10"),
            _line("ART-2", "1", "100", amount="99.90"),
        ],
        order=_order(order_lines),
        receipts=receipts,
        total="244",
    )
    assert offset.outcome == "MATCH_OK"
    rate = _matched(
        lines=[_line("ART-1", "10", "10", rate="10", amount="100")],
        total="110",
    )
    assert rate.outcome == "VAT_MISMATCH"
    assert rate.comparison["invoice_tax"] == []
    compared = rate.comparison["lines"][0]
    assert compared["vat_rate"] == "10"
    assert compared["order_vat_rate"] == "22"
    declared = summed.comparison["invoice_tax"]
    assert declared == [{"rate": "22", "amount": "44.00"}]


def test_mismatch_card_names_both_rates_and_the_decision():
    result = _matched(
        lines=[_line("ART-1", "12.5", "10", rate="10", amount="125")],
        order=_order([_line("ART-1", "12.5", "10", amount="125")]),
        receipts=[_line("ART-1", "12.5", "0", rate="0")],
        total="137.50",
    )
    assert result.outcome == "VAT_MISMATCH"
    assert result.comparison["invoice_tax"] == []
    line = result.comparison["lines"][0]
    assert line["vat_rate"] == "10"
    assert line["order_vat_rate"] == "22"
    assert line["invoiced_qty"] == "12.5"
    rendered = subprocess.run(
        ["node", "-e", _CARD],
        cwd=ROOT,
        input=json.dumps({"comparison": result.comparison}),
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"


def test_price_within_tolerance_matches_when_the_invoice_is_coherent():
    result = _matched(
        lines=[_line("ART-1", "1", "1000", amount="1005")],
        order=_order([_line("ART-1", "1", "1000", amount="1000")]),
        receipts=[_line("ART-1", "1", "0", rate="0")],
        total="1226.10",
    )
    assert result.outcome == "MATCH_OK"
    assert result.criteria == {
        "unique": True,
        "order": True,
        "quantity": True,
        "price": True,
        "vat": True,
        "total": True,
    }


def test_quantity_is_the_sum_of_the_same_article():
    split = _matched(
        lines=[
            _line("ART-1", "6", "10", amount="60"),
            _line("ART-1", "6", "10", amount="60"),
        ],
        order=_order([
            _line("ART-1", "6", "10", amount="60"),
            _line("ART-1", "6", "10", amount="60"),
        ]),
        receipts=[_line("ART-1", "10", "0", rate="0")],
        total="146.40",
    )
    assert split.outcome == "QTY_OVER"
    summed = _matched(
        lines=[_line("ART-1", "10", "10", amount="100")],
        order=_order([
            _line("ART-1", "4", "10", amount="40"),
            _line("ART-1", "6", "10", amount="60"),
        ]),
        receipts=[_line("ART-1", "10", "0", rate="0")],
    )
    assert summed.outcome == "MATCH_OK"
    assert summed.comparison["lines"][0]["ordered_qty"] == "10"
    conflict = _matched(
        lines=[_line("ART-1", "2", "10", amount="20")],
        order=_order([
            _line("ART-1", "1", "10", amount="10"),
            _line("ART-1", "1", "12", amount="12"),
        ]),
        receipts=[_line("ART-1", "2", "0", rate="0")],
        total="24.40",
    )
    assert conflict.outcome == "PRICE_OUT_OF_TOL"
    assert conflict.comparison["price_conflicts"] == ["ART-1"]


def test_partial_receipt_false_is_not_a_match():
    short = _matched(
        lines=[_line("ART-1", "6", "10", amount="60")],
        receipts=[_line("ART-1", "6", "0", rate="0")],
        total="73.20",
        partial_receipt=False,
    )
    assert short.outcome == "QTY_NOT_RECEIVED"
    allowed = _matched(
        lines=[_line("ART-1", "6", "10", amount="60")],
        receipts=[_line("ART-1", "6", "0", rate="0")],
        total="73.20",
    )
    assert allowed.outcome == "MATCH_OK"


def test_a_later_invoice_for_the_same_goods_is_over_received():
    company = give_company("mapper", "Tre vie seconda fattura")
    model = load_merged()
    with connect() as conn:
        _books(conn, model, company)
        write_supplier_invoice(
            conn,
            model,
            company_id=company,
            supplier_vat="IT99999999999",
            supplier_name="Fornitore Prova",
            number="FT-2",
            issued_at="2026-05-04",
            amount=Decimal("122.00"),
            currency="EUR",
            document_type="TD01",
            label_origin="synthetic",
            source="fatture_fornitori",
            source_class="external",
            order_number="PO-1",
            lines=[
                {
                    "number": "1",
                    "description": "Merce",
                    "product_source_key": "ART-1",
                    "quantity": Decimal("10"),
                    "unit_price": Decimal("10"),
                    "line_amount": Decimal("100.00"),
                    "vat_rate": Decimal("22"),
                }
            ],
        )
        from services.purchasing.match3 import propose_company

        propose_company(conn, model, company)
        conn.commit()
        rows = conn.execute(
            """
            SELECT p.outcome, f.value #>> '{}' AS number
            FROM app.match3_proposals p
            JOIN app.facts f ON f.entity_id = p.supplier_invoice_entity_id
              AND f.valid_to IS NULL AND f.property = 'invoice_number'
            WHERE p.company_id = %s::uuid
            """,
            (company,),
        ).fetchall()
        assert {row["number"]: row["outcome"] for row in rows} == {
            "FT-1": "MATCH_OK",
            "FT-2": "QTY_OVER",
        }


def _write(conn, model, company, type_name, key, values):
    entity_id = _ensure_entity(conn, type_name, key, company)
    _write_facts(
        conn,
        model,
        entity_id,
        type_name,
        values,
        "acquisti",
        {},
        source_classes={name: "external" for name in values},
    )
    return entity_id


def _books(conn, model, company, *, receipt_qty="10", invoice_qty="10", invoice_amount="100", total="122.00", number="FT-1", price_unit="10"):
    _write(
        conn,
        model,
        company,
        "PurchaseOrder",
        "PO-1",
        {"source_key": "PO-1", "status": "approved", "ordered_at": "2026-05-01", "supplier_vat": "IT99999999999"},
    )
    _write(
        conn,
        model,
        company,
        "PurchaseOrderLine",
        "PO-1|1",
        {
            "source_key": "PO-1|1",
            "quantity": Decimal("10"),
            "unit_price": Decimal(price_unit),
            "line_amount": Decimal("100.00"),
            "vat_rate": Decimal("22"),
            "order_source_key": "PO-1",
            "product_source_key": "ART-1",
        },
    )
    _write(
        conn,
        model,
        company,
        "GoodsReceipt",
        "GR-1",
        {"source_key": "GR-1", "status": "received", "received_at": "2026-05-02", "order_source_key": "PO-1"},
    )
    _write(
        conn,
        model,
        company,
        "GoodsReceiptLine",
        "GR-1|1",
        {
            "source_key": "GR-1|1",
            "quantity": Decimal(receipt_qty),
            "receipt_source_key": "GR-1",
            "order_line_source_key": "PO-1|1",
            "product_source_key": "ART-1",
        },
    )
    return write_supplier_invoice(
        conn,
        model,
        company_id=company,
        supplier_vat="IT99999999999",
        supplier_name="Fornitore Prova",
        number=number,
        issued_at="2026-05-03",
        amount=Decimal(total),
        currency="EUR",
        document_type="TD01",
        label_origin="synthetic",
        source="fatture_fornitori",
        source_class="external",
        order_number="PO-1",
        lines=[
            {
                "number": "1",
                "description": "Merce",
                "product_source_key": "ART-1",
                "quantity": Decimal(invoice_qty),
                "unit_price": Decimal(price_unit),
                "line_amount": Decimal(invoice_amount),
                "vat_rate": Decimal("22"),
            }
        ],
    )


def test_tables_reject_update_delete_and_truncate_and_a_short_reason():
    script = (ROOT / "db" / "migrations" / "0024_match3.sql").read_text(encoding="utf-8")
    assert migration_violation(script, "0024_match3.sql") is None
    assert "NOT VALID" in script
    assert "decided_at timestamptz NOT NULL" in script
    assert "company_id uuid NOT NULL" in script
    assert "GRANT SELECT, INSERT ON app.match3_proposals TO ieol_app;" in script
    assert "GRANT SELECT, INSERT ON app.match3_decisions TO ieol_app;" in script
    assert "REVOKE UPDATE, DELETE, TRUNCATE ON app.match3_proposals FROM ieol_app;" in script
    assert "REVOKE UPDATE, DELETE, TRUNCATE ON app.match3_decisions FROM ieol_app;" in script
    for line in script.splitlines():
        code = line.split("--", 1)[0].strip().lower()
        assert not code.startswith("update")
    company = give_company("mapper", "Tre vie tabelle")
    model = load_merged()
    with connect() as conn:
        _books(conn, model, company)
        from services.purchasing.match3 import propose_company

        created = propose_company(conn, model, company)
        conn.commit()
        proposal_id = created[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO app.match3_decisions (
                  id, proposal_id, supplier_invoice_entity_id, company_id, decision, actor, actor_role, reason
                )
                VALUES ('00000000-0000-0000-0000-0000000000b1', %s::uuid,
                        (SELECT supplier_invoice_entity_id FROM app.match3_proposals WHERE id = %s::uuid),
                        %s::uuid, 'reject', 'approver', 'approver', 'no')
                """,
                (proposal_id, proposal_id, company),
            )
        conn.rollback()
        conn.execute(
            """
            INSERT INTO app.match3_decisions (
              id, proposal_id, supplier_invoice_entity_id, company_id, decision, actor, actor_role, reason
            )
            VALUES ('00000000-0000-0000-0000-0000000000b2', %s::uuid,
                    (SELECT supplier_invoice_entity_id FROM app.match3_proposals WHERE id = %s::uuid),
                    %s::uuid, 'confirm', 'approver', 'approver', '')
            """,
            (proposal_id, proposal_id, company),
        )
        conn.commit()
        for table in ("app.match3_proposals", "app.match3_decisions"):
            with pytest.raises(psycopg.Error, match="append-only"):
                conn.execute(f"UPDATE {table} SET source_name = 'x'")
            conn.rollback()
            with pytest.raises(psycopg.Error, match="append-only"):
                conn.execute(f"DELETE FROM {table}")
            conn.rollback()
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute("TRUNCATE app.match3_decisions")
        conn.rollback()
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute("TRUNCATE app.match3_proposals, app.match3_decisions")
        conn.rollback()
    with app_connect() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE app.match3_proposals SET source_name = 'x'")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM app.match3_decisions")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("TRUNCATE app.match3_proposals")
        conn.rollback()


def test_company_isolation_and_cash_measure_stay_put(client: TestClient):
    company = give_company("mapper", "Tre vie Alfa")
    other = give_company("operator", "Tre vie Beta")
    _assign("approver", company)
    model = load_merged()
    with connect() as conn:
        sales = write_invoice(
            conn,
            model,
            company_id=company,
            number="FT-VENDITA",
            document_type="TD01",
            issued_at="2026-04-01",
            amount=Decimal("100.00"),
            currency="EUR",
            customer_name="Cliente Prova",
            customer_vat="IT00000000001",
            order_number=None,
            due_dates=["2026-04-30"],
            lines=[],
            source="fatture",
            source_class="external",
            label_origin="synthetic",
        )
        supplier = _books(conn, model, company)
        _books(conn, model, other, number="FT-ALTRO")
        balance = remaining_balance(conn, company, sales)
        measure = measure_payment_matches(conn)
        edges = [
            row["rel"]
            for row in conn.execute(
                """
                SELECT g.rel
                FROM app.graph_edges g
                JOIN app.entities e ON e.id = g.from_id OR e.id = g.to_id
                WHERE e.type = 'SupplierInvoice' AND g.valid_to IS NULL
                """
            ).fetchall()
        ]
        from services.purchasing.match3 import propose_company

        own = propose_company(conn, model, company)
        other_ids = propose_company(conn, model, other)
        project_graph(conn)
        conn.commit()
        assert len(own) == 1
        assert len(other_ids) == 1
        assert remaining_balance(conn, company, sales) == balance
        assert remaining_balance(conn, company, supplier) is None
        assert measure_payment_matches(conn) == measure
        linked = [
            row["rel"]
            for row in conn.execute(
                """
                SELECT g.rel
                FROM app.graph_edges g
                JOIN app.entities e ON e.id = g.from_id OR e.id = g.to_id
                WHERE e.type = 'SupplierInvoice' AND g.valid_to IS NULL AND g.rel <> 'triggered'
                """
            ).fetchall()
        ]
        assert linked == []
        assert edges == []
        status = conn.execute(
            """
            SELECT value #>> '{}' AS status
            FROM app.facts
            WHERE entity_id = %s::uuid AND property = 'status' AND valid_to IS NULL
            """,
            (supplier,),
        ).fetchone()["status"]
        assert status == "received"
        stored = conn.execute(
            "SELECT outcome, label_origin FROM app.match3_proposals WHERE id = %s::uuid",
            (own[0],),
        ).fetchone()
        assert stored["outcome"] == "MATCH_OK"
        assert stored["label_origin"] == "synthetic"
        with pytest.raises(psycopg.Error, match="altra azienda"):
            conn.execute(
                """
                INSERT INTO app.match3_decisions (
                  id, proposal_id, supplier_invoice_entity_id, company_id, decision, actor, actor_role, reason
                )
                VALUES ('00000000-0000-0000-0000-0000000000c1', %s::uuid,
                        (SELECT supplier_invoice_entity_id FROM app.match3_proposals WHERE id = %s::uuid),
                        %s::uuid, 'confirm', 'operator', 'operator', '')
                """,
                (own[0], own[0], other),
            )
        conn.rollback()

    login(client, "operator")
    foreign = client.get("/api/inbox").json()["recommendations"]
    foreign_cards = [item for item in foreign if item.get("kind") == "three_way"]
    assert [item["id"] for item in foreign_cards] == [other_ids[0]]
    assert foreign_cards[0]["invoice_number"] == "FT-ALTRO"
    assert own[0] not in {item["id"] for item in foreign}
    denied = client.post(
        f"/api/match3-proposals/{own[0]}/confirm",
        json={"decision": "confirm", "evidence_hash": "ab" * 32},
    )
    assert denied.status_code == 403

    login(client, "approver")
    inbox = client.get("/api/inbox").json()["recommendations"]
    card = next(item for item in inbox if item.get("kind") == "three_way")
    assert card["id"] == own[0]
    assert card["allowed_actions"] == ["confirm", "correct", "reject"]
    assert card["outcome"] == "MATCH_OK"
    assert "direction" not in card
    assert "payment" not in card
    blob = json.dumps(card, ensure_ascii=False)
    for word in ("Incasso", "incasso", "incoming", "outgoing", "pagamento a fornitore"):
        assert word not in blob
    detail = client.get(f"/api/objects/{supplier}").json()
    assert detail["match3"]["outcome"] == "MATCH_OK"
    assert detail["match3"]["allowed_actions"] == ["confirm", "correct", "reject"]
    confirmed = client.post(
        f"/api/match3-proposals/{card['id']}/confirm",
        json={"decision": "confirm", "evidence_hash": card["evidence_hash"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    with connect() as conn:
        assert remaining_balance(conn, company, sales) == balance
        assert measure_payment_matches(conn) == measure
        status = conn.execute(
            """
            SELECT value #>> '{}' AS status
            FROM app.facts
            WHERE entity_id = %s::uuid AND property = 'status' AND valid_to IS NULL
            """,
            (supplier,),
        ).fetchone()["status"]
        assert status == "received"
        count = conn.execute("SELECT count(*) AS n FROM app.match3_decisions").fetchone()["n"]
        assert count == 1


def test_latest_tolerance_partial_duplicate_and_the_page(client: TestClient):
    catalog = describe_ontology(load_merged(), None)
    assert catalog["match3_labels"]["outcomes"]["PRICE_OUT_OF_TOL"] == "Prezzo fuori tolleranza"
    assert catalog["match3_labels"]["outcomes"]["DUPLICATE_INVOICE"] == "Fattura già presente"
    assert catalog["match3_labels"]["failures"]["vat"] == "Aliquota IVA diversa dall'ordine"
    assert catalog["match3_labels"]["failures"]["price"] == "Prezzo fuori tolleranza"
    assert catalog["match3_labels"]["captions"]["expected_tax"] == "Imposta attesa"
    assert catalog["match3_labels"]["captions"]["invoice_tax"] == "Imposta in fattura"
    assert catalog["value_labels"]["SupplierInvoice"]["status"]["open"] == "Aperta"
    assert catalog["value_labels"]["SupplierInvoice"]["status"]["received"] == "Ricevuta"
    company = give_company("mapper", "Tre vie pagina")
    _assign("approver", company)
    _assign("operator", company)
    model = load_merged()
    with connect() as conn:
        record_tolerances(conn, company, price_abs=Decimal("0"))
        supplier = _books(
            conn,
            model,
            company,
            invoice_amount="100.01",
            total="122.00",
        )
        from services.purchasing.match3 import propose_company

        created = propose_company(conn, model, company)
        conn.commit()
        proposal = conn.execute(
            "SELECT outcome FROM app.match3_proposals WHERE id = %s::uuid",
            (created[0],),
        ).fetchone()
        assert proposal["outcome"] == "PRICE_OUT_OF_TOL"
        with pytest.raises(AppError, match="già presente"):
            write_supplier_invoice(
                conn,
                model,
                company_id=company,
                supplier_vat="IT99999999999",
                supplier_name="Fornitore Prova",
                number="FT-1",
                issued_at="2026-05-04",
                amount=Decimal("1.00"),
                currency="EUR",
                document_type="TD01",
                label_origin="synthetic",
                source="fatture_fornitori",
                source_class="external",
            )
        with pytest.raises(AppError, match="synthetic"):
            write_supplier_invoice(
                conn,
                model,
                company_id=company,
                supplier_vat="IT99999999999",
                supplier_name="Fornitore Prova",
                number="FT-2",
                issued_at="2026-05-04",
                amount=Decimal("1.00"),
                currency="EUR",
                document_type="TD01",
                label_origin="rule",
                source="fatture_fornitori",
                source_class="external",
            )
    login(client, "operator")
    inbox = client.get("/api/inbox").json()["recommendations"]
    card = next(item for item in inbox if item.get("kind") == "three_way")
    assert card["allowed_actions"] == []
    assert card["outcome"] == "PRICE_OUT_OF_TOL"
    detail = client.get(f"/api/objects/{supplier}").json()
    assert detail["match3"]["allowed_actions"] == []
    rendered = subprocess.run(
        ["node", "-e", _PAGE],
        cwd=ROOT,
        input=json.dumps({"card": card, "detail": detail, "ontology": client.get("/api/ontology").json()}),
        text=True,
        capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout == "ok\n"
    login(client, "approver")
    approver = client.get("/api/inbox").json()["recommendations"]
    shown = next(item for item in approver if item["id"] == card["id"])
    assert shown["allowed_actions"] == ["confirm", "correct", "reject"]
    rejected = client.post(
        f"/api/match3-proposals/{shown['id']}/reject",
        json={"decision": "reject", "evidence_hash": shown["evidence_hash"], "reason": "prezzo non dovuto"},
    )
    assert rejected.status_code == 200, rejected.text
    with connect() as conn:
        audit = conn.execute(
            """
            SELECT action, payload->>'decision' AS decision
            FROM app.audit_log
            WHERE action = 'human_decision'
              AND payload->>'proposal_id' = %s
            """,
            (shown["id"],),
        ).fetchone()
        assert audit["action"] == "human_decision"
        assert audit["decision"] == "reject"
    again = client.post(
        f"/api/match3-proposals/{shown['id']}/confirm",
        json={"decision": "confirm", "evidence_hash": shown["evidence_hash"]},
    )
    assert again.status_code == 409
    refreshed = client.get("/api/inbox").json()["recommendations"]
    decided = next(item for item in refreshed if item["id"] == shown["id"])
    assert decided["problem"].startswith("Eccezione rifiutata")
    assert decided["history"][0]["reason"] == "prezzo non dovuto"
    assert decided["history"][0]["actor"] == "approver"
    assert "revoke" in decided["allowed_actions"]


_CARD = r"""
const { inboxCard, objectDetail } = require("./apps/web/escape.js");
let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { raw += chunk; });
process.stdin.on("end", () => {
  const input = JSON.parse(raw);
  const ontology = {
    match3_labels: {
      outcomes: { VAT_MISMATCH: "IVA non coincidente" },
      failures: { vat: "Aliquota IVA diversa dall'ordine" },
      captions: {
        expected_tax: "Imposta attesa",
        invoice_tax: "Imposta in fattura",
        vat_rate: "Aliquota fattura",
        order_vat_rate: "Aliquota ordine",
        ordered_qty: "ordinata",
        received_qty: "ricevuta",
        invoiced_qty: "fatturata",
        expected_line: "Atteso",
        invoiced_line: "fatturato",
        threshold: "soglia",
        vat_heading: "IVA",
      },
    },
    types: [
      { name: "SupplierInvoice", label: "Fattura fornitore" },
      { name: "PurchaseOrder", label: "Ordine di acquisto" },
      { name: "GoodsReceipt", label: "Ricevimento" },
    ],
    value_labels: { SupplierInvoice: { status: { open: "Aperta", received: "Ricevuta" } } },
  };
  const card = {
    id: "p1",
    kind: "three_way",
    status: "rejected",
    outcome: "VAT_MISMATCH",
    invoice_number: "FT-9",
    order_number: "PO-9",
    headline: "Fattura fornitore FT-9",
    problem: "Eccezione rifiutata. La fattura fornitore non viene chiusa.",
    evidence_hash: "abc",
    failed_criteria: ["vat"],
    allowed_actions: ["revoke"],
    comparison: input.comparison,
    history: [{
      id: "d1",
      decision: "reject",
      actor: "approver",
      decided_at: "2026-05-04T10:00:00",
      revoked: false,
      reason: "aliquota diversa",
    }],
  };
  const html = inboxCard(card, "operator", true, ontology);
  const open = objectDetail({
    type: "SupplierInvoice",
    business_key: "FT-OPEN",
    fields: [{ name: "status", value: "open", label: "Stato", masked: false }],
    links: [],
    history: [],
  }, ontology);
  function fail(message) {
    console.error(message);
    process.exit(1);
  }
  if (!html.includes("fatturata 12,5")) fail(html);
  if (!html.includes("Aliquota fattura 10%") || !html.includes("Aliquota ordine 22%")) fail(html);
  if (!html.includes("Imposta attesa") || !html.includes("IVA 10%: 12,50 €")) fail(html);
  if (html.includes("Imposta in fattura")) fail(html);
  if (!html.includes("Aliquota IVA diversa dall")) fail(html);
  if (!html.includes("Ordine di acquisto PO-9")) fail(html);
  if (!html.includes("Eccezione rifiutata")) fail(html);
  if (!html.includes("Rifiutato da approver il 04/05/2026. aliquota diversa")) fail(html);
  if (!html.includes(">Annulla il rifiuto<")) fail(html);
  if (!open.includes("Stato: Aperta") || open.includes("Stato: open")) fail(open);
  for (const word of ["Incasso", "incasso", "incoming", "outgoing", "pagamento a fornitore", "IT999"]) {
    if (html.includes(word)) fail(word);
  }
  process.stdout.write("ok\n");
});
"""


_PAGE = r"""
const { inboxCard, objectDetail } = require("./apps/web/escape.js");
let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { raw += chunk; });
process.stdin.on("end", () => {
  const input = JSON.parse(raw);
  const operator = inboxCard(input.card, "operator", true, input.ontology);
  const detail = objectDetail(input.detail, input.ontology);
  const approverCard = Object.assign({}, input.card, {
    allowed_actions: ["confirm", "correct", "reject"],
  });
  const approver = inboxCard(approverCard, "approver", true, input.ontology);
  function fail(message) {
    console.error(message);
    process.exit(1);
  }
  if (!operator.includes("Prezzo fuori tolleranza")) fail(operator);
  if (!operator.includes("Aliquota fattura") || !operator.includes("Aliquota ordine")) fail(operator);
  if (!operator.includes("22%")) fail(operator);
  if (!operator.includes("Imposta attesa") || operator.includes("Imposta in fattura")) fail(operator);
  if (!operator.includes("Ordine di acquisto PO-1")) fail(operator);
  if (!operator.includes("Fattura fornitore")) fail(operator);
  if (!detail.includes("Prezzo fuori tolleranza")) fail(detail);
  if (!detail.includes("Stato: Ricevuta") || detail.includes("Stato: open") || detail.includes("Stato: received")) fail(detail);
  if (operator.includes("confirm-match3") || operator.includes("correct-match3") || operator.includes("reject-match3")) fail(operator);
  if (detail.includes("confirm-match3")) fail(detail);
  if (!approver.includes('data-act="confirm-match3"') || !approver.includes('data-act="reject-match3"')) fail(approver);
  for (const word of ["Incasso", "incasso", "incoming", "outgoing", "pagamento a fornitore", "In entrata", "In uscita"]) {
    if (operator.includes(word) || detail.includes(word)) fail(word + "\n" + operator);
  }
  process.stdout.write("ok\n");
});
"""
