import json
import logging
import re
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from html import unescape
from html.parser import HTMLParser

import httpx
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import ValidationError

from workflows.db import JsonObject
from workflows.jobtypes.catalog import HttpExecutor
from workflows.jobtypes.catalog import JobType
from workflows.jobtypes.catalog import ProviderError
from workflows.jobtypes.catalog import Step
from workflows.jobtypes.catalog import form_price
from workflows.jobtypes.forms import FieldKind
from workflows.jobtypes.forms import FieldSpec
from workflows.jobtypes.forms import Form
from workflows.jobtypes.forms import ValueType
from workflows.jobtypes.pricing import PriceError
from workflows.jobtypes.pricing import PriceRule

logger = logging.getLogger(__name__)

TAG = "h4ks-workflows"
API_KEY_HEADER = "X-N8N-API-KEY"
LIST_TIMEOUT_SECONDS = 15.0
PAGE_SIZE = 100
WEBHOOK_NODE = "n8n-nodes-base.webhook"
FORM_NODE = "n8n-nodes-base.formTrigger"
FORM_PAGE_NODE = "n8n-nodes-base.form"
IF_NODE = "n8n-nodes-base.if"
SWITCH_NODE = "n8n-nodes-base.switch"
FIELD_REFERENCE = re.compile(r"""\$json(?:\.(\w+)|\[["'](\w+)["']\])|\.json\.(\w+)""")
HTML_TAG = re.compile(r"<[^>]*>")
NAME_PATTERN = r"^[a-z][a-z0-9-]*$"
WEBHOOK_PREFIX = "workflows-"
DEFAULT_STEP = ("run", 1)
TEXT_LIMIT = 200
TEXTAREA_LIMIT = 2000


class N8nWorkflowError(Exception):
    pass


class N8nNode(BaseModel):
    name: str
    type: str
    parameters: JsonObject = Field(default_factory=dict)
    disabled: bool = False
    notes: str = ""


class N8nWorkflow(BaseModel):
    id: str
    name: str
    nodes: list[N8nNode]
    connections: JsonObject = Field(default_factory=dict)


class N8nPage(BaseModel):
    data: list[N8nWorkflow]
    nextCursor: str | None = None


class FormOption(BaseModel):
    option: str


class FormOptions(BaseModel):
    values: list[FormOption] = Field(default_factory=list)


class FormElement(BaseModel):
    fieldType: str = "text"
    fieldName: str | None = None
    fieldLabel: str | None = None
    placeholder: str | None = None
    defaultValue: str | None = None
    requiredField: bool = False
    multiselect: bool = False
    acceptFileTypes: str | None = None
    html: str | None = None
    fieldOptions: FormOptions = Field(default_factory=FormOptions)


class FormFields(BaseModel):
    values: list[FormElement] = Field(default_factory=list)


class FieldOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    show_when: JsonObject | None = None


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: str
    pricing: str | None = None
    steps: list[tuple[str, int]] = Field(default_factory=lambda: [DEFAULT_STEP], min_length=1)
    position: int = 0
    fields: dict[str, FieldOverride] = Field(default_factory=dict)


def _node(workflow: N8nWorkflow, node_type: str, enabled_only: bool) -> N8nNode:
    for node in workflow.nodes:
        if node.type == node_type and not (enabled_only and node.disabled):
            return node
    raise N8nWorkflowError(f"it has no {node_type.rsplit('.', 1)[-1]} node")


def _live_form(workflow: N8nWorkflow) -> N8nNode:
    form_node = _node(workflow, FORM_NODE, enabled_only=True)
    if form_node.parameters.get("authentication", "none") == "none":
        raise N8nWorkflowError("its form is public; set it to require an n8n login")
    if form_node.name not in workflow.connections:
        raise N8nWorkflowError("its form is not connected to the rest of the workflow")
    return form_node


def _manifest(form_node: N8nNode) -> Manifest:
    if not form_node.notes.strip():
        raise N8nWorkflowError("its Job Form has no settings in its Notes; add at least the price")
    try:
        return Manifest.model_validate_json(form_node.notes)
    except ValidationError as error:
        raise N8nWorkflowError(f"its settings are invalid: {error}") from error


def _kind_and_type(element: FormElement) -> tuple[FieldKind, ValueType]:
    match element.fieldType:
        case "text" | "email" | "date":
            return "text", "string"
        case "textarea":
            return "textarea", "string"
        case "number":
            return "number", "number" if "." in (element.defaultValue or "") else "integer"
        case "dropdown" | "radio":
            return "select", "string"
        case "file":
            return "text", "url"
        case "checkbox" if len(element.fieldOptions.values) == 1:
            return "checkbox", "boolean"
    raise N8nWorkflowError(f"the {element.fieldType} field {element.fieldName} is not supported")


