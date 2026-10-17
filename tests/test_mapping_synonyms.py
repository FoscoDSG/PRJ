"""Normalized headers, the synonym dictionary, and the held-out measure."""

from pathlib import Path

from ontology.loader import load_merged
from services.mapping.engine import (
    HEADER_SIMILARITY_RULE,
    NORMALIZED_HEADER_RULE,
    PROFILES,
    SYNONYM_HEADER_RULE,
    THRESHOLD,
    UNKNOWN_HEADER_RULE,
    assigned_mapping,
    detect_profile,
)
from services.mapping.headers import normalize_header
from services.mapping.holdout import holdout_report
from services.mapping.synonyms import ALLOWED_SOURCES, synonym_book, synonym_headers
from services.mapping.tokens import TOKEN_RULE, token_book
from services.testset import MIN_MEASURED_CASES, MIN_MEASURED_SOURCES

ROOT = Path(__file__).resolve().parents[1]


def test_normalize_header_strips_accents_and_splits_camel_case():
    assert normalize_header("PrezzoUnitario") == "prezzo unitario"
    assert normalize_header("IdFiscaleIVA") == "id fiscale iva"
    assert normalize_header("AliquotaIVA") == "aliquota iva"
    assert normalize_header("ragione_sociale") == "ragione sociale"
    assert normalize_header("Ragione-Sociale") == "ragione sociale"
    assert normalize_header("Indirizzo e-mail") == "indirizzo e mail"
    assert normalize_header("quantità") == "quantita"
    assert normalize_header("Città") == "citta"
    assert normalize_header("dell'imposta") == "dell imposta"
    assert normalize_header("Weight (lbs)") == "weight lbs"
    assert normalize_header("In stock?") == "in stock"
    assert normalize_header("  Prezzo / Unitario. ") == "prezzo unitario"


def test_exact_italian_keys_keep_header_similarity_and_score():
    expected = {
        ("customer", "anag_cli"): ("Customer", "source_key", 0.96),
        ("customer", "anag_clienti"): ("Customer", "source_key", 0.98),
        ("customer", "ragione_sociale"): ("Customer", "company_name", 0.95),
        ("customer", "piva"): ("Customer", "vat", 0.97),
        ("customer", "email"): ("Customer", "email", 0.90),
        ("order", "ord_head"): ("Order", "source_key", 0.98),
        ("order", "anag_cli"): ("Order", "customer_source_key", 0.94),
        ("order", "importo"): ("Order", "amount", 0.93),
        ("order", "stato"): ("Order", "status", 0.86),
        ("order", "data"): ("Order", "ordered_at", 0.84),
        ("order_line", "ord_head"): ("OrderLine", "order_source_key", 0.95),
        ("order_line", "cod_art"): ("OrderLine", "product_source_key", 0.99),
        ("order_line", "qty"): ("OrderLine", "quantity", 0.92),
        ("order_line", "quantita"): ("OrderLine", "quantity", 0.93),
        ("order_line", "prezzo"): ("OrderLine", "unit_price", 0.90),
        ("product", "cod_art"): ("Product", "source_key", 0.99),
        ("product", "descrizione"): ("Product", "name", 0.90),
        ("product", "costo"): ("Product", "cost", 0.92),
        ("stock", "cod_art"): ("StockPosition", "product_source_key", 0.95),
        ("stock", "magazzino"): ("StockPosition", "warehouse_source_key", 0.90),
        ("stock", "giacenza"): ("StockPosition", "on_hand", 0.94),
        ("stock", "impegnato"): ("StockPosition", "reserved", 0.90),
        ("supplier", "forn"): ("Supplier", "source_key", 0.93),
        ("supplier", "nome"): ("Supplier", "name", 0.84),
        ("supplier", "cod_art"): ("Supplier", "product_source_key", 0.90),
        ("supplier", "ritardo_giorni"): ("Supplier", "delay_days", 0.92),
    }
    assert set(expected) == {
        (profile, column) for profile, columns in PROFILES.items() for column in columns
    }
    for (profile, column), target in expected.items():
        assert assigned_mapping(profile, column) == (*target, HEADER_SIMILARITY_RULE)


