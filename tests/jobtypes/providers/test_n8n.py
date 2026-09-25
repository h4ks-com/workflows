import copy
import json
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import JsonValue

from workflows.db import JsonObject
from workflows.jobtypes.catalog import Catalog
from workflows.jobtypes.catalog import HttpExecutor
from workflows.jobtypes.catalog import ProviderError
from workflows.jobtypes.providers.builtin import builtin_job_types
from workflows.jobtypes.providers.n8n import N8nProvider

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
    second = copy.deepcopy(with_manifest({"name": "image-two", "position": 9}))
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
        ({"fields": {"nope": {"description": "x"}}}, "nope"),
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
