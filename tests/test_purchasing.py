"""Acquisti: ontologia, tolleranze e fatture dei fornitori.

Le migrazioni 0022 e 0023 sono entrambe presenti, in ordine di numero.
Il runner non chiede che i numeri siano consecutivi.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from ontology.loader import load_merged
from services.agent.payment_measure import measure_payment_matches
from services.cash.balance import remaining_balance
from services.cash.decisions import _resolve_invoice
from services.cash.documents import write_invoice
from services.db import connect
from services.errors import AppError
from services.graph.projection import project_graph
from services.ingest.fatturapa import parse_fatturapa
from services.migrate import migration_files, migration_violation
from services.purchasing.invoices import supplier_invoice_key, write_supplier_invoice
from services.purchasing.tolerances import (
    PARTIAL_RECEIPT,
    PRICE_ABS,
    PRICE_RATE,
    TOTAL_ABS,
    VAT_ABS,
    current_tolerances,
    record_tolerances,
)
from tests.test_flow import give_company

ROOT = Path(__file__).resolve().parents[1]


def _open_edges(conn):
    return [
        (row["from_id"], row["rel"], row["to_id"])
        for row in conn.execute(
            """
            SELECT from_id::text, rel, to_id::text
            FROM app.graph_edges
            WHERE valid_to IS NULL
            ORDER BY 1, 2, 3
            """
        ).fetchall()
    ]

_PASSIVE = """<!-- label_origin: synthetic -->
<FatturaElettronica>
  <FatturaElettronicaHeader>
    <CedentePrestatore>
      <DatiAnagrafici>
        <Anagrafica><Denominazione>Fornitore Prova</Denominazione></Anagrafica>
        <IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>99999999999</IdCodice></IdFiscaleIVA>
      </DatiAnagrafici>
    </CedentePrestatore>
    <CessionarioCommittente>
      <DatiAnagrafici>
        <Anagrafica><Denominazione>Noi</Denominazione></Anagrafica>
        <IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>00000000001</IdCodice></IdFiscaleIVA>
      </DatiAnagrafici>
    </CessionarioCommittente>
  </FatturaElettronicaHeader>
  <FatturaElettronicaBody>
    <DatiGenerali>
      <DatiGeneraliDocumento>
        <TipoDocumento>TD01</TipoDocumento>
        <Divisa>EUR</Divisa>
        <Data>2026-05-02</Data>
        <Numero>FT-100</Numero>
        <ImportoTotaleDocumento>9000.00</ImportoTotaleDocumento>
      </DatiGeneraliDocumento>
    </DatiGenerali>
  </FatturaElettronicaBody>