def test_normalized_variant_keeps_the_profile_target_and_score():
    cases = (
        ("customer", "Ragione Sociale", "Customer", "company_name", 0.95),
        ("customer", "Email", "Customer", "email", 0.90),
        ("order", "ORD_HEAD", "Order", "source_key", 0.98),
        ("order", "Importo", "Order", "amount", 0.93),
        ("product", "Descrizione", "Product", "name", 0.90),
    )
    for profile, column, type_name, prop, score in cases:
        assert assigned_mapping(profile, column) == (
            type_name,
            prop,
            score,
            NORMALIZED_HEADER_RULE,
        )


def test_unknown_profile_does_not_assign_an_ambiguous_key():
    found = assigned_mapping("unknown", "cod_art")
    assert found == ("Document", "title", 0.42, UNKNOWN_HEADER_RULE)


def test_english_and_variant_headers_detect_the_table_type():
    assert detect_profile(["Deal Name", "Amount", "Close Date"]) == "order"
    assert detect_profile(["Deal Name"]) == "unknown"
    assert detect_profile(["Internal Reference", "Cost"]) == "product"
    assert detect_profile(["Partita IVA"]) == "customer"
    assert detect_profile(["vat"]) == "customer"
    assert detect_profile(["Delivery Lead Time"]) == "supplier"
    assert detect_profile(["Quantity On Hand"]) == "stock"
    assert detect_profile(["Quantity On Hand", "Cost"]) == "product"
    assert detect_profile(["Quantity", "Price"]) == "order_line"
    assert detect_profile(["Quantità prodotti"]) == "order_line"
    assert detect_profile(["TrackingNumber"]) == "shipment"


def test_synonym_score_is_a_rule_score_for_the_documented_header():
    book = synonym_book()
    assert book.rule == SYNONYM_HEADER_RULE
    folded = book.score_kind.casefold()
    assert "punteggio di regola" in folded
    assert "non probabil" in folded
    found = assigned_mapping("customer", "Company Name")
    assert found == ("Customer", "company_name", 0.93, SYNONYM_HEADER_RULE)
    assert assigned_mapping("customer", "Partita Iva")[:3] == ("Customer", "vat", 0.96)
    assert assigned_mapping("supplier", "Delivery Lead Time") == (
        "Supplier",
        "delay_days",
        0.92,
        SYNONYM_HEADER_RULE,
    )


def test_name_parts_stay_below_threshold_and_do_not_overwrite():
    model = load_merged()
    assert model.prop("Person", "given_name") is None
    assert model.prop("Person", "family_name") is None
    book = synonym_book()
    parts = {(item.header, item.part) for item in book.name_parts}
    assert ("First Name", "given_name") in parts
    assert ("Last Name", "family_name") in parts
    assert ("firstname", "given_name") in parts
    assert ("lastname", "family_name") in parts
    assert {item.part for item in book.name_parts if item.header in {"First Name", "firstname"}} == {"given_name"}
    assert {item.part for item in book.name_parts if item.header in {"Last Name", "lastname"}} == {"family_name"}
    stored: dict[str, str] = {}
    row = {
        "First Name": "Ada",
        "Last Name": "Lovelace",
        "firstname": "Grace",
        "lastname": "Hopper",
    }
    for column, raw in row.items():
        target_type, prop, score, rule = assigned_mapping("customer", column)
        assert (target_type, prop) != ("Person", "full_name")
        assert score < THRESHOLD
        assert rule == UNKNOWN_HEADER_RULE
        if score < THRESHOLD:
            continue
        assert prop not in stored
        stored[prop] = raw
    assert stored == {}
    assert "full_name" not in stored


