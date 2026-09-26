import asyncio

import httpx
import pytest
import respx

from workflows.jobtypes.catalog import Catalog
from workflows.jobtypes.catalog import DispatchRequest
from workflows.jobtypes.catalog import HttpExecutor
from workflows.jobtypes.catalog import JobType
from workflows.jobtypes.catalog import ProviderError
from workflows.jobtypes.catalog import StaticProvider
from workflows.jobtypes.forms import Form
from workflows.jobtypes.pricing import PriceRule

EXECUTOR_URL = "https://executor.test/run"
REQUEST = DispatchRequest(1, "echo", {}, ["run"], "https://cb", "token")


def job_type(name: str, title: str = "") -> JobType:
    return JobType(name, title or name, "", "", Form(name, ()), PriceRule("1"), ())


class FailingProvider:
    def __init__(self) -> None:
        self.failing = False
        self.job_types = [job_type("echo")]
        self.errors: dict[str, str] = {}

    async def discover(self) -> list[JobType]:
        if self.failing:
            raise ProviderError("n8n is down")
        return self.job_types


async def test_catalog_merges_providers_and_keeps_the_first_name() -> None:
    catalog = Catalog(
        [
            StaticProvider([job_type("echo", "first")]),
            StaticProvider([job_type("echo", "second"), job_type("song")]),
        ]
    )
    await catalog.refresh()

    assert [found.name for found in catalog.all()] == ["echo", "song"]
    assert catalog.find("echo").title == "first"


async def test_catalog_keeps_the_last_good_types_when_a_provider_fails() -> None:
    provider = FailingProvider()
    catalog = Catalog([provider])
    await catalog.refresh()

    provider.failing = True
    await catalog.refresh()

    assert catalog.get("echo") is not None


class HangingProvider(FailingProvider):
    async def discover(self) -> list[JobType]:
        if self.failing:
            await asyncio.Event().wait()
        return self.job_types


async def test_a_hanging_provider_keeps_its_last_types_and_frees_the_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("workflows.jobtypes.catalog.DISCOVER_TIMEOUT_SECONDS", 0.01)
    hanging = HangingProvider()
    catalog = Catalog([hanging, StaticProvider([job_type("song")])])
    await catalog.refresh()

    hanging.failing = True
    await catalog.refresh()

    assert [found.name for found in catalog.all()] == ["echo", "song"]


async def test_catalog_returns_a_retired_type_for_unknown_names() -> None:
    catalog = Catalog([StaticProvider([])])
    await catalog.refresh()

    retired = catalog.find("gone")
    assert catalog.get("gone") is None
    assert (retired.name, retired.available) == ("gone", False)


@respx.mock
@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (httpx.Response(202), "accepted"),
        (httpx.Response(500), "refused"),
        (httpx.ConnectError("down"), "refused"),
        (httpx.ReadTimeout("slow"), "unconfirmed"),
    ],
)
async def test_http_executor_reports_the_dispatch_outcome(
    response: httpx.Response | Exception, outcome: str
) -> None:
    route = respx.post(EXECUTOR_URL).mock(side_effect=[response])
    async with httpx.AsyncClient() as http:
        result = await HttpExecutor(http, EXECUTOR_URL, "key").dispatch(REQUEST)

    assert result == outcome
    assert route.calls.last.request.headers["x-api-key"] == "key"
