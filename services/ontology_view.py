"""Read model of the ontology for a later page. The page does not invent steps."""

from __future__ import annotations

from typing import Any

import psycopg

from ontology.loader import Ontology

TYPE_LABELS = {
    "Person": "Persona",
    "Organization": "Organizzazione",
    "Asset": "Bene",
    "Document": "Documento",
    "Event": "Evento",
    "Location": "Luogo",
    "Transaction": "Transazione",
    "Customer": "Cliente",
    "Supplier": "Fornitore",
    "Product": "Prodotto",
    "Warehouse": "Magazzino",
    "Order": "Ordine",
    "OrderLine": "Riga d'ordine",
    "StockPosition": "Giacenza",
    "Shipment": "Spedizione",
    "Invoice": "Fattura",
    "InvoiceLine": "Riga fattura",
    "CreditNote": "Nota di credito",
    "Payment": "Incasso",
    "PaymentAllocation": "Assegnazione incasso",
    "PurchaseRequest": "Richiesta di acquisto",
    "PurchaseOrder": "Ordine di acquisto",
    "PurchaseOrderLine": "Riga ordine di acquisto",
    "GoodsReceipt": "Ricevimento",
    "GoodsReceiptLine": "Riga di ricevimento",
    "SupplierInvoice": "Fattura fornitore",
    "SupplierInvoiceLine": "Riga fattura fornitore",
}

# Italian names for fields. A page reads these; it does not translate the key.
FIELD_LABELS = {
    "name": "Nome",
    "title": "Titolo",
    "full_name": "Nome e cognome",
    "amount": "Importo",
    "source_key": "Chiave",
    "status": "Stato",
    "email": "Email",
    "vat": "Partita IVA",
    "company_name": "Ragione sociale",
    "customer_vat": "Partita IVA cliente",
    "customer_name": "Nome cliente",
    "document_type": "Tipo documento",
    "invoice_source_key": "Numero fattura",
    "issued_at": "Data",
    "reason": "Motivo",
    "currency": "Valuta",
    "due_at": "Scadenza",
    "scadenze": "Scadenze",
    "order_source_key": "Numero ordine",
    "description": "Descrizione",
    "line_amount": "Importo riga",
    "quantity": "Quantità",
    "unit_price": "Prezzo unitario",
    "vat_rate": "Aliquota IVA",
    "event_type": "Tipo evento",
    "occurred_at": "Data evento",
    "process_id": "Processo",
    "step_id": "Passo",
    "summary": "Sintesi",
    "commessa": "Commessa",
    "customer_source_key": "Cliente",
    "margin": "Margine",
    "ordered_at": "Data ordine",
    "shortage_qty": "Quantità mancante",
    "product_source_key": "Prodotto",
    "shortage": "Mancanza",
    "causale": "Causale",
    "counterparty_name": "Nome controparte",
    "counterparty_vat": "Partita IVA controparte",
    "direction": "Direzione",
    "iban": "IBAN",
    "paid_at": "Data incasso",
    "reference": "Riferimento",
    "allocated_amount": "Importo assegnato",
    "payment_source_key": "Incasso",
    "cost": "Costo",
    "shipped_at": "Data spedizione",
    "available": "Disponibile",
    "on_hand": "Giacenza",
    "reserved": "Impegnato",
    "warehouse_source_key": "Magazzino",
    "delay_days": "Giorni di ritardo",
    "requested_at": "Data richiesta",
    "requester": "Richiedente",
    "purchase_request_source_key": "Richiesta di acquisto",
    "supplier_source_key": "Fornitore",
    "supplier_vat": "Partita IVA fornitore",
    "supplier_name": "Nome fornitore",
    "received_at": "Data ricevimento",
    "receipt_source_key": "Ricevimento",
    "order_line_source_key": "Riga ordine",
    "invoice_number": "Numero fattura fornitore",
    "invoice_year": "Anno",
    "supplier_invoice_source_key": "Fattura fornitore",
}

