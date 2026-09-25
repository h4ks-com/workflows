import asyncio
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel
from pydantic import ValidationError

PROBE_TIMEOUT_SECONDS = 60.0
PROBE_CONCURRENCY = 4
PROBE_LIMITS = httpx.Limits(max_connections=PROBE_CONCURRENCY, max_keepalive_connections=2)
MAX_MEDIA_SECONDS = 3600
YTDL_API_KEY_HEADER = "x-api-key"


class ProbeError(Exception):
    pass


@dataclass(frozen=True)
class Probe:
    """The length and title of the media behind a link."""

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
    """Reads the length and title of the media behind a link."""

    async def info(self, url: str) -> Probe: ...


class YtdlProber:
    """Probes links through the ytdl service, a few at a time."""

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