def _default(element: FormElement, value_type: ValueType) -> JsonValue:
    raw = element.defaultValue
    if value_type == "boolean":
        return raw == element.fieldOptions.values[0].option
    if raw is None or raw == "":
        return None
    match value_type:
        case "integer":
            return int(raw)
        case "number":
            return float(raw)
    return raw


def _lengths(
    kind: FieldKind, value_type: ValueType, required: bool
) -> tuple[int | None, int | None]:
    if value_type != "string" or kind == "select":
        return None, None
    return (1 if required else None), (TEXTAREA_LIMIT if kind == "textarea" else TEXT_LIMIT)


class _Pictures(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_alt: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        alt, src = found.get("alt"), found.get("src")
        if tag == "img" and alt and src:
            self.by_alt[alt] = src


@dataclass(frozen=True)
class FieldHtml:
    text: str = ""
    pictures: dict[str, str] = field(default_factory=dict)


def _field_html(html: str) -> FieldHtml:
    pictures = _Pictures()
    pictures.feed(html)
    return FieldHtml(" ".join(unescape(HTML_TAG.sub(" ", html)).split()), pictures.by_alt)


def _html_after_fields(elements: list[FormElement]) -> dict[str, FieldHtml]:
    after: dict[str, FieldHtml] = {}
    previous: str | None = None
    for element in elements:
        if element.fieldType == "html" and previous and element.html:
            after[previous] = _field_html(element.html)
        elif element.fieldType not in ("html", "hiddenField"):
            previous = element.fieldName
    return after


def _description(element: FormElement, help_text: str) -> str:
    options = element.fieldOptions.values
    single_checkbox = element.fieldType == "checkbox" and len(options) == 1
    option_text = options[0].option if single_checkbox else None
    return help_text or element.placeholder or option_text or ""


def _previews(
    element: FormElement, pictures: dict[str, str], options: tuple[str, ...]
) -> dict[str, str] | None:
    if not pictures:
        return None
    unknown = set(pictures) - set(options)
    if unknown:
        raise N8nWorkflowError(
            f"the field {element.fieldName} has pictures for unknown options {sorted(unknown)}"
        )
    if not all(src.startswith("https://") for src in pictures.values()):
        raise N8nWorkflowError(f"the field {element.fieldName} has a picture that is not https")
    return pictures


def _field(element: FormElement, override: FieldOverride, html: FieldHtml) -> FieldSpec:
    if not element.fieldName:
        raise N8nWorkflowError(f"the field {element.fieldLabel} has no field name")
    if element.multiselect:
        raise N8nWorkflowError(f"the field {element.fieldName} is a multiselect")
    kind, value_type = _kind_and_type(element)
    options = tuple(option.option for option in element.fieldOptions.values)
    min_length, max_length = _lengths(kind, value_type, element.requiredField)
    return FieldSpec(
        name=element.fieldName,
        label=element.fieldLabel or element.fieldName,
        description=_description(element, html.text),
        kind=kind,
        required=element.requiredField,
        value_type=value_type,
        enum=options if kind == "select" else None,
        minimum=override.minimum,
        maximum=override.maximum,
        min_length=override.min_length if override.min_length is not None else min_length,
        max_length=override.max_length if override.max_length is not None else max_length,
        default=_default(element, value_type),
        accept=(element.acceptFileTypes or "*/*") if value_type == "url" else None,
        show_when=override.show_when,
        previews=_previews(element, html.pictures, options),
    )


@dataclass(frozen=True)
class FormPage:
    node: N8nNode
    conditions: JsonObject


def _elements(page: N8nNode) -> list[FormElement]:
    try:
        return FormFields.model_validate(page.parameters.get("formFields") or {}).values
    except ValidationError as error:
        raise N8nWorkflowError(f"the form page {page.name} cannot be read: {error}") from error


def _page_fields(page: FormPage, manifest: Manifest) -> list[FieldSpec]:
    elements = _elements(page.node)
    html_after = _html_after_fields(elements)
    fields = []
    for element in elements:
        if element.fieldType in ("html", "hiddenField"):
            continue
        override = manifest.fields.get(element.fieldName or "", FieldOverride())
        spec = _field(element, override, html_after.get(element.fieldName or "", FieldHtml()))
        conditions = _all_of(page.conditions, override.show_when or {})
        fields.append(replace(spec, show_when=conditions or None))
    return fields


def _form(name: str, pages: list[FormPage], manifest: Manifest) -> Form:
    fields = [spec for page in pages for spec in _page_fields(page, manifest)]
    names = [spec.name for spec in fields]
    repeated = sorted({field_name for field_name in names if names.count(field_name) > 1})
    if repeated:
        raise N8nWorkflowError(f"its form pages repeat the fields {repeated}")
    unknown = set(manifest.fields) - set(names)
    if unknown:
        raise N8nWorkflowError(f"its manifest names fields the form lacks: {sorted(unknown)}")
    untested = {key for spec in fields for key in spec.show_when or {}} - set(names)
    if untested:
        raise N8nWorkflowError(f"its conditions test fields the form lacks: {sorted(untested)}")
    return Form(f"{name.replace('-', ' ').title().replace(' ', '')}Params", tuple(fields))


def _all_of(first: JsonObject, second: JsonObject) -> JsonObject:
    for key, condition in second.items():
        if key in first and first[key] != condition:
            raise N8nWorkflowError(f"a form page needs two different things of {key}")
    return {**first, **second}


def _either(first: JsonObject, second: JsonObject) -> JsonObject:
    if first == second:
        return first
    values = [_equal_values(first), _equal_values(second)]
    if values[0] is None or values[1] is None or values[0][0] != values[1][0]:
        raise N8nWorkflowError("a form page is reached in two ways that test different fields")
    return {values[0][0]: {"one_of": [*values[0][1], *values[1][1]]}}


def _equal_values(conditions: JsonObject) -> tuple[str, list[JsonValue]] | None:
    if len(conditions) != 1:
        return None
    key, condition = next(iter(conditions.items()))
    if not isinstance(condition, dict):
        return key, [condition]
    one_of = condition.get("one_of")
    return (key, list(one_of)) if isinstance(one_of, list) else None


def _negate(conditions: JsonObject) -> JsonObject:
    if len(conditions) != 1:
        raise N8nWorkflowError("the false branch of a condition on several fields cannot be read")
    key, condition = next(iter(conditions.items()))
    if not isinstance(condition, dict):
        return {key: {"not": condition}}
    match next(iter(condition.items())):
        case ("not", value):
            return {key: value}
        case ("one_of", values):
            return {key: {"not_one_of": values}}
        case ("not_one_of", values):
            return {key: {"one_of": values}}
        case ("empty", empty):
            return {key: {"empty": not empty}}
    raise N8nWorkflowError(f"the condition on {key} cannot be negated")


def _tested_field(left: JsonValue) -> str:
    found = FIELD_REFERENCE.search(str(left))
    if found is None:
        raise N8nWorkflowError(f"a form condition tests {left}, which is not a form field")
    return next(group for group in found.groups() if group)


def _condition(item: JsonObject) -> tuple[str, JsonValue]:
    operator = item.get("operator")
    operation = operator.get("operation") if isinstance(operator, dict) else None
    right = item.get("rightValue")
    match operation:
        case "equals" | "true" | "false":
            value: JsonValue = right if operation == "equals" else operation == "true"
        case "notEquals":
            value = {"not": right}
        case "empty" | "notEmpty":
            value = {"empty": operation == "empty"}
        case _:
            raise N8nWorkflowError(f"the form condition {operation} is not supported")
    return _tested_field(item.get("leftValue")), value


def _conditions(block: JsonValue) -> JsonObject:
    items = block.get("conditions") if isinstance(block, dict) else None
    if not isinstance(items, list) or not items:
        raise N8nWorkflowError("a form condition has no rules")
    pairs = [_condition(item) for item in items if isinstance(item, dict)]
    if isinstance(block, dict) and block.get("combinator") == "or":
        found: JsonObject = {}
        for key, value in pairs:
            found = _either(found, {key: value}) if found else {key: value}
        return found
    merged: JsonObject = {}
    for key, value in pairs:
        merged = _all_of(merged, {key: value})
    return merged


def _branches(node: N8nNode) -> list[JsonObject]:
    """Return the show conditions each output of an If or Switch node adds."""
    if node.type == IF_NODE:
        condition = _conditions(node.parameters.get("conditions"))
        return [condition, _negate(condition)]
    rules = node.parameters.get("rules")
    values = rules.get("values") if isinstance(rules, dict) else None
    branches = [
        _conditions(rule.get("conditions")) for rule in _json_list(values) if isinstance(rule, dict)
    ]
    options = node.parameters.get("options")
    if isinstance(options, dict) and options.get("fallbackOutput") == "extra":
        branches.append(_fallback(branches))
    return branches


def _fallback(branches: list[JsonObject]) -> JsonObject:
    found: JsonObject = {}
    for branch in branches:
        found = _either(found, branch) if found else branch
    return _negate(found)


def _outputs(workflow: N8nWorkflow, name: str) -> list[list[str]]:
    main = workflow.connections.get(name)
    outputs = main.get("main") if isinstance(main, dict) else None
    return [
        [str(link["node"]) for link in _json_list(output) if isinstance(link, dict)]
        for output in _json_list(outputs)
    ]


def _json_list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _pages(workflow: N8nWorkflow, trigger: N8nNode) -> list[FormPage]:
    """Walk from the trigger through If, Switch and Form page nodes to every form page."""
    nodes = {node.name: node for node in workflow.nodes if not node.disabled}
    found: dict[str, JsonObject] = {}
    order = [trigger.name]
    stack: list[tuple[str, JsonObject]] = [(trigger.name, {})]
    seen: set[str] = set()
    while stack:
        name, conditions = stack.pop()
        key = json.dumps([name, conditions], sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        node = nodes[name]
        branches = _branches(node) if node.type in (IF_NODE, SWITCH_NODE) else []
        for index, targets in reversed(list(enumerate(_outputs(workflow, name)))):
            added = _all_of(conditions, branches[index]) if index < len(branches) else conditions
            for target in reversed(targets):
                stack.extend(_visit(nodes.get(target), added, found, order))
    return [FormPage(nodes[name], found.get(name, {})) for name in order]


def _visit(
    node: N8nNode | None, conditions: JsonObject, found: dict[str, JsonObject], order: list[str]
) -> list[tuple[str, JsonObject]]:
    if node is None or node.type not in (IF_NODE, SWITCH_NODE, FORM_PAGE_NODE):
        return []
    if node.type == FORM_PAGE_NODE:
        if node.parameters.get("operation") == "completion":
            return []
        if node.name in found:
            found[node.name] = _either(found[node.name], conditions)
            return []
        found[node.name] = conditions
        order.append(node.name)
    return [(node.name, conditions)]


def _name_from_path(path: str) -> str:
    name = path.removeprefix(WEBHOOK_PREFIX)
    if not re.fullmatch(NAME_PATTERN, name):
        raise N8nWorkflowError(
            f"its webhook path {path} makes no valid name; after workflows- use a lowercase "
            "letter, then letters, digits or dashes"
        )
    return name


def _pricing_text(price: PriceRule) -> str:
    return (
        f"{price.expression} credits"
        if not price.fields and not price.media_field
        else price.expression
    )


class N8nProvider:
    """Serves every active n8n workflow tagged `h4ks-workflows` as a job type."""

    def __init__(
        self, http: httpx.AsyncClient, base_url: str, api_key: str, executor_token: str
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._executor_token = executor_token
        self.errors: dict[str, str] = {}

    async def discover(self) -> list[JobType]:
        workflows = await self._list()
        job_types: list[tuple[int, JobType]] = []
        errors: dict[str, str] = {}
        for workflow in workflows:
            try:
                job_types.append(self._job_type(workflow))
            except (N8nWorkflowError, PriceError) as error:
                errors[workflow.name] = str(error)
                logger.warning("skipping n8n workflow %s: %s", workflow.name, error)
        self.errors = errors
        return [
            job_type for _, job_type in sorted(job_types, key=lambda pair: (pair[0], pair[1].name))
        ]

    async def _list(self) -> list[N8nWorkflow]:
        workflows: list[N8nWorkflow] = []
        cursor: str | None = None
        while True:
            page = await self._page(cursor)
            workflows.extend(page.data)
            cursor = page.nextCursor
            if not cursor:
                return workflows

    async def _page(self, cursor: str | None) -> N8nPage:
        query: dict[str, str | int] = {"tags": TAG, "active": "true", "limit": PAGE_SIZE}
        if cursor:
            query["cursor"] = cursor
        try:
            response = await self._http.get(
                f"{self._base_url}/api/v1/workflows",
                params=query,
                headers={API_KEY_HEADER: self._api_key},
                timeout=LIST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return N8nPage.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError) as error:
            raise ProviderError(f"could not list n8n workflows: {error}") from error

    def _job_type(self, workflow: N8nWorkflow) -> tuple[int, JobType]:
        form_node = _live_form(workflow)
        manifest = _manifest(form_node)
        webhook = _node(workflow, WEBHOOK_NODE, enabled_only=True)
        path = webhook.parameters.get("path")
        if not isinstance(path, str) or not path:
            raise N8nWorkflowError("its webhook has no path")
        name = _name_from_path(path)
        title = form_node.parameters.get("formTitle")
        description = form_node.parameters.get("formDescription")
        form = _form(name, _pages(workflow, form_node), manifest)
        price = form_price(form, manifest.price)
        job_type = JobType(
            name=name,
            title=title if isinstance(title, str) and title else name,
            description=description if isinstance(description, str) else "",
            pricing=manifest.pricing or _pricing_text(price),
            form=form,
            price=price,
            steps=tuple(Step(name, weight) for name, weight in manifest.steps),
            executor=HttpExecutor(
                self._http, f"{self._base_url}/webhook/{path}", self._executor_token
            ),
        )
        return manifest.position, job_type
