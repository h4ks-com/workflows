import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Literal
from typing import Protocol

import httpx

from workflows.db import JsonObject
from workflows.jobtypes.forms import Form
from workflows.jobtypes.pricing import PriceError
from workflows.jobtypes.pricing import PriceRule
from workflows.jobtypes.probe import Probe
from workflows.jobtypes.probe import Prober

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 60.0
DISPATCH_TIMEOUT_SECONDS = 30.0
EXECUTOR_AUTH_HEADER = "X-API-Key"

type DispatchOutcome = Literal["accepted", "refused", "unconfirmed"]


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class Step:
    """One stage an executor reports, with its share of the progress bar."""

    name: str
    weight: int


@dataclass(frozen=True)
class Quote:
    """What a job costs in credits and how many seconds it should take."""

    credits: int
    estimate_seconds: int
    probe: Probe | None


@dataclass(frozen=True)
class DispatchRequest:
    """A job as its executor receives it."""

    job_id: int
    type: str
    params: JsonObject
    steps: list[str]
    callback_url: str
    callback_token: str
    media: JsonObject = field(default_factory=dict)

    def payload(self) -> JsonObject:
        return {
            "job_id": self.job_id,
            "type": self.type,
            "params": self.params,
            "media": self.media,
            "steps": list(self.steps),
            "callback_url": self.callback_url,
            "callback_token": self.callback_token,
        }


class Executor(Protocol):
    """Runs jobs elsewhere and reports back through each job's callback URL.

    `dispatch` answers "accepted", "refused" when the executor turned the job down, or
    "unconfirmed" when it did not answer in time and the job may still be running.
    """

    async def dispatch(self, request: DispatchRequest) -> DispatchOutcome: ...


class HttpExecutor:
    """Posts each job as JSON to one URL with the executor key in `X-API-Key`."""

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
    """One kind of job: its form, price, steps and the executor that runs it."""

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
        """Price validated params, probing the media behind the price's `duration` field."""
        media_field = self.price.media_field
        media_url = params.get(media_field) if media_field else None
        probe = await prober.info(media_url) if isinstance(media_url, str) else None
        credits = self.price.evaluate(params, probe)
        return Quote(credits, credits, probe)


def form_price(form: Form, expression: str) -> PriceRule:
    """Parse a price expression that may only read fields the form has.

    :raises PriceError: when the expression is invalid or reads an unknown field.
    """
    price = PriceRule(expression)
    missing = price.fields - {spec.name for spec in form.fields}
    if missing:
        raise PriceError(f"its price reads fields the form lacks: {sorted(missing)}")
    return price


def retired_type(name: str) -> JobType:
    """Stand in for a job type no provider offers any more, so its old jobs still render."""
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
    """A source of job types.

    The catalog calls `discover` on startup and every minute. `errors` maps each entry the
    provider skipped to the reason, for the admin page.
    """

    errors: dict[str, str]

    async def discover(self) -> list[JobType]: ...


class StaticProvider:
    """Serves a fixed list of job types."""

    def __init__(self, job_types: Sequence[JobType]) -> None:
        self._job_types = list(job_types)
        self.errors: dict[str, str] = {}

    async def discover(self) -> list[JobType]:
        return list(self._job_types)


class Catalog:
    """Merges the job types of every provider; the rest of the app reads job types only here."""

    def __init__(self, providers: Sequence[Provider]) -> None:
        self._providers = list(providers)
        self._found: list[list[JobType]] = [[] for _ in self._providers]
        self._types: dict[str, JobType] = {}

    async def refresh(self) -> None:
        """Ask every provider again, keeping a provider's last good list on `ProviderError`."""
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
        """Return the job type, or a retired stand-in for a name no provider offers."""
        return self._types.get(name) or retired_type(name)

    def all(self) -> list[JobType]:
        return list(self._types.values())

    def skipped(self) -> dict[str, str]:
        return {
            name: error for provider in self._providers for name, error in provider.errors.items()
        }
