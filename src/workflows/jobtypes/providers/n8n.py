import logging
import re
from html import unescape
from typing import Literal

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

    description: str | None = None
    type: Literal["integer", "number"] | None = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    show_when: JsonObject | None = None


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: str
    name: str | None = Field(None, pattern=NAME_PATTERN)
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


def _kind_and_type(element: FormElement, override: FieldOverride) -> tuple[FieldKind, ValueType]:
    match element.fieldType:
        case "text" | "email" | "date":
            return "text", "string"
        case "textarea":
            return "textarea", "string"
        case "number":
            return "number", override.type or "integer"
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


def _help_texts(elements: list[FormElement]) -> dict[str, str]:
    help_texts: dict[str, str] = {}
    previous: str | None = None
    for element in elements:
        if element.fieldType == "html" and previous and element.html:
            help_texts[previous] = " ".join(unescape(HTML_TAG.sub(" ", element.html)).split())
        elif element.fieldType not in ("html", "hiddenField"):
            previous = element.fieldName
    return help_texts


def _description(element: FormElement, override: FieldOverride, help_text: str | None) -> str:
    options = element.fieldOptions.values
    single_checkbox = element.fieldType == "checkbox" and len(options) == 1
    option_text = options[0].option if single_checkbox else None
    return override.description or help_text or element.placeholder or option_text or ""


def _field(element: FormElement, override: FieldOverride, help_text: str | None) -> FieldSpec:
    if not element.fieldName:
        raise N8nWorkflowError(f"the field {element.fieldLabel} has no field name")
    if element.multiselect:
        raise N8nWorkflowError(f"the field {element.fieldName} is a multiselect")
    kind, value_type = _kind_and_type(element, override)
    options = tuple(option.option for option in element.fieldOptions.values)
    min_length, max_length = _lengths(kind, value_type, element.requiredField)
    return FieldSpec(
        name=element.fieldName,
        label=element.fieldLabel or element.fieldName,
        description=_description(element, override, help_text),
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
    )


def _form(name: str, form_node: N8nNode, manifest: Manifest) -> Form:
    try:
        parsed = FormFields.model_validate(form_node.parameters.get("formFields") or {})
    except ValidationError as error:
        raise N8nWorkflowError(f"its form cannot be read: {error}") from error
    shown = [
        element for element in parsed.values if element.fieldType not in ("html", "hiddenField")
    ]
    help_texts = _help_texts(parsed.values)
    fields = tuple(
        _field(
            element,
            manifest.fields.get(element.fieldName or "", FieldOverride()),
            help_texts.get(element.fieldName or ""),
        )
        for element in shown
    )
    unknown = set(manifest.fields) - {spec.name for spec in fields}
    if unknown:
        raise N8nWorkflowError(f"its manifest names fields the form lacks: {sorted(unknown)}")
    return Form(f"{name.replace('-', ' ').title().replace(' ', '')}Params", fields)


def _name_from_path(path: str) -> str:
    name = path.removeprefix(WEBHOOK_PREFIX)
    if not re.fullmatch(NAME_PATTERN, name):
        raise N8nWorkflowError(
            f"its webhook path {path} makes no valid name; set one in the Job Form's Notes"
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
        name = manifest.name or _name_from_path(path)
        title = form_node.parameters.get("formTitle")
        description = form_node.parameters.get("formDescription")
        form = _form(name, form_node, manifest)
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
