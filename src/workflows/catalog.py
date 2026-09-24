import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from workflows.db import JsonObject
from workflows.forms import Form
from workflows.pricing import PriceRule
from workflows.probe import Probe, Prober

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 60.0
DISPATCH_TIMEOUT_SECONDS = 30.0
EXECUTOR_AUTH_HEADER = "X-API-Key"

type DispatchOutcome = Literal["accepted", "refused", "unconfirmed"]


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class Step:
    name: str
    weight: int


@dataclass(frozen=True)
class Quote:
    credits: int
    estimate_seconds: int
    probe: Probe | None


@dataclass(frozen=True)
class DispatchRequest:
    job_id: int
    type: str
    params: JsonObject
    steps: list[str]
    callback_url: str
    callback_token: str

    def payload(self) -> JsonObject:
        return {
            "job_id": self.job_id,
            "type": self.type,
            "params": self.params,
            "steps": list(self.steps),
            "callback_url": self.callback_url,
            "callback_token": self.callback_token,
        }


class Executor(Protocol):
    async def dispatch(self, request: DispatchRequest) -> DispatchOutcome: ...


class HttpExecutor:
    def __init__(self, http: httpx.AsyncClient, url: str, token: str) -> None:
        self._http = http
        self.url = url
        self._token = token

    async def dispatch(self, request: DispatchRequest) -> DispatchOutcome:
        try:
            response = await self._http.post(
                self.url,
                json=request.payload(),
                headers={EXECUTOR_AUTH_HEADER: self._token},
                timeout=DISPATCH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except (httpx.HTTPStatusError, httpx.ConnectError) as error:
            logger.warning("executor refused job %s: %s", request.job_id, error)
            return "refused"
        except httpx.HTTPError as error:
            logger.warning("dispatch of job %s is unconfirmed: %r", request.job_id, error)
            return "unconfirmed"
        return "accepted"


@dataclass(frozen=True)
class JobType:
    name: str
    title: str
    description: str
    pricing: str
    form: Form
    price: PriceRule
    steps: tuple[Step, ...]
    executor: Executor | None = None

    @property
    def available(self) -> bool:
        return self.executor is not None

    def step_names(self) -> list[str]:
        return [step.name for step in self.steps]

    def validate(self, raw: JsonObject) -> JsonObject:
        return self.form.validate(raw)

    async def quote(self, params: JsonObject, prober: Prober) -> Quote:
        media_field = self.price.media_field
        media_url = params.get(media_field) if media_field else None
        probe = await prober.info(media_url) if isinstance(media_url, str) else None
        credits = self.price.evaluate(params, probe)
        return Quote(credits, credits, probe)


def retired_type(name: str) -> JobType:
    return JobType(
        name=name,
        title=name,
        description="This job type is no longer offered.",
        pricing="not for sale",
        form=Form("RetiredParams", ()),
        price=PriceRule("0"),
        steps=(),
    )


class Provider(Protocol):
    async def discover(self) -> list[JobType]: ...


class StaticProvider:
    def __init__(self, job_types: Sequence[JobType]) -> None:
        self._job_types = list(job_types)

    async def discover(self) -> list[JobType]:
        return list(self._job_types)


class Catalog:
    def __init__(self, providers: Sequence[Provider]) -> None:
        self._providers = list(providers)
        self._found: list[list[JobType]] = [[] for _ in self._providers]
        self._types: dict[str, JobType] = {}

    async def refresh(self) -> None:
        results = await asyncio.gather(
            *(provider.discover() for provider in self._providers), return_exceptions=True
        )
        for index, result in enumerate(results):
            if isinstance(result, ProviderError):
                logger.warning("job type provider %s failed: %s", index, result)
            elif isinstance(result, BaseException):
                raise result
            else:
                self._found[index] = result
        self._types = self._merge()

    def _merge(self) -> dict[str, JobType]:
        merged: dict[str, JobType] = {}
        for job_types in self._found:
            for job_type in job_types:
                if job_type.name in merged:
                    logger.warning("job type %s is offered twice, keeping the first", job_type.name)
                    continue
                merged[job_type.name] = job_type
        return merged

    async def run(self) -> None:
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            await self.refresh()

    def get(self, name: str) -> JobType | None:
        return self._types.get(name)

    def find(self, name: str) -> JobType:
        return self._types.get(name) or retired_type(name)

    def all(self) -> list[JobType]:
        return list(self._types.values())
