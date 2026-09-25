import httpx
import pytest
import respx
from pydantic import JsonValue

from workflows.jobtypes.catalog import HttpExecutor
from workflows.jobtypes.catalog import ProviderError
from workflows.jobtypes.providers.services import ServiceProvider

SERVICE = "http://midifier.test:8000"
MANIFEST_URL = f"{SERVICE}/v1/workflow"


def manifest(**change: JsonValue) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {
        "name": "midi",
        "title": "Song to MIDI",
        "description": "Turns a song into MIDI.",
        "pricing": "1.5 credits per second, plus 60",
        "price": "1.5 * duration(url) + 60",
        "steps": [["fetch", 5], ["transcribe", 90], ["store", 5]],
        "params_schema": {
            "title": "MidiParams",
            "type": "object",
            "properties": {
                "url": {
                    "title": "Song",
                    "type": "string",
                    "format": "uri",
                    "description": "A link.",
                    "x-upload": "audio/*",
                }
            },
            "required": ["url"],
        },
    }
    return base | change


def provider() -> ServiceProvider:
    return ServiceProvider(httpx.AsyncClient(), f"{SERVICE}/", "executor-key")


@respx.mock
async def test_discover_reads_the_manifest_into_a_job_type() -> None:
    route = respx.get(MANIFEST_URL).respond(json=manifest())

    [midi] = await provider().discover()

    assert (midi.name, midi.title, midi.step_names()) == (
        "midi",
        "Song to MIDI",
        ["fetch", "transcribe", "store"],
    )
    [url] = midi.form.fields
    assert (url.name, url.value_type, url.accept, url.required) == ("url", "url", "audio/*", True)
    assert midi.price.media_field == "url"
    assert isinstance(midi.executor, HttpExecutor)
    assert midi.executor.url == f"{SERVICE}/v1/workflow/jobs"
    assert route.calls.last.request.headers["x-api-key"] == "executor-key"


@respx.mock
@pytest.mark.parametrize(
    ("change", "error"),
    [({"price": "open('x')"}, "only call allowed"), ({"price": "size * 2"}, "size")],
)
async def test_a_bad_price_is_skipped_and_reported(
    change: dict[str, JsonValue], error: str
) -> None:
    respx.get(MANIFEST_URL).respond(json=manifest(**change))
    service = provider()

    assert await service.discover() == []
    assert error in service.errors[SERVICE]


@respx.mock
@pytest.mark.parametrize(
    "response",
    [httpx.Response(401), httpx.Response(200, json={"name": "midi"}), httpx.ConnectError("down")],
)
async def test_an_unreachable_service_raises_provider_error(
    response: httpx.Response | Exception,
) -> None:
    respx.get(MANIFEST_URL).mock(side_effect=[response])

    with pytest.raises(ProviderError):
        await provider().discover()
