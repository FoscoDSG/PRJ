"""Fixed Italian labels for a recognized table type.

The words are chosen here. They are never copied from a file name, a column
name, or a cell.
"""

from __future__ import annotations

PROFILE_LABELS = {
    "customer": "Probabile anagrafica clienti",
    "order": "Probabile export ordini",
    "order_line": "Probabile righe d'ordine",
    "product": "Probabile anagrafica prodotti",
    "stock": "Probabile giacenze",
    "supplier": "Probabile anagrafica fornitori",
    "shipment": "Probabile spedizioni",
}
UNKNOWN_PROFILE_LABEL = "Tipo non riconosciuto"


def profile_label(profile: str | None) -> str:
    if profile is None:
        return UNKNOWN_PROFILE_LABEL
    return PROFILE_LABELS.get(str(profile), UNKNOWN_PROFILE_LABEL)
