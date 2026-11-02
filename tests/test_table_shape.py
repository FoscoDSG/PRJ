"""Table shape recognition. Scores are rule scores. Ambiguous files stay unknown."""

import io
import re
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import app
from services.mapping.engine import (
    DISAGREEMENT_SCORE,
    THRESHOLD,
    assigned_mapping,
    classify_table,
    detect_profile,
)
from services.mapping.labels import PROFILE_LABELS, UNKNOWN_PROFILE_LABEL, profile_label
from services.mapping.shape import (
    TABLE_SHAPE_RULE,
    _present_roles,
    catalog_signal,
    recognize_table,
    shape_rules,
)
from services.testset import MIN_MEASURED_CASES, MIN_MEASURED_SOURCES
from tests.test_flow import give_company, login

import pytest

ROOT_LABELS = PROFILE_LABELS


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_shape_rule_is_a_hand_score_from_allowed_sources():
    score_kind, rules = shape_rules()
    assert TABLE_SHAPE_RULE == "mapping.table_shape@v1"
    assert "punteggio di regola" in score_kind.casefold()
    assert rules
    text = __import__("pathlib").Path("ontology/table_shape.yaml").read_text(encoding="utf-8").casefold()
    assert "shopify" not in text
    assert "fatturapa" not in text
    assert "fattura pa" not in text
    assert MIN_MEASURED_SOURCES == 3
    assert MIN_MEASURED_CASES == 30


def test_quantity_price_and_date_select_order_lines():
    columns = ["orderQuantity", "price", "orderDate", "Codice prodotto"]
    rows = [
        {"orderQuantity": "2", "price": "10.00", "orderDate": "2024-01-02", "Codice prodotto": "A1"},
        {"orderQuantity": "1", "price": "3.50", "orderDate": "2024-01-03", "Codice prodotto": "B2"},
    ]
    assert detect_profile(columns) == "unknown"
    found = recognize_table(columns, rows)
    assert found.accepted is True
    assert found.profile == "order_line"
    assert found.score == 0.88
    assert found.score >= THRESHOLD
    assert found.rule == TABLE_SHAPE_RULE
    assert detect_profile(columns, rows) == "order_line"
    # Il punteggio più alto, da solo, assegna il codice al prodotto.
    # Con il profilo delle righe lo stesso nome punta alla riga.
    unknown = assigned_mapping("unknown", "Codice prodotto")
    assert unknown[0] == "Product"
    assert unknown[1] == "source_key"
    assert assigned_mapping("order_line", "Codice prodotto")[1] == "product_source_key"


def test_repeating_identifier_with_quantity_and_price_is_order_lines():
    columns = ["orderNumber", "orderQuantity", "price"]
    rows = [
        {"orderNumber": "100", "orderQuantity": "1", "price": "10.00"},
        {"orderNumber": "100", "orderQuantity": "2", "price": "5.00"},
        {"orderNumber": "101", "orderQuantity": "4", "price": "1.50"},
        {"orderNumber": "102", "orderQuantity": "1", "price": "2.00"},
        {"orderNumber": "103", "orderQuantity": "3", "price": "4.00"},
        {"orderNumber": "104", "orderQuantity": "2", "price": "6.00"},
    ]
    found = recognize_table(columns, rows)
    assert found.accepted is True
    assert found.profile == "order_line"
    assert found.score == 0.86


def test_email_and_vat_values_select_customer():
    columns = ["email_formatted", "Riferimento fiscale"]
    rows = [
        {"email_formatted": "a@example.com", "Riferimento fiscale": "IT12345678901"},
        {"email_formatted": "b@example.com", "Riferimento fiscale": "IT10987654321"},
    ]
    found = recognize_table(columns, rows)
    assert found.accepted is True
    assert found.profile == "customer"
    assert found.score == 0.90
    assert detect_profile(columns, rows) == "customer"


