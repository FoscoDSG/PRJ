"""Load versioned ontology packages. Extensions add properties; they do not rewrite base files."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ONTOLOGY_DIR = ROOT / "ontology"


class OntologyError(ValueError):
    pass


@dataclass
class PropertyDef:
    name: str
    type: str
    classification: str
    origin: str
    identifier: bool = False
    identity: bool = False


@dataclass
class TypeDef:
    name: str
    extends: str | None
    properties: dict[str, PropertyDef] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    package: str = ""


@dataclass
class ProcessStep:
    id: str
    label: str


@dataclass
class ProcessDef:
    id: str
    label: str
    package: str
    steps: list[ProcessStep] = field(default_factory=list)

    def step(self, step_id: str) -> ProcessStep | None:
        for item in self.steps:
            if item.id == step_id:
                return item
        return None


@dataclass
class Package:
    name: str
    version: str
    changelog: str
    path: Path
    raw: dict


@dataclass
class Ontology:
    packages: list[Package]
    types: dict[str, TypeDef]
    relations: list[dict]
    processes: dict[str, ProcessDef] = field(default_factory=dict)

    def process(self, process_id: str) -> ProcessDef | None:
        return self.processes.get(process_id)

    def prop(self, type_name: str, property_name: str) -> PropertyDef | None:
        type_def = self.types.get(type_name)
        if type_def is None:
            return None
        return type_def.properties.get(property_name)

    def require_prop(self, type_name: str, property_name: str) -> PropertyDef:
        found = self.prop(type_name, property_name)
        if found is None:
            raise OntologyError(f"Proprietà sconosciuta: {type_name}.{property_name}")
        return found


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise OntologyError(f"Pacchetto non valido: {path}")
    return data


def _property(raw: dict, package: str) -> PropertyDef:
    return PropertyDef(
        name=raw["name"],
        type=raw.get("type", "string"),
        classification=raw.get("classification", "internal"),
        origin=raw.get("origin", "original"),
        identifier=bool(raw.get("identifier", False)),
        identity=bool(raw.get("identity", False)),
    )


def _inherit(types: dict[str, TypeDef], type_def: TypeDef) -> None:
    if not type_def.extends:
        return
    parent = types.get(type_def.extends)
    if parent is None:
        raise OntologyError(f"{type_def.name} estende un tipo assente: {type_def.extends}")
    _inherit(types, parent)
    for name, prop in parent.properties.items():
        type_def.properties.setdefault(name, copy.deepcopy(prop))


# Dizionari di mapping, non pacchetti. Un file con `package` è un pacchetto.
_NOT_PACKAGES = frozenset({"synonyms.yaml", "tokens.yaml", "table_shape.yaml"})


def _package_paths(folder: Path) -> list[Path]:
    paths = [path for path in sorted(folder.glob("*.yaml")) if path.name not in _NOT_PACKAGES]
    extension_dir = folder / "extensions"
    if extension_dir.exists():
        paths.extend(path for path in sorted(extension_dir.glob("*.yaml")) if path.name not in _NOT_PACKAGES)
    return paths


def _order_by_depends(packages: list[Package]) -> list[Package]:
    """Parent packages first. `depends_on` is the only order."""
    by_name = {item.name: item for item in packages}
    if len(by_name) != len(packages):
        raise OntologyError("Due pacchetti hanno lo stesso nome")
    ordered: list[Package] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise OntologyError(f"Dipendenza ciclica dell'ontologia: {name}")
        package = by_name.get(name)
        if package is None:
            raise OntologyError(f"Dipendenza assente: {name}")
        visiting.add(name)
        depends = package.raw.get("depends_on") or []
        if not isinstance(depends, list):
            raise OntologyError(f"depends_on non è un elenco: {name}")
        for dep in depends:
            if not isinstance(dep, str) or dep.strip() == "":
                raise OntologyError(f"depends_on non valido in {name}")
            visit(dep)
        visiting.remove(name)
        visited.add(name)
        ordered.append(package)

    for package in packages:
        visit(package.name)
    return ordered


def _processes(raw: dict, package: str) -> list[ProcessDef]:
    found: list[ProcessDef] = []
    seen: set[str] = set()
    for item in raw.get("processes") or []:
        process_id = str(item["id"])
        if process_id in seen:
            raise OntologyError(f"Passo di processo duplicato: {package}.{process_id}")
        seen.add(process_id)
        steps: list[ProcessStep] = []
        step_ids: set[str] = set()
        for step in item.get("steps") or []:
            step_id = str(step["id"])
            if step_id in step_ids:
                raise OntologyError(f"Passo duplicato: {process_id}.{step_id}")
            step_ids.add(step_id)
            label = str(step.get("label") or "").strip()
            if label == "":
                raise OntologyError(f"Passo senza etichetta: {process_id}.{step_id}")
            steps.append(ProcessStep(id=step_id, label=label))
        if not steps:
            raise OntologyError(f"Processo senza passi: {process_id}")
        label = str(item.get("label") or "").strip()
        if label == "":
            raise OntologyError(f"Processo senza etichetta: {process_id}")
        found.append(ProcessDef(id=process_id, label=label, package=package, steps=steps))
    return found


def load_packages(directory: Path | None = None) -> list[Package]:
    folder = directory or ONTOLOGY_DIR
    packages = []
    for path in _package_paths(folder):
        raw = _read_yaml(path)
        if "package" not in raw:
            raise OntologyError(f"Pacchetto senza nome: {path.name}")
        packages.append(
            Package(
                name=raw["package"],
                version=str(raw["version"]),
                changelog=raw.get("changelog", ""),
                path=path,
                raw=raw,
            )
        )
    return _order_by_depends(packages)


def load_merged(directory: Path | None = None) -> Ontology:
    packages = load_packages(directory)
    types: dict[str, TypeDef] = {}
    relations: list[dict] = []
    processes: dict[str, ProcessDef] = {}
    for package in packages:
        raw = package.raw
        for item in raw.get("types", []):
            type_def = TypeDef(
                name=item["name"],
                extends=item.get("extends"),
                actions=list(item.get("actions", [])),
                package=package.name,
            )
            for prop_raw in item.get("properties", []):
                type_def.properties[prop_raw["name"]] = _property(prop_raw, package.name)
            types[type_def.name] = type_def
        for item in raw.get("extends_types", []):
            _apply_extension(types, item, package.name)
        relations.extend(raw.get("relations", []))
        for process in _processes(raw, package.name):
            if process.id in processes:
                raise OntologyError(f"Processo già dichiarato: {process.id}")
            processes[process.id] = process
    for type_def in types.values():
        _inherit(types, type_def)
    return Ontology(packages=packages, types=types, relations=relations, processes=processes)


def _apply_extension(types: dict[str, TypeDef], item: dict, package: str) -> None:
    name = item["name"]
    current = types.get(name)
    if current is None:
        raise OntologyError(f"L'estensione {package} cita un tipo assente: {name}")
    for prop_raw in item.get("properties", []):
        prop = _property(prop_raw, package)
        existing = current.properties.get(prop.name)
        if existing is None:
            current.properties[prop.name] = prop
            continue
        if (
            existing.type != prop.type
            or existing.classification != prop.classification
            or existing.origin != prop.origin
        ):
            raise OntologyError(
                f"L'estensione {package} non può ridefinire {name}.{prop.name}"
            )
