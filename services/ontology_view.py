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
        "relations": list(model.relations),
        "processes": processes,
        "step_transitions": step_transitions,
        "extensions": extensions,
        "company_id": company_id,
    }


def type_label(name: str) -> str:
    label = TYPE_LABELS.get(name)
    if not label:
        raise KeyError(name)
    return label


def field_label(type_name: str, prop_name: str) -> str:
    label = FIELD_LABEL_OVERRIDES.get((type_name, prop_name)) or FIELD_LABELS.get(prop_name)
    if not label:
        raise KeyError(f"{type_name}.{prop_name}")
    return label


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
