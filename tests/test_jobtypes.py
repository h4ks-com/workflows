import httpx
import pytest
import respx

from conftest import SONG_URL, FakeProber
from workflows.jobtypes import (
    ParodyParams,
    PodcastParams,
    Probe,
    ProbeError,
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
    assert VoiceParams(url=SONG_URL, voice_url=SONG_URL).quote(PROBE) == 170
    assert SongParams(prompt="cats").quote(None) == 105
    assert SongParams(prompt="cats", model="minimax", seconds=100).quote(None) == 240
    assert PodcastParams(prompt="cats").quote(None) == 660


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
    registry = build_registry({"parody": "https://n8n/webhook/parody"})

    assert list(registry) == ["parody", "song", "voice", "podcast"]
    assert registry["parody"].available
    assert not registry["voice"].available
    assert registry["podcast"].step_names() == ["research", "cast", "write", "speak", "bed", "mix"]


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
    ],
)
async def test_ytdl_prober_rejects_unusable_answers(response: httpx.Response) -> None:
    respx.post(f"{YTDL}/v1/info").mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProbeError):
            await YtdlProber(http, YTDL, "key").info(SONG_URL)
