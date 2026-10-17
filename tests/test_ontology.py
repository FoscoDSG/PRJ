from pathlib import Path

import pytest

from ontology.loader import OntologyError, _apply_extension, load_merged


def test_base_package_is_not_rewritten_by_extensions():
    path = Path("ontology/manufacturing.yaml")
    before = path.read_bytes()
    model = load_merged()
    assert path.read_bytes() == before
    assert model.prop("Order", "commessa") is not None
    assert model.prop("Order", "amount").classification == "confidential"
    assert "approve" in model.types["Order"].actions


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
