import copy
import json
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import JsonValue
from pydantic import ValidationError

from workflows.db import JsonObject
from workflows.jobtypes.catalog import Catalog
from workflows.jobtypes.catalog import HttpExecutor
from workflows.jobtypes.catalog import JobType
from workflows.jobtypes.catalog import ProviderError
from workflows.jobtypes.forms import form_from_schema
from workflows.jobtypes.providers.builtin import builtin_job_types
from workflows.jobtypes.providers.n8n import N8nNode
from workflows.jobtypes.providers.n8n import N8nProvider
from workflows.jobtypes.providers.n8n import N8nWorkflowError
from workflows.jobtypes.providers.n8n import _branches

N8N = "https://n8n.test"
LIST_URL = f"{N8N}/api/v1/workflows"
IMAGE_WORKFLOW: JsonObject = json.loads(
    (Path(__file__).parent / "data" / "n8n_image_workflow.json").read_text()
)


TEMPLATE_PATH = Path(__file__).parents[3] / "n8n" / "executor-template.json"


def provider() -> N8nProvider:
    return N8nProvider(httpx.AsyncClient(), N8N, "api-key", "executor-key")


@respx.mock
async def test_the_documented_template_is_a_valid_executor() -> None:
    template: JsonObject = {"id": "template", **json.loads(TEMPLATE_PATH.read_text())}
    respx.get(LIST_URL).respond(json={"data": [template], "nextCursor": None})
    n8n = provider()

    [greeting] = await n8n.discover()

    assert n8n.errors == {}
    assert (greeting.name, greeting.step_names()) == ("template", ["greet"])
    assert [spec.name for spec in greeting.form.fields] == ["name"]


def node(workflow: JsonObject, node_type: str) -> JsonObject:
    nodes = workflow["nodes"]
    assert isinstance(nodes, list)
    found = next(item for item in nodes if isinstance(item, dict) and item["type"] == node_type)
    assert isinstance(found, dict)
    return found


def with_manifest(change: dict[str, object], replace: bool = False) -> JsonObject:
    workflow = copy.deepcopy(IMAGE_WORKFLOW)
    form = node(workflow, "n8n-nodes-base.formTrigger")
    manifest = change if replace else json.loads(str(form["notes"])) | change
    form["notes"] = json.dumps(manifest)
    return workflow


def with_form_fields(fields: list[JsonValue]) -> JsonObject:
    workflow = copy.deepcopy(IMAGE_WORKFLOW)
    form = node(workflow, "n8n-nodes-base.formTrigger")
    form["parameters"] = {
        "authentication": "n8nUserAuth",
        "formTitle": "Echo",
        "formFields": {"values": fields},
    }
    form["notes"] = json.dumps({"price": "1"})
    return workflow


@respx.mock
async def test_html_after_a_field_is_its_help_text() -> None:
    fields: list[JsonValue] = [
        {
            "fieldType": "dropdown",
            "fieldName": "mood",
            "fieldLabel": "Mood",
            "fieldOptions": {"values": [{"option": "calm"}]},
        },
        {"fieldType": "html", "html": "<p>How it <b>sounds</b> &amp; feels.</p>"},
        {
            "fieldType": "checkbox",
            "fieldName": "loud",
            "fieldLabel": "Loud",
            "fieldOptions": {"values": [{"option": "Make it loud."}]},
        },
        {
            "fieldType": "text",
            "fieldName": "note",
            "fieldLabel": "Note",
            "placeholder": "Anything else.",
        },
    ]
    respx.get(LIST_URL).respond(json={"data": [with_form_fields(fields)], "nextCursor": None})

    [echo] = await provider().discover()

    descriptions = {spec.name: spec.description for spec in echo.form.fields}
    assert descriptions == {
        "mood": "How it sounds & feels.",
        "loud": "Make it loud.",
        "note": "Anything else.",
    }


@respx.mock
async def test_a_form_without_notes_is_skipped() -> None:
    workflow = copy.deepcopy(IMAGE_WORKFLOW)
    node(workflow, "n8n-nodes-base.formTrigger")["notes"] = ""
    respx.get(LIST_URL).respond(json={"data": [workflow], "nextCursor": None})
    n8n = provider()

    assert await n8n.discover() == []
    assert "no settings in its Notes" in n8n.errors[str(IMAGE_WORKFLOW["name"])]


@respx.mock
async def test_a_note_with_only_a_price_uses_defaults() -> None:
    minimal = with_manifest({"price": "80 if size == 'large' else 40"}, replace=True)
    respx.get(LIST_URL).respond(json={"data": [minimal], "nextCursor": None})

    [image] = await provider().discover()

    assert image.name == "image"
    assert image.pricing == "80 if size == 'large' else 40"
    assert image.step_names() == ["run"]
    assert image.form.fields[0].name == "prompt"


