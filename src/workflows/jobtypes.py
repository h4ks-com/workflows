import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, model_validator
from pydantic.config import JsonDict

PROBE_TIMEOUT_SECONDS = 60.0
PROBE_CONCURRENCY = 4
PROBE_LIMITS = httpx.Limits(max_connections=PROBE_CONCURRENCY, max_keepalive_connections=2)
MAX_MEDIA_SECONDS = 3600
YTDL_API_KEY_HEADER = "x-api-key"
SHORT_TEXT = 200
MEDIUM_TEXT = 500
LONG_TEXT = 2000
LYRICS_TEXT = 6000
TEXTAREA = "textarea"
MULTILINE: JsonDict = {"format": TEXTAREA}
UPLOAD = "x-upload"
SHOW_WHEN = "x-show-when"
MEDIA_UPLOAD: JsonDict = {UPLOAD: "audio/*,video/*"}


class ProbeError(Exception):
    pass


@dataclass(frozen=True)
class Probe:
    duration_seconds: float
    title: str

    def __post_init__(self) -> None:
        if not 0 < self.duration_seconds <= MAX_MEDIA_SECONDS:
            raise ProbeError(
                f"the media must be between 0 and {MAX_MEDIA_SECONDS // 60} minutes long"
            )


class ProbeInfo(BaseModel):
    duration: float
    title: str | None = None


class Prober(Protocol):
    async def info(self, url: str) -> Probe: ...


