"""Process and step ids declared by the ontology packages.

The inbox copies the label from the loaded process, it does not invent one.
"""

ORDER_PROCESS_ID = "order_exception"
ORDER_STEP_ID = "shortage"
CASH_PROCESS_ID = "order_to_cash"
STEP_INVOICED = "invoiced"
STEP_CREDITED = "credited"
STEP_PAYMENT = "payment_received"
STEP_MATCH = "match_proposed"
STEP_CONFIRMED = "match_confirmed"
