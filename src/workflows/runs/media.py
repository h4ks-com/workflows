import asyncio
import mimetypes
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote

import httpx
from minio import Minio
from pydantic import BaseModel
from pydantic import ValidationError

from workflows.jobtypes.forms import Form
from workflows.settings import Settings

DOWNLOAD_TIMEOUT_SECONDS = 1800.0
SIGNED_URL_LIFETIME = timedelta(hours=12)
CHUNK_BYTES = 1 << 20
YTDL_API_KEY_HEADER = "x-api-key"


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


def media_fields(form: Form) -> list[str]:
    """Name the link fields that take audio, whose links we download before dispatch."""
    return [
        spec.name
        for spec in form.fields
        if spec.value_type == "url" and spec.accept is not None and "audio/" in spec.accept
    ]


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

    async def stage(self, key: str, url: str) -> StagedMedia:
        """Download the audio behind `url` and store it under `key`.

        :raises StagingError: when the link cannot be downloaded.
        """
        with tempfile.TemporaryDirectory() as workdir:
            path = Path(workdir) / "media"
            try:
                async with self._http.stream(
                    "POST",
                    f"{self._ytdl_url}/v1/file",
                    json={"url": url, "mode": "audio"},
                    headers={YTDL_API_KEY_HEADER: self._ytdl_api_key},
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                ) as response:
                    if response.is_error:
                        await response.aread()
                        raise StagingError(f"could not download {url}: {_reason(response)}")
                    with path.open("wb") as file:
                        async for chunk in response.aiter_bytes(CHUNK_BYTES):
                            file.write(chunk)
                    headers = response.headers
            except httpx.HTTPError as error:
                raise StagingError(f"could not download {url}") from error
            content_type = headers.get("content-type", "application/octet-stream")
            suffix = mimetypes.guess_extension(content_type.split(";")[0]) or ""
            signed = await self._store.keep(f"{key}{suffix}", path, content_type)
        return StagedMedia(
            url=signed,
            title=unquote(headers.get("x-media-title", "")),
            duration_seconds=_duration(headers.get("x-media-duration", "")),
        )


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