def test_phone_values_with_email_select_customer():
    columns = ["Indirizzo e-mail", "Recapito"]
    rows = [
        {"Indirizzo e-mail": "a@example.com", "Recapito": "+39 021234567"},
        {"Indirizzo e-mail": "b@example.com", "Recapito": "+39 061234568"},
    ]
    found = recognize_table(columns, rows)
    assert found.profile == "customer"
    assert found.accepted is True


def test_article_code_and_price_without_date_select_product():
    columns = ["barcode", "Sales Price"]
    rows = [
        {"barcode": "ABC123", "Sales Price": "10.00"},
        {"barcode": "DEF456", "Sales Price": "3.50"},
    ]
    found = recognize_table(columns, rows)
    assert found.accepted is True
    assert found.profile == "product"
    assert found.score == 0.84


def test_amount_and_date_select_order():
    columns = ["totalPaymentDue", "orderDate"]
    rows = [
        {"totalPaymentDue": "80.00", "orderDate": "2024-03-01"},
        {"totalPaymentDue": "15.50", "orderDate": "2024-03-02"},
    ]
    found = recognize_table(columns, rows)
    assert found.accepted is True
    assert found.profile == "order"
    assert found.score == 0.82


def test_stock_and_supplier_shapes():
    stock = recognize_table(
        ["Quantità giacenza fisica", "barcode"],
        [
            {"Quantità giacenza fisica": "4", "barcode": "A"},
            {"Quantità giacenza fisica": "9", "barcode": "B"},
        ],
    )
    assert stock.profile == "stock"
    assert stock.score == 0.80
    supplier = recognize_table(
        ["Tempo di risposta per consegne", "Codice prodotto del fornitore"],
        [
            {"Tempo di risposta per consegne": "5", "Codice prodotto del fornitore": "PX"},
            {"Tempo di risposta per consegne": "12", "Codice prodotto del fornitore": "PY"},
        ],
    )
    assert supplier.profile == "supplier"
    assert supplier.accepted is True


def test_ambiguous_files_stay_below_threshold_and_are_not_guessed():
    empty = recognize_table(
        ["Nota", "Commento"],
        [{"Nota": "ciao", "Commento": "libero"}, {"Nota": "altro", "Commento": "testo"}],
    )
    assert empty.accepted is False
    assert empty.profile == "unknown"
    assert empty.score < THRESHOLD
    single = recognize_table(
        ["email_formatted"],
        [{"email_formatted": "a@example.com"}, {"email_formatted": "b@example.com"}],
    )
    assert single.accepted is False
    assert single.profile == "unknown"
    dated_catalog = recognize_table(
        ["barcode", "Sales Price", "orderDate"],
        [
            {"barcode": "ABC", "Sales Price": "10.00", "orderDate": "2024-01-02"},
            {"barcode": "DEF", "Sales Price": "3.50", "orderDate": "2024-01-03"},
        ],
    )
    assert dated_catalog.accepted is False
    assert dated_catalog.profile == "unknown"
    assert dated_catalog.score < THRESHOLD
    conflict = recognize_table(
        ["email_formatted", "Riferimento fiscale", "orderQuantity", "price", "orderDate"],
        [
            {
                "email_formatted": "a@example.com",
                "Riferimento fiscale": "IT12345678901",
                "orderQuantity": "1",
                "price": "10.00",
                "orderDate": "2024-01-02",
            },
            {
                "email_formatted": "b@example.com",
                "Riferimento fiscale": "IT10987654321",
                "orderQuantity": "2",
                "price": "3.50",
                "orderDate": "2024-01-03",
            },
        ],
    )
    assert conflict.accepted is False
    assert conflict.profile == "unknown"
    assert conflict.score < THRESHOLD
    assert detect_profile(
        ["email_formatted", "Riferimento fiscale", "orderQuantity", "price", "orderDate"],
        conflict and [
            {
                "email_formatted": "a@example.com",
                "Riferimento fiscale": "IT12345678901",
                "orderQuantity": "1",
                "price": "10.00",
                "orderDate": "2024-01-02",
            },
            {
                "email_formatted": "b@example.com",
                "Riferimento fiscale": "IT10987654321",
                "orderQuantity": "2",
                "price": "3.50",
                "orderDate": "2024-01-03",
            },
        ],
    ) == "unknown"


