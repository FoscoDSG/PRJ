"""Read model of the ontology for a later page. The page does not invent steps."""

from __future__ import annotations

from typing import Any

from ontology.loader import Ontology


def describe_ontology(model: Ontology, company_id: str | None) -> dict[str, Any]:
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
                "package": type_def.package,
                "extends": type_def.extends,
                "fields": fields,
            }
        )
    processes = []
    for process_id in sorted(model.processes):
        process = model.processes[process_id]
        processes.append(
            {
                "id": process.id,
                "label": process.label,
                "package": process.package,
                "steps": [{"id": step.id, "label": step.label} for step in process.steps],
            }
        )
    extensions = []
    for package in model.packages:
        extended = package.raw.get("extends_types") or []
        if not extended or package.raw.get("types"):
            continue
        extensions.append(
            {
                "package": package.name,
                "version": package.version,
                "types": [item["name"] for item in extended],
            }
        )
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
        "extensions": extensions,
        "company_id": company_id,
    }


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
