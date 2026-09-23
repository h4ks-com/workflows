from dataclasses import dataclass

from pydantic import JsonValue

from workflows.db import JsonObject
from workflows.jobtypes import TEXTAREA


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    description: str
    kind: str
    required: bool
    enum: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    default: JsonValue = None


def _as_object(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _any_of(prop: JsonObject) -> list[JsonValue]:
    branches = prop.get("anyOf")
    return branches if isinstance(branches, list) else []


def _schema_type(prop: JsonObject) -> str | None:
    prop_type = prop.get("type")
    if isinstance(prop_type, str):
        return prop_type
    for branch in _any_of(prop):
        branch_type = _as_object(branch).get("type")
        if isinstance(branch_type, str) and branch_type != "null":
            return branch_type
    return None


def _enum(prop: JsonObject) -> tuple[str, ...] | None:
    values = prop.get("enum")
    if isinstance(values, list):
        return tuple(str(value) for value in values)
    for branch in _any_of(prop):
        branch_values = _as_object(branch).get("enum")
        if isinstance(branch_values, list):
            return tuple(str(value) for value in branch_values)
    return None


def _kind(prop: JsonObject, prop_type: str | None, enum: tuple[str, ...] | None) -> str:
    if enum is not None:
        return "select"
    if prop_type == "boolean":
        return "checkbox"
    if prop_type in ("integer", "number"):
        return "number"
    if prop.get("format") == TEXTAREA:
        return "textarea"
    return "text"


def _bound(prop: JsonObject, key: str) -> float | None:
    value = prop.get(key)
    return value if isinstance(value, int | float) else None


def field_specs(schema: JsonObject) -> list[FieldSpec]:
    properties = _as_object(schema.get("properties"))
    required = schema.get("required")
    required_names = set(required) if isinstance(required, list) else set()
    specs = []
    for name, raw_prop in properties.items():
        prop = _as_object(raw_prop)
        prop_type = _schema_type(prop)
        enum = _enum(prop)
        specs.append(
            FieldSpec(
                name=name,
                label=str(prop.get("title", name)),
                description=str(prop.get("description", "")),
                kind=_kind(prop, prop_type, enum),
                required=name in required_names,
                enum=enum,
                minimum=_bound(prop, "minimum"),
                maximum=_bound(prop, "maximum"),
                default=prop.get("default"),
            )
        )
    return specs
