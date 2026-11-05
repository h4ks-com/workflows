import asyncio
import mimetypes
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from pathlib import PurePosixPath
from typing import Literal
from typing import Protocol
from urllib.parse import unquote
from urllib.parse import urlparse

import httpx
from minio import Minio
from pydantic import BaseModel
from pydantic import ValidationError

from workflows.jobtypes.forms import Form
from workflows.settings import Settings

DOWNLOAD_TIMEOUT_SECONDS = 1800.0
SIGNED_URL_LIFETIME = timedelta(hours=12)
CHUNK_BYTES = 1 << 20
FILE_LIMIT_BYTES = 500 << 20
YTDL_API_KEY_HEADER = "x-api-key"
MIDI_TYPES = {"audio/midi", "audio/x-midi"}

type InputKind = Literal["audio", "file"]


class StagingError(Exception):
    pass


@dataclass(frozen=True)
class StagedMedia:
    """A job input downloaded to private storage, with what we learned about it."""

    url: str
    title: str
    duration_seconds: float | None


class InputStore(Protocol):
    """Keeps job inputs private and hands out links that expire."""

    async def keep(self, key: str, path: Path, content_type: str) -> str: ...


class MinioInputStore:
    def __init__(self, client: Minio, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    async def keep(self, key: str, path: Path, content_type: str) -> str:
        return await asyncio.to_thread(self._keep, key, path, content_type)

    def _keep(self, key: str, path: Path, content_type: str) -> str:
        self._client.fput_object(self._bucket, key, str(path), content_type=content_type)
        return self._client.presigned_get_object(self._bucket, key, expires=SIGNED_URL_LIFETIME)


class YtdlError(BaseModel):
    detail: str


def _takes_audio(accept: str) -> bool:
    return any(
        kind.strip().startswith("audio/") and kind.strip() not in MIDI_TYPES
        for kind in accept.split(",")
    )


def staged_fields(form: Form) -> dict[str, InputKind]:
    """Name the upload fields whose links we download before dispatch, and how.

    Audio goes through yt-dlp, which reads any page with a song on it; other files, such as
    MIDI, we fetch as they are.
    """
    return {
        spec.name: "audio" if _takes_audio(spec.accept) else "file"
        for spec in form.fields
        if spec.value_type == "url" and spec.accept is not None
    }


def _reason(response: httpx.Response) -> str:
    try:
        return YtdlError.model_validate_json(response.content).detail
    except ValidationError:
        return f"the downloader answered {response.status_code}"


def _duration(header: str) -> float | None:
    try:
        return float(header)
    except ValueError:
        return None


class MediaStager:
    """Downloads any link yt-dlp reads through the ytdl service into private storage."""

    def __init__(
        self, http: httpx.AsyncClient, ytdl_url: str, ytdl_api_key: str, store: InputStore
    ) -> None:
        self._http = http
        self._ytdl_url = ytdl_url
        self._ytdl_api_key = ytdl_api_key
        self._store = store

    async def stage(self, key: str, url: str, kind: InputKind) -> StagedMedia:
        """Download the file behind `url` and store it under `key`.

        :raises StagingError: when the link cannot be downloaded.
        """
        if kind == "audio":
            request = self._http.stream(
                "POST",
                f"{self._ytdl_url}/v1/file",
                json={"url": url, "mode": "audio"},
                headers={YTDL_API_KEY_HEADER: self._ytdl_api_key},
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
        else:
            request = self._http.stream(
                "GET", url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS
            )
        with tempfile.TemporaryDirectory() as workdir:
            path = Path(workdir) / "media"
            try:
                async with request as response:
                    if response.is_error:
                        await response.aread()
                        raise StagingError(f"could not download {url}: {_reason(response)}")
                    await _save(response, path, url)
                    headers = response.headers
            except httpx.HTTPError as error:
                raise StagingError(f"could not download {url}") from error
            content_type = headers.get("content-type", "application/octet-stream")
            suffix = (
                PurePosixPath(urlparse(url).path).suffix
                if kind == "file"
                else mimetypes.guess_extension(content_type.split(";")[0])
            )
            signed = await self._store.keep(f"{key}{suffix or ''}", path, content_type)
        return StagedMedia(
            url=signed,
            title=unquote(headers.get("x-media-title", "")),
            duration_seconds=_duration(headers.get("x-media-duration", "")),
        )


async def _save(response: httpx.Response, path: Path, url: str) -> None:
    written = 0
    with path.open("wb") as file:
        async for chunk in response.aiter_bytes(CHUNK_BYTES):
            written += len(chunk)
            if written > FILE_LIMIT_BYTES:
                raise StagingError(
                    f"could not download {url}: it is over {FILE_LIMIT_BYTES >> 20} MB"
                )
            file.write(chunk)


def build_stager(settings: Settings, http: httpx.AsyncClient) -> MediaStager | None:
    """Download audio links before dispatch when ytdl, MinIO and an inputs bucket are set."""
    if not (settings.ytdl_url and settings.inputs_bucket and settings.minio_endpoint):
        return None
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_use_ssl,
    )
    store = MinioInputStore(client, settings.inputs_bucket)
    return MediaStager(http, settings.ytdl_url, settings.ytdl_api_key, store)
