"""Deterministic model used in tests and in the local demo."""

from __future__ import annotations

import json
from typing import Any


class StubOrderModel:
    """Read the context embedded in the prompt and return a schema-shaped object."""

    def complete_structured(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        context = json.loads(prompt.split("CONTEXT_JSON=", 1)[1])
        short_lines = [line for line in context["lines"] if line["shortage"] > 0]
        if not short_lines:
            return {
                "problem": "Nessuna eccezione di disponibilità.",
                "evidence": context["evidence"],
                "actions": [],
                "expected_impact": "Nessun intervento.",
                "risk": "low",
                "confidence": 0.9,
            }
        product = short_lines[0]["product"]
        shortage = sum(line["shortage"] for line in short_lines if line["product"] == product)
        suppliers = [item for item in context["suppliers"] if item["product"] == product]
        chosen = min(suppliers, key=lambda item: item["delay_days"]) if suppliers else None
        actions: list[dict[str, Any]] = []
        if chosen is not None:
            actions.append(
                {
                    "type": "createPurchaseOrder",
                    "params": {
                        "supplier": chosen["source_key"],
                        "product": product,
                        "quantity": shortage,
                        "order": context["order_key"],
                    },
                }
            )
        actions.append(
            {
                "type": "notifyCustomer",
                "params": {
                    "order": context["order_key"],
                    "customer": context["customer_key"],
                },
            }
        )
        delay = chosen["delay_days"] if chosen else 0
        risk = "high" if context["amount"] > 50_000 or delay >= 10 else "medium"
        return {
            "problem": (
                f"Ordine {context['order_key']}: mancano {shortage} unità di {product}. "
                f"Il fornitore più rapido è {chosen['source_key'] if chosen else 'sconosciuto'}."
            ),
            "evidence": context["evidence"],
            "actions": actions,
            "expected_impact": f"Coprire lo shortage di {shortage} unità sull'ordine {context['order_key']}.",
            "risk": risk,
            "confidence": 0.86 if chosen else 0.55,
        }
