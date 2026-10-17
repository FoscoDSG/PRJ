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


def load_packages(directory: Path | None = None) -> list[Package]:
    folder = directory or ONTOLOGY_DIR
    paths = [folder / "core.yaml", folder / "manufacturing.yaml"]
    extension_dir = folder / "extensions"
    if extension_dir.exists():
        paths.extend(sorted(extension_dir.glob("*.yaml")))
    packages = []
    for path in paths:
        raw = _read_yaml(path)
        packages.append(
            Package(
                name=raw["package"],
                version=str(raw["version"]),
                changelog=raw.get("changelog", ""),
                path=path,
                raw=raw,
            )
        )
    return packages


def load_merged(directory: Path | None = None) -> Ontology:
    packages = load_packages(directory)
    types: dict[str, TypeDef] = {}
    relations: list[dict] = []
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
    for type_def in types.values():
        _inherit(types, type_def)
    return Ontology(packages=packages, types=types, relations=relations)


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
