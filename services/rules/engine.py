"""Operational rules. The first rule requires CFO approval above 50 000 €."""

from __future__ import annotations

RULES_VERSION = "rules.cfo_amount@v1"
CFO_THRESHOLD = 50_000


def requires_cfo(amount: int | float | None) -> bool:
    if amount is None:
        return False
    return amount > CFO_THRESHOLD


def decide_rules_only(context: dict) -> dict:
    """First supplier in list order. The agent instead picks the fastest."""
    groups: dict[str, int | float] = {}
    for line in context.get("lines") or []:
        shortage = line.get("shortage") or 0
        if shortage <= 0:
            continue
        product = line["product"]
        groups[product] = groups.get(product, 0) + shortage
    actions = []
    for product, shortage in groups.items():
        suppliers = [item for item in context.get("suppliers") or [] if item.get("product") == product]
        chosen = suppliers[0] if suppliers else None
        if chosen is None:
            continue
        actions.append(
            {
                "type": "createPurchaseOrder",
                "params": {
                    "supplier": chosen["source_key"],
                    "product": product,
                    "quantity": shortage,
                    "order": context.get("order_key"),
                },
            }
        )
    if groups:
        actions.append(
            {
                "type": "notifyCustomer",
                "params": {
                    "order": context.get("order_key"),
                    "customer": context.get("customer_key"),
                },
            }
        )
    return {"actions": actions}