def test_profile_label_comes_from_the_fixed_map():
    assert profile_label("order") == "Probabile export ordini"
    assert profile_label("unknown") == UNKNOWN_PROFILE_LABEL
    assert profile_label("<script>alert(1)</script>") == UNKNOWN_PROFILE_LABEL
    assert profile_label("orderDate") == UNKNOWN_PROFILE_LABEL
    for label in PROFILE_LABELS.values():
        assert label.startswith("Probabile ")
        assert "%" not in label
        assert not any(character.isdigit() for character in label)
    script = __import__("pathlib").Path("apps/web/escape.js").read_text(encoding="utf-8")
    for label in (*ROOT_LABELS.values(), UNKNOWN_PROFILE_LABEL):
        assert label in script


def test_uploaded_file_does_not_become_the_label(client: TestClient):
    give_company("mapper", "Etichette")
    login(client, "mapper")
    hostile = '"><img src=x onerror=alert(1)>'
    order = "Order Date,Total Payment Due\n2024-01-02,10.00\n2024-02-03,12.50\n"
    ingested = client.post(
        "/api/ingest/csv",
        files={"file": ("pilot.csv", io.BytesIO(order.encode()), "text/csv")},
        data={"source_name": hostile, "source_class": "external"},
    )
    assert ingested.status_code == 200, ingested.text
    mappings = client.get("/api/mappings").json()["mappings"]
    assert mappings
    assert {item["profile_label"] for item in mappings} == {"Probabile export ordini"}
    for item in mappings:
        assert hostile not in item["profile_label"]
        assert "Order Date" not in item["profile_label"]
        assert "Total Payment Due" not in item["profile_label"]
        assert "%" not in item["profile_label"]
    vague = "Nota libera,Annotazione interna\nciao,libero\naltro,testo\n"
    second = client.post(
        "/api/ingest/csv",
        files={"file": ("note.csv", io.BytesIO(vague.encode()), "text/csv")},
        data={"source_name": "note_libere", "source_class": "external"},
    )
    assert second.status_code == 200, second.text
    rows = client.get("/api/mappings").json()["mappings"]
    notes = [item for item in rows if item["source_name"] == "note_libere"]
    assert notes
    assert {item["profile"] for item in notes} == {"unknown"}
    assert {item["profile_label"] for item in notes} == {UNKNOWN_PROFILE_LABEL}
    assert all(item["confidence"] < THRESHOLD for item in notes)


def test_country_on_a_customer_registry_is_not_order_lines():
    columns = ["Nome completo", "Paese", "orderQuantity", "price"]
    rows = [
        {
            "Nome completo": f"Cliente {index}",
            "Paese": "Italia",
            "orderQuantity": str(index + 1),
            "price": f"{index + 1}.00",
        }
        for index in range(20)
    ]
    found = recognize_table(columns, rows)
    assert found.profile != "order_line"
    assert found.accepted is False
    assert detect_profile(columns, rows) != "order_line"


def test_two_emails_in_a_thousand_rows_do_not_type_the_column():
    rows = [{"Appunto": "nessuna", "Recapito": "+39 021234567"} for _ in range(998)]
    rows.append({"Appunto": "a@example.com", "Recapito": "+39 021234567"})
    rows.append({"Appunto": "b@example.com", "Recapito": "+39 061234568"})
    found = recognize_table(["Appunto", "Recapito"], rows)
    assert found.profile != "customer"
    assert found.accepted is False


def test_integer_and_one_decimal_amounts_require_a_price_header():
    priced = recognize_table(
        ["price", "orderDate"],
        [
            {"price": "18", "orderDate": "2024-01-02"},
            {"price": "45.0", "orderDate": "2024-01-03"},
        ],
    )
    assert priced.accepted is True
    assert priced.profile == "order"
    assert priced.score == 0.82
    plain = recognize_table(
        ["Nota", "Giorno indicato"],
        [
            {"Nota": "18", "Giorno indicato": "02/03/24"},
            {"Nota": "45.0", "Giorno indicato": "15/03/2024"},
        ],
    )
    assert plain.profile != "order"
    assert plain.accepted is False


