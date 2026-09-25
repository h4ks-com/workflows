from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from typing import Annotated
from typing import ClassVar
from typing import Literal
from typing import Self

from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import HttpUrl
from pydantic import JsonValue
from pydantic import create_model
from pydantic import model_validator
from pydantic.config import JsonDict

from workflows.db import JsonObject

TEXTAREA = "textarea"
UPLOAD = "x-upload"
SHOW_WHEN = "x-show-when"
PREVIEWS = "x-previews"

type FieldKind = Literal["select", "checkbox", "number", "textarea", "text"]
type ValueType = Literal["string", "integer", "number", "boolean", "url"]

VALUE_TYPES: dict[ValueType, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "url": HttpUrl,
}


@dataclass(frozen=True)
class FieldSpec:
    """One form field: how the site renders it and how its value is validated."""

    name: str
    label: str
    description: str
    kind: FieldKind
    required: bool
    value_type: ValueType = "string"
    enum: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    default: JsonValue = None
    accept: str | None = None
    show_when: JsonObject | None = None
    previews: dict[str, str] | None = None


class FormParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    show_rules: ClassVar[tuple[tuple[str, str, JsonObject], ...]] = ()

    @model_validator(mode="after")
    def only_set_when_shown(self) -> Self:
        for name, label, condition in self.show_rules:
            if getattr(self, name) is None:
                continue
            for key, value in condition.items():
                if getattr(self, key) != value:
                    raise ValueError(f"{label.lower()} needs {key} {value}")
        return self


def _one_of(values: tuple[str, ...]) -> Callable[[str | None], str | None]:
    def check(value: str | None) -> str | None:
        if value is not None and value not in values:
            raise ValueError(f"choose one of {', '.join(values)}")
        return value

    return check


def _annotation(spec: FieldSpec) -> object:
    value_type = VALUE_TYPES[spec.value_type]
    nullable = not spec.required and spec.default is None
    annotation = value_type | None if nullable else value_type
    if spec.enum is not None:
        return Annotated[annotation, AfterValidator(_one_of(spec.enum))]
    return annotation


def _schema_extra(spec: FieldSpec) -> JsonDict:
    extra: JsonDict = {}
    if spec.enum is not None:
        extra["enum"] = list(spec.enum)
    if spec.kind == "textarea":
        extra["format"] = TEXTAREA
    if spec.accept is not None:
        extra[UPLOAD] = spec.accept
    if spec.show_when is not None:
        extra[SHOW_WHEN] = spec.show_when
    if spec.previews is not None:
        extra[PREVIEWS] = dict(spec.previews)
    return extra


def _field_info(spec: FieldSpec) -> object:
    lengths = spec.value_type != "url"
    return Field(
        ... if spec.required else spec.default,
        title=spec.label,
        description=spec.description,
        ge=spec.minimum,
        le=spec.maximum,
        min_length=spec.min_length if lengths else None,
        max_length=spec.max_length if lengths else None,
        json_schema_extra=_schema_extra(spec),
    )


@dataclass(frozen=True)
class Form:
    """A job type's form, the one source for the web form, its JSON schema and validation."""

    title: str
    fields: tuple[FieldSpec, ...]

    @cached_property
    def _model(self) -> type[FormParams]:
        definitions = {spec.name: (_annotation(spec), _field_info(spec)) for spec in self.fields}
        model = create_model(self.title, __base__=FormParams, **definitions)  # type: ignore[call-overload]
        model.show_rules = tuple(
            (spec.name, spec.label, spec.show_when) for spec in self.fields if spec.show_when
        )
        return model

    def validate(self, raw: JsonObject) -> JsonObject:
        """Validate raw params strictly and return them normalised.

        :raises pydantic.ValidationError: when a field is missing, unknown, invalid or hidden.
        """
        return self._model.model_validate(raw).model_dump(mode="json")

    def json_schema(self) -> JsonObject:
        """Describe the params as a JSON schema for the API and MCP."""
        return self._model.model_json_schema()


def _as_object(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _branches(prop: JsonObject) -> list[JsonObject]:
    branches = prop.get("anyOf")
    alternatives = [_as_object(branch) for branch in branches] if isinstance(branches, list) else []
    return [prop, *alternatives]


def _first(prop: JsonObject, key: str) -> JsonValue:
    for branch in _branches(prop):
        value = branch.get(key)
        if value is not None and value != "null":
            return value
    return None


def _enum(prop: JsonObject) -> tuple[str, ...] | None:
    values = _first(prop, "enum")
    return tuple(str(value) for value in values) if isinstance(values, list) else None


def _number(prop: JsonObject, key: str) -> float | None:
    value = _first(prop, key)
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _length(prop: JsonObject, key: str) -> int | None:
    value = _first(prop, key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _value_type(prop: JsonObject) -> ValueType:
    if _first(prop, "format") == "uri":
        return "url"
    match _first(prop, "type"):
        case "integer":
            return "integer"
        case "number":
            return "number"
        case "boolean":
            return "boolean"
    return "string"


def _kind(prop: JsonObject, value_type: ValueType, enum: tuple[str, ...] | None) -> FieldKind:
    if enum is not None:
        return "select"
    if value_type == "boolean":
        return "checkbox"
    if value_type in ("integer", "number"):
        return "number"
    if prop.get("format") == TEXTAREA:
        return "textarea"
    return "text"


def _previews(value: JsonValue) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    return {str(option): str(url) for option, url in value.items()}


def _field_from_schema(name: str, prop: JsonObject, required: bool) -> FieldSpec:
    value_type = _value_type(prop)
    enum = _enum(prop)
    lengths = value_type != "url"
    return FieldSpec(
        name=name,
        label=str(prop.get("title", name)),
        description=str(prop.get("description", "")),
        kind=_kind(prop, value_type, enum),
        required=required,
        value_type=value_type,
        enum=enum,
        minimum=_number(prop, "minimum"),
        maximum=_number(prop, "maximum"),
        min_length=_length(prop, "minLength") if lengths else None,
        max_length=_length(prop, "maxLength") if lengths else None,
        default=prop.get("default"),
        accept=str(prop[UPLOAD]) if UPLOAD in prop else None,
        show_when=_as_object(prop[SHOW_WHEN]) if SHOW_WHEN in prop else None,
        previews=_previews(prop.get(PREVIEWS)),
    )


def form_from_schema(schema: JsonObject) -> Form:
    """Build a form from a JSON schema with its `x-upload`, `x-show-when` and `x-previews`."""
    required = schema.get("required")
    required_names = set(required) if isinstance(required, list) else set()
    properties = _as_object(schema.get("properties"))
    fields = tuple(
        _field_from_schema(name, _as_object(prop), name in required_names)
        for name, prop in properties.items()
    )
    return Form(str(schema.get("title", "Params")), fields)