def mood_form(html: str) -> JsonObject:
    mood: JsonValue = {
        "fieldType": "dropdown",
        "fieldName": "mood",
        "fieldLabel": "Mood",
        "fieldOptions": {"values": [{"option": "calm"}, {"option": "loud"}]},
    }
    return with_form_fields([mood, {"fieldType": "html", "html": html}])


@respx.mock
async def test_pictures_in_a_fields_html_preview_its_options() -> None:
    calm = "https://media.example/calm.webp"
    html = f'<p>How it sounds.</p><img src="{calm}" alt="calm">'
    respx.get(LIST_URL).respond(json={"data": [mood_form(html)], "nextCursor": None})

    [echo] = await provider().discover()

    [mood] = echo.form.fields
    assert mood.description == "How it sounds."
    assert mood.previews == {"calm": calm}
    assert form_from_schema(echo.form.json_schema()).fields[0].previews == {"calm": calm}


@respx.mock
@pytest.mark.parametrize(
    ("html", "error"),
    [
        ('<img src="https://media.example/q.webp" alt="quiet">', "unknown options ['quiet']"),
        ('<img src="http://media.example/c.webp" alt="calm">', "not https"),
    ],
)
async def test_bad_option_pictures_skip_the_workflow(html: str, error: str) -> None:
    respx.get(LIST_URL).respond(json={"data": [mood_form(html)], "nextCursor": None})
    n8n = provider()

    assert await n8n.discover() == []
    assert error in n8n.errors[str(IMAGE_WORKFLOW["name"])]


@respx.mock
async def test_a_constant_price_reads_as_credits() -> None:
    minimal = with_manifest({"price": "40"}, replace=True)
    respx.get(LIST_URL).respond(json={"data": [minimal], "nextCursor": None})

    [image] = await provider().discover()

    assert image.pricing == "40 credits"


def with_form(change: JsonObject, connected: bool = True) -> JsonObject:
    workflow = copy.deepcopy(IMAGE_WORKFLOW)
    form = node(workflow, "n8n-nodes-base.formTrigger")
    parameters = form["parameters"]
    assert isinstance(parameters, dict)
    for key, value in change.items():
        if key == "disabled":
            form["disabled"] = value
        else:
            parameters[key] = value
    connections = workflow["connections"]
    assert isinstance(connections, dict)
    if not connected:
        connections.pop("Job Form")
    return workflow


@respx.mock
@pytest.mark.parametrize(
    ("workflow", "error"),
    [
        (with_form({"authentication": "none"}), "form is public"),
        (with_form({}, connected=False), "form is not connected"),
        (with_form({"disabled": True}), "no formTrigger node"),
    ],
)
async def test_the_form_must_be_a_private_connected_trigger(
    workflow: JsonObject, error: str
) -> None:
    respx.get(LIST_URL).respond(json={"data": [workflow], "nextCursor": None})
    n8n = provider()

    assert await n8n.discover() == []
    assert error in n8n.errors[str(IMAGE_WORKFLOW["name"])]


@respx.mock
async def test_skipped_workflows_reach_the_catalog() -> None:
    respx.get(LIST_URL).respond(
        json={"data": [with_manifest({}, replace=True)], "nextCursor": None}
    )
    catalog = Catalog([provider()])

    await catalog.refresh()

    assert "price" in catalog.skipped()[str(IMAGE_WORKFLOW["name"])]


@respx.mock
async def test_discover_reads_the_tagged_workflows_into_job_types() -> None:
    route = respx.get(LIST_URL).respond(json={"data": [IMAGE_WORKFLOW], "nextCursor": None})

    [image] = await provider().discover()

    builtin = next(
        job_type
        for job_type in builtin_job_types(httpx.AsyncClient(), N8N, "k")
        if job_type.name == "image"
    )
    assert image.form.json_schema() == builtin.form.json_schema()
    assert (image.title, image.pricing, image.steps) == (
        builtin.title,
        builtin.pricing,
        builtin.steps,
    )
    assert isinstance(image.executor, HttpExecutor)
    assert image.executor.url == f"{N8N}/webhook/workflows-image"
    request = route.calls.last.request
    assert request.headers["x-n8n-api-key"] == "api-key"
    assert request.url.params["tags"] == "h4ks-workflows"
    assert request.url.params["active"] == "true"