</FatturaElettronica>
"""


def test_purchasing_package_declares_types_links_and_process():
    manufacturing = (ROOT / "ontology" / "manufacturing.yaml").read_text(encoding="utf-8")
    model = load_merged()
    purchasing = next(item for item in model.packages if item.name == "purchasing")
    assert purchasing.raw["depends_on"] == ["sales"]
    assert model.types["Order"].package == "manufacturing"
    assert "name: PurchaseOrder" not in manufacturing
    assert model.prop("Order", "amount") is not None
    assert model.prop("Supplier", "vat") is not None
    for name in (
        "PurchaseRequest",
        "PurchaseOrder",
        "PurchaseOrderLine",
        "GoodsReceipt",
        "GoodsReceiptLine",
        "SupplierInvoice",
        "SupplierInvoiceLine",
    ):
        assert model.types[name].package == "purchasing"
        assert model.prop(name, "source_key").identifier is True
    invoice = model.types["SupplierInvoice"]
    assert invoice.properties["supplier_vat"].identifier is False
    assert invoice.properties["invoice_number"].identifier is False
    assert invoice.properties["invoice_year"].identifier is False
    links = {(item["name"], item["from"], item["to"]) for item in model.relations}
    assert ("requests", "PurchaseRequest", "PurchaseOrder") in links
    assert ("orderedFrom", "PurchaseOrder", "Supplier") in links
    assert ("receives", "GoodsReceipt", "PurchaseOrder") in links
    assert ("billsFor", "SupplierInvoice", "PurchaseOrder") in links
    assert ("settles", "PaymentAllocation", "SupplierInvoice") in links
    assert ("settles", "PaymentAllocation", "Invoice") in links
    process = model.process("procure_to_pay")
    assert process is not None
    assert process.label == "Dall'acquisto al pagamento"
    assert [step.id for step in process.steps] == [
        "requested",
        "ordered",
        "received",
        "supplier_invoiced",
        "three_way",
        "payment_proposed",
        "payment_authorized",
    ]


def test_migration_0023_follows_0022_and_is_versioned_per_company():
    versions = [item.version for item in migration_files()]
    assert versions.index("0022") == versions.index("0021") + 1
    assert versions.index("0023") == versions.index("0022") + 1
    assert versions == sorted(versions, key=int)
    script = (ROOT / "db" / "migrations" / "0023_p2p_tolerances.sql").read_text(encoding="utf-8")
    assert migration_violation(script, "0023_p2p_tolerances.sql") is None
    for line in script.splitlines():
        code = line.split("--", 1)[0].strip().lower()
        assert not code.startswith("update")
    assert "company_id uuid NOT NULL" in script
    assert "DEFAULT 0.01" in script
    assert "DEFAULT 5" in script
    assert "DEFAULT 0.05" in script
    assert "DEFAULT true" in script
    assert "CHECK (price_rate >= 0 AND price_rate <= 1)" in script
    assert "CHECK (price_abs >= 0)" in script
    assert "CHECK (vat_abs >= 0)" in script
    assert "CHECK (total_abs >= 0)" in script
    company_a = give_company("mapper", "Tolleranze A")
    company_b = give_company("operator", "Tolleranze B")
    with connect() as conn:
        untouched = current_tolerances(conn, company_a)
        assert untouched["stored"] is False
        assert untouched["version"] is None
        assert untouched["price_rate"] == PRICE_RATE == Decimal("0.01")
        assert untouched["price_abs"] == PRICE_ABS == Decimal("5")
        assert untouched["vat_abs"] == VAT_ABS == Decimal("0.01")
        assert untouched["total_abs"] == TOTAL_ABS == Decimal("0.05")
        assert untouched["partial_receipt"] is PARTIAL_RECEIPT is True
        assert conn.execute("SELECT count(*) AS n FROM app.p2p_tolerances").fetchone()["n"] == 0
        first = record_tolerances(conn, company_a, price_abs=Decimal("4"))
        assert first["version"] == 1
        assert first["price_abs"] == Decimal("4")
        assert first["price_rate"] == Decimal("0.01")
        second = record_tolerances(conn, company_a, price_rate=Decimal("0.02"))
        assert second["version"] == 2
        assert second["price_rate"] == Decimal("0.02")
        assert second["price_abs"] == Decimal("4")
        kept = conn.execute(
            """
            SELECT price_abs, price_rate
            FROM app.p2p_tolerances
            WHERE company_id = %s::uuid AND version = 1
            """,
            (company_a,),
        ).fetchone()
        assert Decimal(kept["price_abs"]) == Decimal("4")
        assert Decimal(kept["price_rate"]) == Decimal("0.01")
        other = current_tolerances(conn, company_b)
        assert other["stored"] is False
        assert other["price_abs"] == Decimal("5")
        with pytest.raises(Exception, match="append-only"):
            conn.execute(
                "UPDATE app.p2p_tolerances SET price_abs = 9 WHERE company_id = %s::uuid",
                (company_a,),
            )
        conn.rollback()
        with pytest.raises(Exception):
            conn.execute(
                """
                INSERT INTO app.p2p_tolerances (id, company_id, version)
                VALUES ('00000000-0000-0000-0000-000000000099', NULL, 1)
                """
            )
        conn.rollback()


def test_tolerance_bounds_reject_a_negative_and_a_rate_above_one():
    company = give_company("mapper", "Tolleranze limiti")
    with connect() as conn:
        with pytest.raises(AppError, match="tra 0 e 1"):
            record_tolerances(conn, company, price_rate=Decimal("1.5"))
        with pytest.raises(AppError, match="maggiori o uguali a zero"):
            record_tolerances(conn, company, price_abs=Decimal("-1"))
        with pytest.raises(AppError, match="maggiori o uguali a zero"):
            record_tolerances(conn, company, vat_abs=Decimal("-0.01"))
        with pytest.raises(AppError, match="maggiori o uguali a zero"):
            record_tolerances(conn, company, total_abs=Decimal("-0.05"))
        kept = record_tolerances(
            conn,
            company,
            price_rate=Decimal("1"),
            price_abs=Decimal("0"),
            vat_abs=Decimal("0"),
            total_abs=Decimal("0"),
        )
        assert kept["price_rate"] == Decimal("1")
        assert kept["price_abs"] == Decimal("0")
        assert kept["vat_abs"] == Decimal("0")
        assert kept["total_abs"] == Decimal("0")
        conn.commit()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO app.p2p_tolerances (id, company_id, version, price_rate)
                VALUES ('00000000-0000-0000-0000-0000000000a1', %s::uuid, 9, 1.5)
                """,
                (company,),
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO app.p2p_tolerances (id, company_id, version, price_abs)
                VALUES ('00000000-0000-0000-0000-0000000000a2', %s::uuid, 8, -1)
                """,
                (company,),
            )
        conn.rollback()
        still = current_tolerances(conn, company)
        assert still["version"] == 1
        assert still["price_rate"] == Decimal("1")


def test_parser_reads_the_supplier_and_still_requires_synthetic():
    naked = _PASSIVE.replace("<!-- label_origin: synthetic -->", "")
    with pytest.raises(AppError, match="synthetic"):
        parse_fatturapa(naked)
    parsed = parse_fatturapa(_PASSIVE)
    assert parsed["supplier"] == {"name": "Fornitore Prova", "vat": "IT99999999999"}
    assert parsed["invoices"][0]["customer_vat"] == "IT00000000001"
    assert parsed["invoices"][0]["number"] == "FT-100"
    sales = parse_fatturapa((ROOT / "fixtures" / "synthetic" / "fatture_prova.xml").read_text(encoding="utf-8"))
    assert sales["supplier"] == {"name": "", "vat": ""}
    assert sales["invoices"][0]["customer_name"] == "Cliente Prova Alfa"
    assert len(sales["invoices"]) == 4
    assert len(sales["credit_notes"]) == 1


def test_supplier_invoice_key_blocks_duplicates_without_touching_cash():
    assert "SupplierInvoice" not in (ROOT / "services" / "cash" / "decisions.py").read_text(encoding="utf-8")
    assert "SupplierInvoice" not in (ROOT / "services" / "graph" / "projection.py").read_text(encoding="utf-8")
    company = give_company("mapper", "Fatture fornitori")
    other = give_company("operator", "Altra azienda")
    model = load_merged()
    with connect() as conn:
        invoice_id = write_invoice(
            conn,
            model,
            company_id=company,
            number="FT-100",
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
        project_graph(conn)
        balance = remaining_balance(conn, company, invoice_id)
        measure = measure_payment_matches(conn)
        edges = _open_edges(conn)
        assert supplier_invoice_key("it 99999999999", "FT-100", 2026) == "IT99999999999|FT-100|2026"
        supplier_id = write_supplier_invoice(
            conn,
            model,
            company_id=company,
            supplier_vat="IT99999999999",
            supplier_name="Fornitore Prova",
            number="FT-100",
            issued_at="2026-05-02",
            amount=Decimal("9000.00"),
            currency="EUR",
            document_type="TD01",
            label_origin="synthetic",
            source="fatture_fornitori",
            source_class="external",
            lines=[{"number": "1", "description": "Merce", "quantity": Decimal("2"), "line_amount": Decimal("9000")}],
        )
        project_graph(conn)
        assert supplier_id != invoice_id
        assert remaining_balance(conn, company, invoice_id) == balance
        assert remaining_balance(conn, company, supplier_id) is None
        assert measure_payment_matches(conn) == measure
        assert _open_edges(conn) == edges
        resolved = _resolve_invoice(conn, company, None, "FT-100")
        assert resolved["id"] == invoice_id
        stored = conn.execute(
            """
            SELECT business_key FROM app.entities WHERE id = %s::uuid
            """,
            (supplier_id,),
        ).fetchone()
        assert stored["business_key"] == "IT99999999999|FT-100|2026"
        shielded = conn.execute(
            """
            SELECT value #>> '{}' AS shown, value_cleartext #>> '{}' AS clear
            FROM app.facts
            WHERE entity_id = %s::uuid AND property = 'supplier_vat' AND valid_to IS NULL
            """,
            (supplier_id,),
        ).fetchone()
        assert shielded["shown"] == "••••"
        assert shielded["clear"] == "IT99999999999"
        with pytest.raises(AppError, match="già presente"):
            write_supplier_invoice(
                conn,
                model,
                company_id=company,
                supplier_vat="IT99999999999",
                supplier_name="Fornitore Prova",
                number="FT-100",
                issued_at="2026-06-01",
                amount=Decimal("1.00"),
                currency="EUR",
                document_type="TD01",
                label_origin="synthetic",
                source="fatture_fornitori",
                source_class="external",
            )
        next_year = write_supplier_invoice(
            conn,
            model,
            company_id=company,
            supplier_vat="IT99999999999",
            supplier_name="Fornitore Prova",
            number="FT-100",
            issued_at="2025-05-02",
            amount=Decimal("1.00"),
            currency="EUR",
            document_type="TD01",
            label_origin="synthetic",
            source="fatture_fornitori",
            source_class="external",
        )
        assert next_year != supplier_id
        elsewhere = write_supplier_invoice(
            conn,
            model,
            company_id=other,
            supplier_vat="IT99999999999",
            supplier_name="Fornitore Prova",
            number="FT-100",
            issued_at="2026-05-02",
            amount=Decimal("1.00"),
            currency="EUR",
            document_type="TD01",
            label_origin="synthetic",
            source="fatture_fornitori",
            source_class="external",
        )
        assert elsewhere != supplier_id
        with pytest.raises(AppError, match="synthetic"):
            write_supplier_invoice(
                conn,
                model,
                company_id=company,
                supplier_vat="IT11111111111",
                supplier_name="Altro",
                number="FT-9",
                issued_at="2026-01-01",
                amount=Decimal("1.00"),
                currency="EUR",
                document_type="TD01",
                label_origin="rule",
                source="fatture_fornitori",
                source_class="external",
            )
        conn.rollback()