def test_prezzo_unitario_is_normalized_and_stays_unknown():
    assert normalize_header("PrezzoUnitario") == "prezzo unitario"
    assert "prezzo unitario" not in synonym_headers()
    assert assigned_mapping("order_line", "PrezzoUnitario") == (
        "Document",
        "title",
        0.42,
        UNKNOWN_HEADER_RULE,
    )


def test_synonym_sources_are_odoo_hubspot_danea_and_schema_org():
    book = synonym_book()
    origins = {entry.source for entry in book.entries} | {part.source for part in book.name_parts}
    assert origins <= ALLOWED_SOURCES
    for source in origins:
        assert source.startswith("file:") or source.startswith("doc:")
        folded = source.casefold()
        assert "shopify" not in folded
        assert "fatturapa" not in folded
        assert "woocommerce" not in folded
        assert "magento" not in folded


def test_sku_and_price_cite_schema_org_not_woocommerce():
    book = synonym_book()
    schema = [
        entry
        for entry in book.entries
        if entry.source.startswith("doc:https://schema.org/")
    ]
    assert {(entry.normalized, entry.source) for entry in schema} >= {
        ("sku", "doc:https://schema.org/sku"),
        ("price", "doc:https://schema.org/price"),
    }
    assert "regular price" not in {entry.normalized for entry in book.entries}
    assert "regular price" not in synonym_headers()
    sku = next(entry for entry in schema if entry.normalized == "sku")
    assert sku.target_type == "Product"
    assert sku.target_property == "source_key"
    assert sku.profile == "product"
    prices = [entry for entry in schema if entry.normalized == "price"]
    assert prices
    assert {(entry.target_type, entry.target_property) for entry in prices} == {
        ("OrderLine", "unit_price")
    }
    for entry in book.entries:
        folded = f"{entry.header} {entry.source}".casefold()
        assert "woocommerce" not in folded
        assert "magento" not in folded
        assert "regular price" not in folded
    assert assigned_mapping("product", "SKU") == (
        "Product",
        "source_key",
        0.94,
        SYNONYM_HEADER_RULE,
    )
    assert assigned_mapping("product", "price") == (
        "OrderLine",
        "unit_price",
        0.90,
        SYNONYM_HEADER_RULE,
    )
    # Regular price è il nome WooCommerce, non Offer.price. Non è un sinonimo.
    regular = assigned_mapping("product", "Regular price")
    assert regular[3] != SYNONYM_HEADER_RULE
    assert regular[2] < THRESHOLD


def test_normalized_holdout_header_is_not_a_synonym_key():
    report = holdout_report()
    dictionary = synonym_headers()
    leaked = sorted(
        {row["normalized"] for row in report["columns"] if row["normalized"] in dictionary}
    )
    assert leaked == []


def test_synonym_headers_are_unique_per_profile_and_cover_both_languages():
    book = synonym_book()
    grouped: dict[tuple[str, str], set[tuple[str, str]]] = {}
    languages: dict[tuple[str, str], set[str]] = {}
    for entry in book.entries:
        grouped.setdefault((entry.profile, entry.normalized), set()).add(
            (entry.target_type, entry.target_property)
        )
        languages.setdefault((entry.target_type, entry.target_property), set()).add(entry.language)
    collisions = {key: targets for key, targets in grouped.items() if len(targets) != 1}
    assert collisions == {}
    missing = {pair: langs for pair, langs in languages.items() if langs != {"it", "en"}}
    assert missing == {}


def test_every_ontology_property_is_a_synonym_or_uncovered():
    book = synonym_book()
    covered = {(entry.target_type, entry.target_property) for entry in book.entries}
    uncovered = {(item.target_type, item.target_property) for item in book.uncovered}
    assert covered.isdisjoint(uncovered)
    model = load_merged()
    missing = []
    for type_name, type_def in model.types.items():
        for prop_name in type_def.properties:
            pair = (type_name, prop_name)
            if pair not in covered and pair not in uncovered:
                missing.append(pair)
    assert missing == []