@respx.mock
async def test_discover_follows_the_cursor() -> None:
    second = copy.deepcopy(with_manifest({"position": 9}))
    webhook = node(second, "n8n-nodes-base.webhook")["parameters"]
    assert isinstance(webhook, dict)
    webhook["path"] = "workflows-image-two"
    respx.get(LIST_URL).mock(
        side_effect=[
            httpx.Response(200, json={"data": [IMAGE_WORKFLOW], "nextCursor": "next"}),
            httpx.Response(200, json={"data": [second], "nextCursor": None}),
        ]
    )

    names = [job_type.name for job_type in await provider().discover()]

    assert names == ["image", "image-two"]


@respx.mock
@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"price": "80 if colour == 'red' else 40"}, "colour"),
        ({"price": "open('x')"}, "only call allowed"),
        ({"price": "[40]"}, "not allowed"),
        ({"fields": {"nope": {"max_length": 9}}}, "nope"),
        ({"name": "image-two"}, "settings are invalid"),
        ({"steps": []}, "settings are invalid"),
        ({"surprise": True}, "settings are invalid"),
    ],
)
async def test_discover_skips_broken_workflows_and_reports_why(
    change: dict[str, object], error: str
) -> None:
    respx.get(LIST_URL).respond(json={"data": [with_manifest(change)], "nextCursor": None})
    n8n = provider()

    assert await n8n.discover() == []
    assert error in n8n.errors[str(IMAGE_WORKFLOW["name"])]


@respx.mock
async def test_discover_skips_a_workflow_without_a_webhook() -> None:
    no_webhook = copy.deepcopy(IMAGE_WORKFLOW)
    node(no_webhook, "n8n-nodes-base.webhook")["disabled"] = True
    respx.get(LIST_URL).respond(json={"data": [no_webhook], "nextCursor": None})

    assert await provider().discover() == []


@respx.mock
async def test_discover_rejects_unsupported_form_fields() -> None:
    workflow = copy.deepcopy(IMAGE_WORKFLOW)
    form = node(workflow, "n8n-nodes-base.formTrigger")
    parameters = form["parameters"]
    assert isinstance(parameters, dict)
    fields = parameters["formFields"]
    assert isinstance(fields, dict) and isinstance(fields["values"], list)
    fields["values"].append(
        {"fieldType": "password", "fieldName": "secret", "fieldLabel": "Secret"}
    )
    respx.get(LIST_URL).respond(json={"data": [workflow], "nextCursor": None})
    n8n = provider()

    assert await n8n.discover() == []
    assert "password" in n8n.errors[str(IMAGE_WORKFLOW["name"])]


@respx.mock
@pytest.mark.parametrize(
    "response",
    [httpx.Response(401), httpx.Response(200, json={"nope": 1}), httpx.ConnectError("down")],
)
async def test_discover_raises_provider_error_when_n8n_fails(
    response: httpx.Response | Exception,
) -> None:
    respx.get(LIST_URL).mock(side_effect=[response])

    with pytest.raises(ProviderError):
        await provider().discover()


KIND: JsonValue = {
    "fieldType": "dropdown",
    "fieldName": "kind",
    "fieldLabel": "Kind",
    "fieldOptions": {"values": [{"option": "photo"}, {"option": "drawing"}, {"option": "text"}]},
}


def field(name: str, required: bool = False, default: str | None = None) -> JsonValue:
    return {
        "fieldType": "text",
        "fieldName": name,
        "fieldLabel": name.title(),
        "requiredField": required,
        "defaultValue": default,
    }


def page(name: str, fields: list[JsonValue]) -> JsonObject:
    return {
        "name": name,
        "type": "n8n-nodes-base.form",
        "parameters": {"formFields": {"values": fields}},
    }


def rule(operation: str, right: JsonValue = None, left: str = "={{ $json.kind }}") -> JsonObject:
    return {
        "leftValue": left,
        "rightValue": right,
        "operator": {"type": "string", "operation": operation},
    }


def if_node(*rules: JsonObject, combinator: str = "and") -> JsonObject:
    return {
        "name": "Kind?",
        "type": "n8n-nodes-base.if",
        "parameters": {"conditions": {"conditions": list(rules), "combinator": combinator}},
    }


def paged(extra: list[JsonObject], connections: dict[str, list[list[str]]]) -> JsonObject:
    workflow = with_form_fields([KIND])
    trigger = node(workflow, "n8n-nodes-base.formTrigger")
    nodes = workflow["nodes"]
    assert isinstance(nodes, list)
    nodes.extend(extra)
    links = workflow["connections"]
    assert isinstance(links, dict)
    for source, outputs in connections.items():
        name = str(trigger["name"]) if source == "trigger" else source
        links[name] = {
            "main": [
                [{"node": target, "type": "main", "index": 0} for target in output]
                for output in outputs
            ]
        }
    return workflow