def test_woocommerce_regular_price_file_stays_unrecognized():
    """Regular price is WooCommerce's own header, not schema.org price."""
    columns = ["SKU", "Name", "Regular price"]
    rows = [
        {"SKU": "WC-1", "Name": "Tazza", "Regular price": "18"},
        {"SKU": "WC-2", "Name": "Piatto", "Regular price": "45.0"},
    ]
    found = recognize_table(columns, rows)
    assert found.accepted is False
    assert found.profile == "unknown"
    assert found.score < THRESHOLD
    assert profile_label(found.profile) == UNKNOWN_PROFILE_LABEL
    assert detect_profile(columns, rows) == "unknown"
    assert "price" not in _present_roles(columns, rows)


def test_integer_quantity_column_does_not_become_a_price():
    columns = ["orderQuantity", "orderDate"]
    rows = [
        {"orderQuantity": "18", "orderDate": "2024-01-02"},
        {"orderQuantity": "45", "orderDate": "2024-01-03"},
        {"orderQuantity": "7", "orderDate": "2024-01-04"},
    ]
    roles = _present_roles(columns, rows)
    assert "price" not in roles
    assert "amount" not in roles
    found = recognize_table(columns, rows)
    assert found.profile != "order_line"
    assert found.profile != "order"
    assert found.accepted is False


def test_three_row_odoo_customers_have_no_repeating_key():
    """A short res.partner extract: company and country repeat, three rows do not."""
    columns = ["complete_name", "email_formatted", "vat", "commercial_company_name", "country_id"]
    rows = [
        {
            "complete_name": "Azure Interior",
            "email_formatted": "azure@example.com",
            "vat": "IT12345678901",
            "commercial_company_name": "Azure Interior",
            "country_id": "Italy",
        },
        {
            "complete_name": "Brandon Freeman",
            "email_formatted": "brandon@example.com",
            "vat": "IT10987654321",
            "commercial_company_name": "Azure Interior",
            "country_id": "Italy",
        },
        {
            "complete_name": "Colleen Diaz",
            "email_formatted": "colleen@example.com",
            "vat": "IT10293847561",
            "commercial_company_name": "Azure Interior",
            "country_id": "France",
        },
    ]
    roles = _present_roles(columns, rows)
    assert "repeating_key" not in roles
    found = recognize_table(columns, rows)
    assert found.accepted is True
    assert found.profile == "customer"
    assert found.profile != "order_line"


def test_italian_amount_and_date_formats():
    columns = ["Valore indicato", "Giorno indicato"]
    rows = [
        {"Valore indicato": "1.234,56", "Giorno indicato": "02/03/24"},
        {"Valore indicato": "-1.234,56", "Giorno indicato": "15/03/2024"},
        {"Valore indicato": "(1.234,56)", "Giorno indicato": "01/04/26"},
    ]
    found = recognize_table(columns, rows)
    assert found.accepted is True
    assert found.profile == "order"
    assert found.score == 0.82


def test_order_line_rules_are_blocked_by_a_catalog():
    _, rules = shape_rules()
    order_lines = [rule for rule in rules if rule.profile == "order_line"]
    assert order_lines
    assert all("catalog" in rule.absent for rule in order_lines)
    assert DISAGREEMENT_SCORE < THRESHOLD
    assert THRESHOLD == 0.75


