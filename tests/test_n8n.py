import copy
import json
from pathlib import Path

import httpx
import pytest
import respx

from workflows.builtin import builtin_job_types
from workflows.catalog import HttpExecutor, ProviderError
from workflows.db import JsonObject
from workflows.n8n import N8nProvider

N8N = "https://n8n.test"
LIST_URL = f"{N8N}/api/v1/workflows"
IMAGE_WORKFLOW: JsonObject = json.loads(
    (Path(__file__).parent / "data" / "n8n_image_workflow.json").read_text()
)


def provider() -> N8nProvider:
    return N8nProvider(httpx.AsyncClient(), N8N, "api-key", "executor-key")


def node(workflow: JsonObject, node_type: str) -> JsonObject:
    nodes = workflow["nodes"]
    assert isinstance(nodes, list)
    found = next(item for item in nodes if isinstance(item, dict) and item["type"] == node_type)
    assert isinstance(found, dict)
    return found


def with_manifest(change: dict[str, object]) -> JsonObject:
    workflow = copy.deepcopy(IMAGE_WORKFLOW)
    sticky = node(workflow, "n8n-nodes-base.stickyNote")
    parameters = sticky["parameters"]
    assert isinstance(parameters, dict)
    content = str(parameters["content"])
    start = content.index("{")
    end = content.rindex("}") + 1
    manifest = json.loads(content[start:end]) | change
    parameters["content"] = content[:start] + json.dumps(manifest) + content[end:]
    return workflow


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
        ({"steps": []}, "manifest is invalid"),
        ({"surprise": True}, "manifest is invalid"),
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
async def test_discover_skips_a_workflow_without_a_webhook_or_manifest() -> None:
    no_webhook = copy.deepcopy(IMAGE_WORKFLOW)
    node(no_webhook, "n8n-nodes-base.webhook")["disabled"] = True
    no_manifest = copy.deepcopy(IMAGE_WORKFLOW)
    node(no_manifest, "n8n-nodes-base.stickyNote")["parameters"] = {"content": "just notes"}
    respx.get(LIST_URL).respond(json={"data": [no_webhook, no_manifest], "nextCursor": None})

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
