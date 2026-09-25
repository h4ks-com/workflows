import asyncio

import httpx
import pytest
import respx

from conftest import SONG_URL
from workflows.jobtypes.probe import Probe
from workflows.jobtypes.probe import ProbeError
from workflows.jobtypes.probe import YtdlProber

YTDL = "http://ytdl.test"


@respx.mock
async def test_ytdl_prober_reads_duration_and_title() -> None:
    route = respx.post(f"{YTDL}/v1/info").respond(json={"duration": 181, "title": "Song"})
    async with httpx.AsyncClient() as http:
        probe = await YtdlProber(http, YTDL, "key").info(SONG_URL)

    assert probe == Probe(181.0, "Song")
    assert route.calls.last.request.headers["x-api-key"] == "key"


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"duration": None, "title": "Live stream"}),
        httpx.Response(200, json={"duration": -2000, "title": "Negative"}),
        httpx.Response(200, json={"duration": 3601, "title": "Too long"}),
        httpx.Response(200, json=[180]),
    ],
)
async def test_ytdl_prober_rejects_unusable_answers(response: httpx.Response) -> None:
    respx.post(f"{YTDL}/v1/info").mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProbeError):
            await YtdlProber(http, YTDL, "key").info(SONG_URL)


async def test_ytdl_prober_runs_at_most_four_probes_at_once() -> None:
    in_flight = 0
    peak = 0

    async def slow_info(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, json={"duration": 60, "title": "Song"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_info)) as http:
        prober = YtdlProber(http, YTDL, "key")
        await asyncio.gather(*(prober.info(SONG_URL) for _ in range(10)))

    assert peak == 4
