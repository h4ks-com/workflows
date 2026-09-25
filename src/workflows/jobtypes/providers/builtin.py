from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import HttpUrl
from pydantic.config import JsonDict

from workflows.jobtypes.catalog import HttpExecutor
from workflows.jobtypes.catalog import JobType
from workflows.jobtypes.catalog import StaticProvider
from workflows.jobtypes.catalog import Step
from workflows.jobtypes.forms import SHOW_WHEN
from workflows.jobtypes.forms import TEXTAREA
from workflows.jobtypes.forms import UPLOAD
from workflows.jobtypes.forms import form_from_schema
from workflows.jobtypes.pricing import PriceRule

SHORT_TEXT = 200
MEDIUM_TEXT = 500
LONG_TEXT = 2000
LYRICS_TEXT = 6000
MULTILINE: JsonDict = {"format": TEXTAREA}
MEDIA_UPLOAD: JsonDict = {UPLOAD: "audio/*,video/*"}


class BuiltinParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParodyParams(BuiltinParams):
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


class SongParams(BuiltinParams):
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


class VoiceParams(BuiltinParams):
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


class PodcastParams(BuiltinParams):
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


class ImageParams(BuiltinParams):
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
    size: Literal["normal", "large"] = Field(
        "normal",
        title="Size",
        description="normal is about 1 megapixel; large is about 2.3 and costs double.",
    )


@dataclass(frozen=True)
class Builtin:
    name: str
    title: str
    description: str
    pricing: str
    price: str
    params: type[BuiltinParams]
    steps: tuple[tuple[str, int], ...]
    webhook: str


BUILTINS = (
    Builtin(
        name="parody",
        title="Parody",
        description="Replace the lyrics of a song, sung in the original voice.",
        pricing="1.2 credits per second of song + 120",
        price="1.2 * duration(url) + 120",
        params=ParodyParams,
        steps=(
            ("fetch", 5),
            ("separate", 20),
            ("align", 5),
            ("lyrics", 10),
            ("sing", 50),
            ("splice", 10),
        ),
        webhook="workflows-parody",
    ),
    Builtin(
        name="song",
        title="Song",
        description="Create a new song from a description.",
        pricing="0.3 credits per second (1.8 with minimax) + 60",
        price="(0.3 if model == 'ace-step' else 1.8) * seconds + 60",
        params=SongParams,
        steps=(("write", 20), ("generate", 70), ("store", 10)),
        webhook="workflows-song",
    ),
    Builtin(
        name="voice",
        title="Voice swap",
        description="Replace the voice in a song with another voice.",
        pricing="2 credits per second of song + 90",
        price="2 * duration(url) + 90",
        params=VoiceParams,
        steps=(("fetch", 10), ("separate", 30), ("convert", 45), ("mix", 15)),
        webhook="workflows-voice",
    ),
    Builtin(
        name="podcast",
        title="Podcast episode",
        description="Create a two-host podcast episode about any topic.",
        pricing="90 credits per minute + 120",
        price="90 * minutes + 120",
        params=PodcastParams,
        steps=(
            ("research", 10),
            ("cast", 5),
            ("bed", 15),
            ("write", 20),
            ("speak", 40),
            ("mix", 10),
        ),
        webhook="workflows-podcast",
    ),
    Builtin(
        name="image",
        title="Image",
        description="Create an image from a description.",
        pricing="40 credits per image, 80 for large",
        price="80 if size == 'large' else 40",
        params=ImageParams,
        steps=(("generate", 85), ("store", 15)),
        webhook="workflows-image",
    ),
)


def builtin_job_types(http: httpx.AsyncClient, n8n_url: str, token: str) -> list[JobType]:
    """Build the built-in job types, run by n8n webhooks under `n8n_url`."""
    return [
        JobType(
            name=builtin.name,
            title=builtin.title,
            description=builtin.description,
            pricing=builtin.pricing,
            form=form_from_schema(builtin.params.model_json_schema()),
            price=PriceRule(builtin.price),
            steps=tuple(Step(name, weight) for name, weight in builtin.steps),
            executor=HttpExecutor(http, f"{n8n_url.rstrip('/')}/webhook/{builtin.webhook}", token)
            if n8n_url
            else None,
        )
        for builtin in BUILTINS
    ]


def builtin_provider(http: httpx.AsyncClient, n8n_url: str, token: str) -> StaticProvider:
    """Serve the built-in job types, for running without an n8n API key."""
    return StaticProvider(builtin_job_types(http, n8n_url, token))
