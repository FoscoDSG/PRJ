from pathlib import Path

import pytest

from ontology.loader import OntologyError, _apply_extension, load_merged
from services.ontology_view import (
    FIELD_LABEL_OVERRIDES,
    FIELD_LABELS,
    TYPE_LABELS,
    describe_ontology,
    is_incoming,
    payment_label,
    relation_label,
)


def test_base_package_is_not_rewritten_by_extensions():
    path = Path("ontology/manufacturing.yaml")
    before = path.read_bytes()
    model = load_merged()
    assert path.read_bytes() == before
    assert model.prop("Order", "commessa") is not None
    assert model.prop("Order", "amount").classification == "confidential"
    assert "approve" in model.types["Order"].actions


def test_shipped_ontology_names_every_type_and_field_in_italian():
    model = load_merged()
    missing_types = sorted(name for name in model.types if not TYPE_LABELS.get(name))
    missing_fields = sorted(
        f"{type_name}.{prop_name}"
        for type_name, type_def in model.types.items()
        for prop_name in type_def.properties
        if (type_name, prop_name) not in FIELD_LABEL_OVERRIDES and not FIELD_LABELS.get(prop_name)
    )
    assert missing_types == []
    assert missing_fields == []


def test_ontology_read_falls_back_to_the_technical_name():
    model = load_merged()
    removed_type = TYPE_LABELS.pop("Invoice")
    removed_field = FIELD_LABELS.pop("causale")
    try:
        catalog = describe_ontology(model, None)
    finally:
        TYPE_LABELS["Invoice"] = removed_type
        FIELD_LABELS["causale"] = removed_field
    invoice = next(item for item in catalog["types"] if item["name"] == "Invoice")
    payment = next(item for item in catalog["types"] if item["name"] == "Payment")
    causale = next(field for field in payment["fields"] if field["name"] == "causale")
    assert invoice["label"] == "Invoice"
    assert causale["label"] == "causale"
    generates = next(item for item in catalog["relations"] if item["name"] == "generates")
    allocates = next(item for item in catalog["relations"] if item["name"] == "allocates")
    assert generates["label"] == "genera"
    assert allocates["label"] == "assegna"
    assert relation_label("not_a_relation") == "not_a_relation"
    assert payment_label("incoming") == "Incasso"
    assert payment_label("outgoing") == "Pagamento"
    assert payment_label(None) == "Incasso"
    assert payment_label("") == "Incasso"
    assert is_incoming({}) is True
    assert is_incoming({"direction": None}) is True
    assert is_incoming({"direction": ""}) is True
    assert is_incoming({"direction": "incoming"}) is True
    assert is_incoming("incoming") is True
    assert is_incoming({"direction": "outgoing"}) is False
    assert is_incoming("outgoing") is False
    assert catalog["payment_labels"]["outgoing"] == "Pagamento"


def test_extension_cannot_override_a_base_property():
    model = load_merged()
    with pytest.raises(OntologyError):
        _apply_extension(
            model.types,
            {
                "name": "Order",
                "properties": [
                    {
                        "name": "amount",
                        "type": "string",
                        "classification": "public",
                        "origin": "original",
                    }
                ],
            },
            "bad_extension",
        )