FIELD_LABEL_OVERRIDES = {
    ("Invoice", "amount"): "Importo fattura",
    ("Invoice", "source_key"): "Numero fattura",
    ("InvoiceLine", "amount"): "Importo ereditato",
    ("CreditNote", "amount"): "Importo nota",
    ("CreditNote", "source_key"): "Numero nota",
    ("Payment", "amount"): "Importo incasso",
    ("Payment", "source_key"): "Numero incasso",
    ("PaymentAllocation", "amount"): "Importo ereditato",
    ("Order", "amount"): "Importo ordine",
    ("Order", "source_key"): "Numero ordine",
    ("OrderLine", "amount"): "Importo ereditato",
    ("PurchaseOrder", "amount"): "Importo ordine di acquisto",
    ("PurchaseOrder", "source_key"): "Numero ordine di acquisto",
    ("PurchaseRequest", "source_key"): "Numero richiesta",
    ("SupplierInvoice", "amount"): "Importo fattura fornitore",
    ("SupplierInvoice", "source_key"): "Chiave fattura fornitore",
}

# Italian names of relations. A page reads these and falls back to the technical name.
RELATION_LABELS = {
    "places": "effettua",
    "contains": "contiene",
    "suppliedBy": "fornito da",
    "storedAt": "depositato in",
    "triggered": "genera evento",
    "generates": "genera",
    "fulfills": "evade",
    "allocates": "assegna",
    "settles": "salda",
    "sameOrganization": "stessa organizzazione",
    "requests": "richiede",
    "orderedFrom": "ordinato a",
    "receives": "riceve",
    "billsFor": "fattura",
}

PAYMENT_DIRECTION_LABELS = {
    "incoming": "Incasso",
    "outgoing": "Pagamento",
    "movements": "Movimenti",
}

PAYMENT_OUTGOING_FIELDS = {
    "amount": "Importo pagamento",
    "source_key": "Numero pagamento",
    "paid_at": "Data pagamento",
}

# Shown words for a stored code. «open» is not the same word on every type.
VALUE_LABELS = {
    ("Order", "status"): {"open": "Aperto", "closed": "Chiuso"},
    ("Invoice", "status"): {
        "open": "Aperta",
        "issued": "Emessa",
        "paid": "Pagata",
        "closed": "Chiusa",
    },
    ("CreditNote", "status"): {"open": "Aperta", "issued": "Emessa"},
    ("Payment", "status"): {"received": "Ricevuto", "open": "In corso"},
    ("Shipment", "status"): {"open": "In corso"},
    ("Payment", "direction"): {"incoming": "In entrata", "outgoing": "In uscita"},
}

# A link reads as a sentence. `out` leaves this object, `in` arrives here.
RELATION_PHRASES = {
    "places": {"out": "effettua", "in": "effettuato da"},
    "contains": {"out": "contiene", "in": "contenuto in"},
    "suppliedBy": {"out": "fornito da", "in": "fornisce"},
    "storedAt": {"out": "depositato in", "in": "ospita"},
    "triggered": {"out": "genera", "in": "generato da"},
    "generates": {"out": "genera", "in": "generata da"},
    "fulfills": {"out": "evade", "in": "evaso da"},
    "allocates": {"out": "assegna", "in": "assegnato da"},
    "settles": {"out": "salda", "in": "saldato da"},
    "sameOrganization": {"out": "stessa organizzazione di", "in": "stessa organizzazione di"},
}


def describe_ontology(
    model: Ontology,
    company_id: str | None,
    extension_package: str | None = None,
) -> dict[str, Any]:
    """Types, fields, step transitions and this company's extensions.

    ``extension_package`` is the package stored on the caller's company.
    Another company does not receive it. A caller without a company
    receives none.
    """
    types = []
    for name in sorted(model.types):
        type_def = model.types[name]
        fields = []
        for prop_name in sorted(type_def.properties):
            prop = type_def.properties[prop_name]
            computed = prop.origin == "calculated"
            fields.append(
                {
                    "name": prop.name,
                    "label": field_label(name, prop.name),
                    "type": prop.type,
                    "unit": prop.unit,
                    "classification": prop.classification,
                    "origin": prop.origin,
                    "from_source": not computed,
                    "computed": computed,
                }
            )
        types.append(
            {
                "name": type_def.name,
                "label": type_label(name),
                "package": type_def.package,
                "extends": type_def.extends,
                "fields": fields,
            }
        )
    processes = []
    step_transitions = []
    for process_id in sorted(model.processes):
        process = model.processes[process_id]
        steps = [{"id": step.id, "label": step.label} for step in process.steps]
        transitions = [
            {"from": steps[index]["id"], "to": steps[index + 1]["id"]}
            for index in range(len(steps) - 1)
        ]
        for link in transitions:
            step_transitions.append({"process": process.id, **link})
        processes.append(
            {
                "id": process.id,
                "label": process.label,
                "package": process.package,
                "steps": steps,
                "transitions": transitions,
            }
        )
    extensions = _company_extensions(model, extension_package)
    return {
        "version": "+".join(f"{package.name}@{package.version}" for package in model.packages),
        "packages": [
            {
                "name": package.name,
                "version": package.version,
                "depends_on": list(package.raw.get("depends_on") or []),
            }
            for package in model.packages
        ],
        "types": types,
        "relations": [_relation_view(item) for item in model.relations],
        "value_labels": _value_labels(),
        "payment_labels": dict(PAYMENT_DIRECTION_LABELS),
        "payment_field_labels": {"outgoing": dict(PAYMENT_OUTGOING_FIELDS)},
        "processes": processes,
        "step_transitions": step_transitions,
        "extensions": extensions,
        "company_id": company_id,
    }