async def discover_one(workflow: JsonObject) -> tuple[N8nProvider, list[JobType]]:
    respx.get(LIST_URL).respond(json={"data": [workflow], "nextCursor": None})
    n8n = provider()
    return n8n, await n8n.discover()


def shows(job_type: JobType) -> dict[str, JsonValue]:
    return {spec.name: spec.show_when for spec in job_type.form.fields}


@respx.mock
async def test_if_branches_show_their_pages_only_for_their_choice() -> None:
    workflow = paged(
        [
            if_node(rule("equals", "photo")),
            page("Photo", [field("camera")]),
            page("Other", [field("style")]),
        ],
        {"trigger": [["Kind?"]], "Kind?": [["Photo"], ["Other"]]},
    )

    _, [job_type] = await discover_one(workflow)

    assert shows(job_type) == {
        "kind": None,
        "camera": {"kind": "photo"},
        "style": {"kind": {"not": "photo"}},
    }


@respx.mock
async def test_switch_outputs_and_fallback_become_conditions() -> None:
    switch: JsonObject = {
        "name": "Kind?",
        "type": "n8n-nodes-base.switch",
        "parameters": {
            "rules": {
                "values": [
                    {"conditions": {"conditions": [rule("equals", "photo")]}},
                    {"conditions": {"conditions": [rule("equals", "drawing")]}},
                ]
            },
            "options": {"fallbackOutput": "extra"},
        },
    }
    workflow = paged(
        [
            switch,
            page("Photo", [field("camera")]),
            page("Drawing", [field("pen")]),
            page("Rest", [field("font")]),
        ],
        {"trigger": [["Kind?"]], "Kind?": [["Photo"], ["Drawing"], ["Rest"]]},
    )

    _, [job_type] = await discover_one(workflow)

    assert shows(job_type) == {
        "kind": None,
        "camera": {"kind": "photo"},
        "pen": {"kind": "drawing"},
        "font": {"kind": {"not_one_of": ["photo", "drawing"]}},
    }


@respx.mock
async def test_a_page_reached_two_ways_shows_for_either() -> None:
    workflow = paged(
        [
            if_node(rule("equals", "photo"), rule("equals", "drawing"), combinator="or"),
            page("Picture", [field("size")]),
        ],
        {"trigger": [["Kind?"]], "Kind?": [["Picture"], []]},
    )

    _, [job_type] = await discover_one(workflow)

    assert shows(job_type)["size"] == {"kind": {"one_of": ["photo", "drawing"]}}


@respx.mock
async def test_page_fields_are_required_only_while_shown() -> None:
    workflow = paged(
        [
            if_node(rule("equals", "photo")),
            page("Photo", [field("camera", required=True), field("lens", default="50mm")]),
        ],
        {"trigger": [["Kind?"]], "Kind?": [["Photo"], []]},
    )
    _, [job_type] = await discover_one(workflow)

    hidden = job_type.form.validate({"kind": "text", "lens": "50mm"})

    assert (hidden["camera"], hidden["lens"]) == (None, None)
    with pytest.raises(ValidationError, match="fill in camera"):
        job_type.form.validate({"kind": "photo"})
    with pytest.raises(ValidationError, match="camera needs kind photo"):
        job_type.form.validate({"kind": "text", "camera": "leica"})
    assert job_type.form.validate({"kind": "photo", "camera": "leica"})["lens"] == "50mm"


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"Kind?": if_node(rule("contains", "pho"))}, "contains is not supported"),
        ({"Kind?": if_node(rule("equals", "photo", left="={{ $now }}"))}, "not a form field"),
        ({"Kind?": if_node(rule("equals", "photo", left="={{ $json.mood }}"))}, "lacks: ['mood']"),
        ({"Photo": page("Photo", [KIND])}, "repeat the fields ['kind']"),
    ],
)
@respx.mock
async def test_unreadable_pages_skip_the_workflow(
    change: dict[str, JsonObject], error: str
) -> None:
    parts = {
        "Kind?": if_node(rule("equals", "photo")),
        "Photo": page("Photo", [field("camera")]),
    } | change
    workflow = paged(list(parts.values()), {"trigger": [["Kind?"]], "Kind?": [["Photo"], []]})

    n8n, found = await discover_one(workflow)

    assert found == []
    assert error in n8n.errors[str(IMAGE_WORKFLOW["name"])]


def test_the_false_branch_of_an_and_on_two_fields_cannot_be_read() -> None:
    both = N8nNode.model_validate(
        if_node(rule("equals", "photo"), rule("notEmpty", left="={{ $json.note }}"))
    )

    with pytest.raises(N8nWorkflowError, match="several fields"):
        _branches(both)
