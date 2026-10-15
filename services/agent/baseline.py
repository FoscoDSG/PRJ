"""Rules baseline for the order agent. The labeled eval calls this module, not the stub."""

from __future__ import annotations

from typing import Any


def decide(context: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, int | float] = {}
    for line in context.get("lines") or []:
        shortage = line.get("shortage") or 0
        if shortage <= 0:
            continue
        product = line["product"]
        groups[product] = groups.get(product, 0) + shortage
    if not groups:
        return {
            "problem": "Nessuna eccezione di disponibilità.",
            "evidence": context.get("evidence") or [],
            "actions": [],
            "expected_impact": "Nessun intervento.",
            "risk": "low",
            "confidence": 0.9,
        }
    actions = []
    notes = []
    slowest = 0
    for product, shortage in groups.items():
        suppliers = [item for item in context.get("suppliers") or [] if item.get("product") == product]
        chosen = min(suppliers, key=lambda item: item["delay_days"]) if suppliers else None
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
            slowest = max(slowest, chosen["delay_days"])
            notes.append(f"{shortage} unità di {product} da {chosen['source_key']}")
        else:
            notes.append(f"{shortage} unità di {product} senza fornitore")
    actions.append(
        {
            "type": "notifyCustomer",
            "params": {"order": context["order_key"], "customer": context.get("customer_key")},
        }
    )
    amount = context.get("amount") or 0
    return {
        "problem": f"Ordine {context['order_key']}: " + "; ".join(notes) + ".",
        "evidence": context.get("evidence") or [],
        "actions": actions,
        "expected_impact": f"Coprire lo shortage sull'ordine {context['order_key']}.",
        "risk": "high" if amount > 50_000 or slowest >= 10 else "medium",
        "confidence": 0.86 if any(action["type"] == "createPurchaseOrder" for action in actions) else 0.55,
    }