def test_unique_sku_catalog_with_stock_qty_is_not_order_lines():
    """A catalog: unique sku, a price, stock qty, no order key.

    A repeated attribute and integer prices used to score as order lines
    (repeating key + quantity + price, rule score 0.86). A date that only
    the cells show used to score 0.88. Neither is an order.
    """
    columns = ["sku", "price", "qty", "gruppo"]
    groups = ["rosso", "rosso", "blu", "blu", "verde", "verde", "nero", "nero", "bianco", "bianco"]
    rows = [
        {
            "sku": f"SKU-{index + 1}",
            "price": str(10 + index),
            "qty": "100",
            "gruppo": group,
        }
        for index, group in enumerate(groups)
    ]
    roles = _present_roles(columns, rows)
    assert "catalog" in roles
    assert "quantity" in roles
    assert "price" in roles
    assert "product_code" in roles
    assert "repeating_key" not in roles
    assert "order_key" not in roles
    assert catalog_signal(columns, rows) is True
    found = recognize_table(columns, rows)
    assert found.profile != "order_line"
    assert found.accepted is False
    assert found.score < THRESHOLD
    assert found.score != 0.86
    decision = classify_table(columns, rows)
    assert decision.profile != "order_line"
    assert decision.profile == "unknown"
    assert decision.accepted is False
    assert decision.score < THRESHOLD
    assert decision.header_profile == "order_line"
    assert profile_label(decision.profile) == UNKNOWN_PROFILE_LABEL
    assert detect_profile(columns, rows) != "order_line"
    assert detect_profile(columns) == "order_line"
    dated = [
        {**row, "nota_tempo": f"2024-01-{index + 1:02d}"}
        for index, row in enumerate(rows)
    ]
    dated_columns = [*columns, "nota_tempo"]
    dated_found = recognize_table(dated_columns, dated)
    assert "date" in _present_roles(dated_columns, dated)
    assert dated_found.profile != "order_line"
    assert dated_found.score < THRESHOLD
    assert detect_profile(dated_columns, dated) != "order_line"


def test_header_and_shape_disagreement_drops_below_threshold():
    columns = ["Quantity", "Price", "email_formatted", "vat"]
    rows = [
        {
            "Quantity": "1",
            "Price": "10.00",
            "email_formatted": "a@example.com",
            "vat": "IT12345678901",
        },
        {
            "Quantity": "2",
            "Price": "3.50",
            "email_formatted": "b@example.com",
            "vat": "IT10987654321",
        },
    ]
    assert detect_profile(columns) == "order_line"
    shape = recognize_table(columns, rows)
    assert shape.accepted is True
    assert shape.profile == "customer"
    assert shape.score >= THRESHOLD
    decision = classify_table(columns, rows)
    assert decision.header_profile == "order_line"
    assert decision.shape_profile == "customer"
    assert decision.profile == "unknown"
    assert decision.accepted is False
    assert decision.score == DISAGREEMENT_SCORE
    assert decision.score < THRESHOLD
    assert decision.score < shape.score
    assert profile_label(decision.profile) == UNKNOWN_PROFILE_LABEL
    assert detect_profile(columns, rows) == "unknown"


def test_magento_names_are_not_shape_or_synonym_inputs():
    shape = Path("ontology/table_shape.yaml").read_text(encoding="utf-8").casefold()
    synonyms = Path("ontology/synonyms.yaml").read_text(encoding="utf-8")
    assert "magento" not in shape
    assert "magento" in synonyms.casefold()
    from services.mapping.synonyms import synonym_book

    for entry in synonym_book().entries:
        folded = f"{entry.header} {entry.normalized} {entry.source}".casefold()
        assert "magento" not in folded
        assert "attribute_set" not in entry.normalized
        assert "store view" not in entry.normalized


def test_profile_labels_match_between_python_and_javascript():
    script = Path("apps/web/escape.js").read_text(encoding="utf-8")
    block = re.search(r"const PROFILE_LABELS = \{([^}]+)\}", script)
    assert block is not None
    parsed = dict(re.findall(r'(\w+):\s*"([^"]*)"', block.group(1)))
    unknown = re.search(r'const UNKNOWN_PROFILE_LABEL = "([^"]*)"', script)
    assert unknown is not None
    assert parsed == PROFILE_LABELS
    assert unknown.group(1) == UNKNOWN_PROFILE_LABEL
