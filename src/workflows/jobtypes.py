from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

PROBE_TIMEOUT_SECONDS = 60.0
YTDL_API_KEY_HEADER = "x-api-key"


@dataclass(frozen=True)
class Probe:
    duration_seconds: float
    title: str


class ProbeError(Exception):
    pass


class Prober(Protocol):
    async def info(self, url: str) -> Probe: ...


class YtdlProber:
    def __init__(self, http: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        self._http = http
        self._base_url = base_url
        self._api_key = api_key

    async def info(self, url: str) -> Probe:
        try:
            response = await self._http.post(
                f"{self._base_url}/v1/info",
                json={"url": url},
                headers={YTDL_API_KEY_HEADER: self._api_key},
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProbeError(f"could not read {url}") from error
        duration = body.get("duration")
        if not isinstance(duration, int | float):
            raise ProbeError(f"could not find the duration of {url}")
        title = body.get("title")
        return Probe(float(duration), title if isinstance(title, str) else "")


def media_seconds(probe: Probe | None) -> float:
    if probe is None:
        raise ProbeError("this job type needs a probed input")
    return probe.duration_seconds


class JobParams(BaseModel, ABC):
    model_config = ConfigDict(extra="forbid")

    def media_url(self) -> str | None:
        return None

    @abstractmethod
    def quote(self, probe: Probe | None) -> int: ...

    def estimate_seconds(self, probe: Probe | None) -> int:
        return self.quote(probe)


class ParodyParams(JobParams):
    url: HttpUrl = Field(description="Song to parody: YouTube, SoundCloud or a direct audio URL.")
    lyrics: str | None = Field(
        None, description="Original lyrics. Fetched from lrclib when omitted."
    )
    parody_lyrics: str | None = Field(
        None, description="Finished parody lyrics. Written from the idea when omitted."
    )
    idea: str | None = Field(None, description="What the parody is about.")
    amount: Literal["a few words", "most lines", "every line"] = Field(
        "most lines", description="How much of the original lyrics to change."
    )
    title: str | None = Field(None, description="Title for the result.")
    radio: bool = Field(False, description="Also play the result on h4ks radio.")

    def media_url(self) -> str:
        return str(self.url)

    def quote(self, probe: Probe | None) -> int:
        return round(1.2 * media_seconds(probe) + 120)


class SongParams(JobParams):
    prompt: str = Field(min_length=1, description="What the song is about.")
    lyrics: str | None = Field(
        None, description="Lyrics to sing. Written from the prompt when omitted."
    )
    style: str | None = Field(None, description="Musical style, genre or mood.")
    seconds: int = Field(150, ge=60, le=240, description="Song length in seconds.")
    model: Literal["ace-step", "minimax"] = Field("ace-step", description="Music model.")

    def quote(self, probe: Probe | None) -> int:
        per_second = 0.3 if self.model == "ace-step" else 1.8
        return round(per_second * self.seconds + 60)


class VoiceParams(JobParams):
    url: HttpUrl = Field(description="Song whose vocals get the new voice.")
    voice_url: HttpUrl = Field(
        description="Clip or song with the target voice. We use its loudest 30 seconds."
    )

    def media_url(self) -> str:
        return str(self.url)

    def quote(self, probe: Probe | None) -> int:
        return round(2 * media_seconds(probe) + 90)


class PodcastParams(JobParams):
    prompt: str = Field(min_length=1, description="Topic of the episode.")
    minutes: int = Field(6, ge=2, le=15, description="Episode length in minutes.")
    bed_style: str | None = Field(None, description="Style of the background music bed.")

    def quote(self, probe: Probe | None) -> int:
        return round(90 * self.minutes + 120)


@dataclass(frozen=True)
class Step:
    name: str
    weight: int


@dataclass(frozen=True)
class JobType:
    name: str
    title: str
    description: str
    pricing: str
    params_model: type[JobParams]
    steps: tuple[Step, ...]
    executor_url: str = ""

    @property
    def available(self) -> bool:
        return bool(self.executor_url)

    def step_names(self) -> list[str]:
        return [step.name for step in self.steps]


@dataclass(frozen=True)
class Quote:
    credits: int
    estimate_seconds: int
    probe: Probe | None


async def quote(params: JobParams, prober: Prober) -> Quote:
    url = params.media_url()
    probe = await prober.info(url) if url else None
    return Quote(params.quote(probe), params.estimate_seconds(probe), probe)


def _steps(*pairs: tuple[str, int]) -> tuple[Step, ...]:
    return tuple(Step(name, weight) for name, weight in pairs)


JOB_TYPES = (
    JobType(
        name="parody",
        title="Parody",
        description="Rewrite the lyrics of a song and sing them in the original voice.",
        pricing="1.2 credits per second of the song, plus 120",
        params_model=ParodyParams,
        steps=_steps(
            ("fetch", 5),
            ("separate", 20),
            ("align", 5),
            ("lyrics", 10),
            ("sing", 50),
            ("splice", 10),
        ),
    ),
    JobType(
        name="song",
        title="Song",
        description="Write and generate a new song from a prompt.",
        pricing="ace-step: 0.3 credits per second plus 60; minimax: 1.8 credits per second plus 60",
        params_model=SongParams,
        steps=_steps(("write", 20), ("generate", 70), ("store", 10)),
    ),
    JobType(
        name="voice",
        title="Voice swap",
        description="Sing a song in another voice.",
        pricing="2 credits per second of the song, plus 90",
        params_model=VoiceParams,
        steps=_steps(("fetch", 10), ("separate", 30), ("convert", 45), ("mix", 15)),
    ),
    JobType(
        name="podcast",
        title="Podcast episode",
        description="Research, write and voice a podcast episode about a topic.",
        pricing="90 credits per minute, plus 120",
        params_model=PodcastParams,
        steps=_steps(
            ("research", 10), ("cast", 5), ("bed", 15), ("write", 20), ("speak", 40), ("mix", 10)
        ),
    ),
)


def build_registry(executor_urls: Mapping[str, str]) -> dict[str, JobType]:
    return {
        job_type.name: replace(job_type, executor_url=executor_urls.get(job_type.name, ""))
        for job_type in JOB_TYPES
    }