def test_holdout_measure_stays_unmeasured_with_two_sources():
    assert MIN_MEASURED_SOURCES == 3
    assert MIN_MEASURED_CASES == 30
    assert THRESHOLD == 0.75
    report = holdout_report()
    assert report["label"] == "PROVVISORIA"
    assert report["calibrated"] is False
    assert report["human_labels"] is False
    assert report["source_count"] == 2
    assert report["source_count"] < MIN_MEASURED_SOURCES
    assert report["measured"] is False
    assert report["threshold_status"] == "non misurata"
    assert report["threshold"] == 0.75
    assert report["minimum_sources"] == 3
    assert report["minimum_cases"] == 30
    assert report["before_token"]["above"] == 2
    assert report["before_token"]["below"] == 30
    assert report["token_vocabulary_added_after_first_measure"] is True
    assert report["after_token"]["above"] == 2
    assert report["after_token"]["below"] == 30
    assert report["above"] == 2
    assert report["below"] == 30
    priced = next(row for row in report["columns"] if row["header"] == "Lineitem price")
    assert priced["rule"] == TOKEN_RULE
    assert priced["score"] == 0.62
    assert priced["above_threshold"] is False
    above = {(row["source"], row["header"], row["rule"], row["score"]) for row in report["columns"] if row["above_threshold"]}
    assert above == {
        ("shopify_order_export", "Email", NORMALIZED_HEADER_RULE, 0.90),
        ("fatturapa_1_2_2", "Quantita", NORMALIZED_HEADER_RULE, 0.93),
    }
    assert detect_profile(
        [row["header"] for row in report["columns"] if row["source"] == "fatturapa_1_2_2"]
    ) == "order_line"
    assert detect_profile(
        [row["header"] for row in report["columns"] if row["source"] == "shopify_order_export"]
    ) == "unknown"


def test_token_rule_stays_below_synonyms_and_ignores_shopify_names():
    book = token_book()
    synonyms = synonym_book()
    assert book.rule == TOKEN_RULE
    assert book.added_after_holdout is True
    assert book.entries
    floor = min(entry.score for entry in synonyms.entries)
    assert all(entry.score < floor for entry in book.entries)
    assert all(entry.score < THRESHOLD for entry in book.entries)
    for entry in (*book.entries, *book.unmapped):
        assert entry.source.startswith("doc:https://schema.org/")
        folded = f"{entry.term} {entry.source}".casefold()
        assert "shopify" not in folded
        assert "fatturapa" not in folded
        assert "lineitem" not in folded
        assert "prezzounitario" not in folded
    terms = {entry.term for entry in book.entries} | {item.term for item in book.unmapped}
    assert {
        "orderNumber",
        "orderDate",
        "price",
        "orderQuantity",
        "priceCurrency",
        "totalPaymentDue",
        "taxID",
        "vatID",
        "email",
        "telephone",
        "postalCode",
        "addressLocality",
    } <= terms
    assert assigned_mapping("order", "orderNumber") == (
        "Order",
        "source_key",
        0.64,
        TOKEN_RULE,
    )
    assert assigned_mapping("order_line", "Price")[:3] == ("OrderLine", "unit_price", 0.90)
    assert assigned_mapping("order_line", "Price")[3] == SYNONYM_HEADER_RULE
    assert assigned_mapping("unknown", "price email") == (
        "Document",
        "title",
        0.42,
        UNKNOWN_HEADER_RULE,
    )
    assert assigned_mapping("customer", "telephone")[3] == UNKNOWN_HEADER_RULE
    assert assigned_mapping("unknown", "Lineitem price") == (
        "OrderLine",
        "unit_price",
        0.62,
        TOKEN_RULE,
    )


def test_holdout_module_does_not_write_mapping_decisions():
    source = (ROOT / "services" / "mapping" / "holdout.py").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "holdout_mapping.py").read_text(encoding="utf-8")
    for text in (source, script):
        assert "mapping_decisions" not in text
        assert "mapping_thresholds" not in text
        assert "evaluate_testset" not in text
