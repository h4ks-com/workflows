import httpx
from pydantic import BaseModel, Field, ValidationError

from workflows.catalog import HttpExecutor, JobType, ProviderError, Step
from workflows.db import JsonObject
from workflows.forms import form_from_schema
from workflows.pricing import PriceError, PriceRule

MANIFEST_PATH = "/v1/workflow"
JOBS_PATH = "/v1/workflow/jobs"
MANIFEST_TIMEOUT_SECONDS = 10.0


class ServiceManifest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    title: str
    description: str
    pricing: str
    price: str
    steps: list[tuple[str, int]] = Field(min_length=1)
    params_schema: JsonObject


class ServiceProvider:
    def __init__(self, http: httpx.AsyncClient, base_url: str, executor_token: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._executor_token = executor_token
        self.errors: dict[str, str] = {}

    async def discover(self) -> list[JobType]:
        manifest = await self._manifest()
        form = form_from_schema(manifest.params_schema)
        try:
            price = PriceRule(manifest.price)
        except PriceError as error:
            self.errors = {self._base_url: f"its price is invalid: {error}"}
            return []
        missing = price.fields - {spec.name for spec in form.fields}
        if missing:
            self.errors = {self._base_url: f"its price reads unknown fields: {sorted(missing)}"}
            return []
        self.errors = {}
        return [
            JobType(
                name=manifest.name,
                title=manifest.title,
                description=manifest.description,
                pricing=manifest.pricing,
                form=form,
                price=price,
                steps=tuple(Step(name, weight) for name, weight in manifest.steps),
                executor=HttpExecutor(
                    self._http, f"{self._base_url}{JOBS_PATH}", self._executor_token
                ),
            )
        ]

    async def _manifest(self) -> ServiceManifest:
        try:
            response = await self._http.get(
                f"{self._base_url}{MANIFEST_PATH}",
                headers={"X-API-Key": self._executor_token},
                timeout=MANIFEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return ServiceManifest.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError) as error:
            raise ProviderError(
                f"could not read {self._base_url}{MANIFEST_PATH}: {error}"
            ) from error
