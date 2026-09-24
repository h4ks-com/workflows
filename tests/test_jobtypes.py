import asyncio

import httpx
import pytest
import respx

from conftest import SONG_URL, FakeProber
from workflows.jobtypes import (
    ImageParams,
    ParodyParams,
    PodcastParams,
    Probe,
    ProbeError,
    RetiredParams,
    SongParams,
    VoiceParams,
    YtdlProber,
    build_registry,
    media_seconds,
    quote,
)

YTDL = "http://ytdl.test"
PROBE = Probe(100.0, "Some Song")


def test_quotes_follow_the_type_formulas() -> None:
    assert ParodyParams(url=SONG_URL).quote(PROBE) == 240
    assert VoiceParams(url=SONG_URL, voice_url=SONG_URL).quote(PROBE) == 290
    assert SongParams(prompt="cats").quote(None) == 105
    assert SongParams(prompt="cats", model="minimax", seconds=100).quote(None) == 240
    assert PodcastParams(prompt="cats").quote(None) == 660
    assert RetiredParams().quote(None) == 0


def test_url_types_need_a_probe() -> None:
    with pytest.raises(ProbeError):
        media_seconds(None)


async def test_quote_probes_only_types_with_media() -> None:
    prober = FakeProber()

    parody = await quote(ParodyParams(url=SONG_URL), prober)
    song = await quote(SongParams(prompt="cats"), prober)

    assert (parody.credits, parody.estimate_seconds, parody.probe) == (240, 240, PROBE)
    assert song.probe is None


def test_registry_marks_types_without_executor_unavailable() -> None:
    registry = build_registry("")

    assert list(registry) == ["parody", "song", "voice", "podcast", "image"]
    assert not registry["parody"].available
    assert not registry["voice"].available
    assert registry["podcast"].step_names() == ["research", "cast", "bed", "write", "speak", "mix"]


def test_build_registry_derives_urls_from_n8n_url() -> None:
    registry = build_registry("https://n8n.test")

    assert registry["parody"].executor_url == "https://n8n.test/webhook/workflows-parody"
    assert registry["parody"].available
    assert not registry["voice"].available


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


def test_image_quote_is_flat() -> None:
    assert ImageParams(prompt="a cat").quote(None) == 40