class YtdlProber:
    def __init__(self, http: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        self._http = http
        self._base_url = base_url
        self._api_key = api_key
        self._slots = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def info(self, url: str) -> Probe:
        async with self._slots:
            info = await self._fetch(url)
        return Probe(info.duration, info.title or "")

    async def _fetch(self, url: str) -> ProbeInfo:
        try:
            response = await self._http.post(
                f"{self._base_url}/v1/info",
                json={"url": url},
                headers={YTDL_API_KEY_HEADER: self._api_key},
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return ProbeInfo.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError) as error:
            raise ProbeError(f"could not read {url}") from error


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
    prompt: str | None = Field(
        None,
        max_length=LONG_TEXT,
        json_schema_extra=MULTILINE,
        title="Prompt",
        description="What the new lyrics should be about.",
    )
    url: HttpUrl = Field(
        title="Song",
        json_schema_extra=MEDIA_UPLOAD,
        description="Link to the song (YouTube, SoundCloud, an audio file) or upload one.",
    )
    lyrics: str | None = Field(
        None,
        max_length=LYRICS_TEXT,
        json_schema_extra=MULTILINE,
        title="Original lyrics",
        description="Original lyrics of the song. Leave empty to look them up automatically.",
    )
    parody_lyrics: str | None = Field(
        None,
        max_length=LYRICS_TEXT,
        json_schema_extra=MULTILINE,
        title="New lyrics",
        description="Your new lyrics, line by line. Leave empty to write them from the prompt.",
    )
    amount: Literal["a few words", "most lines", "every line"] = Field(
        "most lines", title="How much to change", description="How much of the lyrics to replace."
    )
    title: str | None = Field(
        None, max_length=SHORT_TEXT, title="Title", description="Title of the result."
    )
    radio: bool = Field(False, title="Play on radio", description="Also play it on h4ks radio.")

    def media_url(self) -> str:
        return str(self.url)

    def quote(self, probe: Probe | None) -> int:
        return round(1.2 * media_seconds(probe) + 120)


class SongParams(JobParams):
    prompt: str = Field(
        min_length=1,
        max_length=LONG_TEXT,
        json_schema_extra=MULTILINE,
        title="Prompt",
        description="What the song should be about.",
    )
    lyrics: str | None = Field(
        None,
        max_length=LYRICS_TEXT,
        json_schema_extra=MULTILINE,
        title="Lyrics",
        description="Lyrics to sing. Leave empty to have them written for you.",
    )
    style: str | None = Field(
        None,
        max_length=MEDIUM_TEXT,
        title="Style",
        description="Genre, mood or style, for example lo-fi hip hop.",
    )
    seconds: int = Field(
        150, ge=60, le=240, title="Length (seconds)", description="Length in seconds."
    )
    model: Literal["ace-step", "minimax"] = Field(
        "ace-step",
        title="Model",
        description="Music model: ace-step is fast, minimax sounds better but costs more.",
    )

    def quote(self, probe: Probe | None) -> int:
        per_second = 0.3 if self.model == "ace-step" else 1.8
        return round(per_second * self.seconds + 60)


class VoiceParams(JobParams):
    url: HttpUrl = Field(
        title="Song",
        json_schema_extra=MEDIA_UPLOAD,
        description="Link to the song whose voice you want to replace, or upload one.",
    )
    voice_url: HttpUrl = Field(
        title="New voice",
        json_schema_extra=MEDIA_UPLOAD,
        description="Link to a recording of the new voice, or upload one. A song works too.",
    )

    def media_url(self) -> str:
        return str(self.url)

    def quote(self, probe: Probe | None) -> int:
        return round(2 * media_seconds(probe) + 90)


class PodcastParams(JobParams):
    prompt: str = Field(
        min_length=1,
        max_length=LONG_TEXT,
        json_schema_extra=MULTILINE,
        title="Prompt",
        description="What the episode should be about.",
    )
    minutes: int = Field(6, ge=2, le=15, title="Length (minutes)", description="Length in minutes.")
    bed_style: str | None = Field(
        None,
        max_length=MEDIUM_TEXT,
        title="Background music",
        description="Style of the background music, for example soft jazz.",
    )

    def quote(self, probe: Probe | None) -> int:
        return round(90 * self.minutes + 120)


class ImageParams(JobParams):
    prompt: str = Field(
        min_length=1,
        max_length=LONG_TEXT,
        json_schema_extra=MULTILINE,
        title="Prompt",
        description="What the image should show.",
    )
    model: Literal["z-image", "flux-klein"] = Field(
        "z-image",
        title="Model",
        description="z-image looks best from a prompt; flux-klein is faster and takes a reference.",
    )
    reference_url: HttpUrl | None = Field(
        None,
        title="Reference image",
        json_schema_extra={UPLOAD: "image/*", SHOW_WHEN: {"model": "flux-klein"}},
        description="An image to edit or take the look from. Link, upload or drop one.",
    )
    shape: Literal["square", "portrait", "landscape"] = Field(
        "square", title="Shape", description="Shape of the image."
    )

    @model_validator(mode="after")
    def reference_needs_flux_klein(self) -> ImageParams:
        if self.reference_url is not None and self.model != "flux-klein":
            raise ValueError("a reference image needs the flux-klein model")
        return self

    def quote(self, probe: Probe | None) -> int:
        return 40


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
    webhook: str | None = None
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
        description="Replace the lyrics of a song, sung in the original voice.",
        pricing="1.2 credits per second of song + 120",
        params_model=ParodyParams,
        steps=_steps(
            ("fetch", 5),
            ("separate", 20),
            ("align", 5),
            ("lyrics", 10),
            ("sing", 50),
            ("splice", 10),
        ),
        webhook="workflows-parody",
    ),
    JobType(
        name="song",
        title="Song",
        description="Create a new song from a description.",
        pricing="0.3 credits per second (1.8 with minimax) + 60",
        params_model=SongParams,
        steps=_steps(("write", 20), ("generate", 70), ("store", 10)),
        webhook="workflows-song",
    ),
    JobType(
        name="voice",
        title="Voice swap",
        description="Replace the voice in a song with another voice.",
        pricing="2 credits per second of song + 90",
        params_model=VoiceParams,
        steps=_steps(("fetch", 10), ("separate", 30), ("convert", 45), ("mix", 15)),
        webhook="workflows-voice",
    ),
    JobType(
        name="podcast",
        title="Podcast episode",
        description="Create a two-host podcast episode about any topic.",
        pricing="90 credits per minute + 120",
        params_model=PodcastParams,
        steps=_steps(
            ("research", 10), ("cast", 5), ("bed", 15), ("write", 20), ("speak", 40), ("mix", 10)
        ),
        webhook="workflows-podcast",
    ),
    JobType(
        name="image",
        title="Image",
        description="Create an image from a description.",
        pricing="40 credits per image",
        params_model=ImageParams,
        steps=_steps(("generate", 85), ("store", 15)),
        webhook="workflows-image",
    ),
)


class RetiredParams(JobParams):
    def quote(self, probe: Probe | None) -> int:
        return 0


def retired_type(name: str) -> JobType:
    return JobType(
        name=name,
        title=name,
        description="This job type is no longer offered.",
        pricing="not for sale",
        params_model=RetiredParams,
        steps=(),
    )


def build_registry(n8n_url: str) -> dict[str, JobType]:
    return {
        job_type.name: replace(
            job_type,
            executor_url=f"{n8n_url.rstrip('/')}/webhook/{job_type.webhook}"
            if n8n_url and job_type.webhook
            else "",
        )
        for job_type in JOB_TYPES
    }