def type_label(name: str) -> str:
    """Italian name, or the technical name when a module has not been translated yet."""
    return TYPE_LABELS.get(name) or name


def field_label(type_name: str, prop_name: str) -> str:
    """Italian name, or the technical name when a module has not been translated yet."""
    return (
        FIELD_LABEL_OVERRIDES.get((type_name, prop_name))
        or FIELD_LABELS.get(prop_name)
        or prop_name
    )


def relation_label(name: str | None) -> str:
    """Italian name of a link, or the technical name when it has no translation."""
    if not name:
        return ""
    return RELATION_LABELS.get(name) or name


def _relation_view(item: dict) -> dict:
    name = str(item.get("name") or "")
    phrases = RELATION_PHRASES.get(name) or {}
    label = relation_label(name)
    return {
        **dict(item),
        "label": label,
        "out": phrases.get("out") or label,
        "in": phrases.get("in") or label,
    }


def _value_labels() -> dict[str, dict[str, dict[str, str]]]:
    found: dict[str, dict[str, dict[str, str]]] = {}
    for (type_name, prop_name), labels in VALUE_LABELS.items():
        found.setdefault(type_name, {})[prop_name] = dict(labels)
    return found


def is_incoming(payment: Any) -> bool:
    """A payment leaves the company only when direction is exactly ``outgoing``.

    ``incoming``, an empty direction and a missing direction all count as
    money coming in. The label is then Incasso. Old payments written before
    a direction existed stay in the measure and in the match rule.
    """
    if isinstance(payment, dict):
        value = payment.get("direction")
    else:
        value = payment
    return value != "outgoing"


def payment_label(direction: str | None) -> str:
    """Incasso when money comes in, Pagamento when it goes out."""
    if is_incoming(direction):
        return PAYMENT_DIRECTION_LABELS["incoming"]
    return PAYMENT_DIRECTION_LABELS["outgoing"]


def payment_words(text: str, direction: str | None) -> str:
    """Rewrite a stored sentence when this payment leaves the company."""
    if is_incoming(direction) or not text:
        return text
    return text.replace("Incasso", "Pagamento").replace("incasso", "pagamento")


def extension_package_names(model: Ontology) -> set[str]:
    """Packages that only add fields. A package with its own types is not one."""
    names = set()
    for package in model.packages:
        extended = package.raw.get("extends_types") or []
        if extended and not package.raw.get("types"):
            names.add(package.name)
    return names


def extension_for_company(conn: psycopg.Connection, company_id: str | None) -> str | None:
    """Extension package of this company. None when the company has none."""
    if not company_id:
        return None
    row = conn.execute(
        "SELECT extension_package FROM app.companies WHERE id = %s::uuid",
        (company_id,),
    ).fetchone()
    if row is None:
        return None
    return row["extension_package"]


def _company_extensions(model: Ontology, extension_package: str | None) -> list[dict[str, Any]]:
    if not extension_package:
        return []
    if extension_package not in extension_package_names(model):
        return []
    package = next(item for item in model.packages if item.name == extension_package)
    extended = package.raw.get("extends_types") or []
    return [
        {
            "package": package.name,
            "version": package.version,
            "types": [item["name"] for item in extended],
        }
    ]


def catalog_pair(model: Ontology, process_id: str, step_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """The same process and step objects the read endpoint returns."""
    process = model.process(process_id)
    if process is None:
        raise KeyError(process_id)
    step = process.step(step_id)
    if step is None:
        raise KeyError(step_id)
    return (
        {"id": process.id, "label": process.label, "package": process.package},
        {"id": step.id, "label": step.label},
    )
