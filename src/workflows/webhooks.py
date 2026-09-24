import asyncio
import logging
from dataclasses import dataclass
from typing import Annotated

import httpx
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from workflows.bus import QUEUE_TOPIC, EventBus
from workflows.db import Job, JobStatus, JsonObject
from workflows.jobs import STATUS_EVENT, ResultFile, job_type_for
from workflows.jobtypes import JobType

logger = logging.getLogger(__name__)

WEBHOOK_TIMEOUT_SECONDS = 10.0
RETRY_DELAYS_SECONDS = (1.0, 4.0, 16.0)
NOTIFIED_STATUSES = frozenset(
    {
        JobStatus.RUNNING,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }
)

ShortStr = Annotated[str, Field(max_length=200)]


class Webhook(BaseModel):
    url: HttpUrl = Field(description="URL that receives a POST on every status change.")
    token: str = Field(
        min_length=1, max_length=500, description="Sent back as `Authorization: Bearer <token>`."
    )
    extra_params: dict[ShortStr, ShortStr] = Field(
        default_factory=dict, max_length=10, description="Fields merged into every payload as-is."
    )
    message_prefix: str = Field("", max_length=100, description="Prepended to `message`.")


class StatusChange(BaseModel):
    job_id: int
    status: JobStatus


class ResultFiles(BaseModel):
    files: list[ResultFile]


@dataclass(frozen=True)
class Delivery:
    job_id: int
    url: str
    token: str
    payload: JsonObject


def describe(job: Job, status: JobStatus, run_url: str, result_urls: list[str]) -> str:
    match status:
        case JobStatus.RUNNING:
            return f"started: {run_url}"
        case JobStatus.SUCCEEDED:
            return f"is done: {' '.join(result_urls) or run_url}"
        case JobStatus.FAILED:
            return f"failed: {job.error} ({run_url})"
        case _:
            return "was cancelled"


def result_urls(job: Job, status: JobStatus) -> list[str]:
    if status != JobStatus.SUCCEEDED or job.result is None:
        return []
    return [file.url for file in ResultFiles.model_validate(job.result).files]


class WebhookNotifier:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        registry: dict[str, JobType],
        bus: EventBus,
        http: httpx.AsyncClient,
        base_url: str,
    ) -> None:
        self._sessions = sessions
        self._registry = registry
        self._bus = bus
        self._http = http
        self._base_url = base_url

    async def run(self) -> None:
        with self._bus.subscribe(QUEUE_TOPIC) as events:
            async with asyncio.TaskGroup() as deliveries:
                while True:
                    event = await events.get()
                    if event.kind != STATUS_EVENT:
                        continue
                    change = StatusChange.model_validate(event.data)
                    if change.status in NOTIFIED_STATUSES:
                        deliveries.create_task(self.notify(change.job_id, change.status))

    async def notify(self, job_id: int, status: JobStatus) -> None:
        try:
            delivery = self._delivery(job_id, status)
        except SQLAlchemyError:
            logger.exception("could not read job %s for its webhook", job_id)
            return
        if delivery is not None:
            await self._send(delivery)

    def _delivery(self, job_id: int, status: JobStatus) -> Delivery | None:
        with self._sessions() as session:
            job = session.get_one(Job, job_id)
            if job.webhook is None:
                return None
            webhook = Webhook.model_validate(job.webhook)
            title = job_type_for(self._registry, job.type).title.lower()
            run_url = f"{self._base_url}/jobs/{job.id}"
            urls = result_urls(job, status)
            message = f"your {title} #{job.id} {describe(job, status, run_url, urls)}"
            payload: JsonObject = {
                **webhook.extra_params,
                "message": webhook.message_prefix + message,
                "job_id": job.id,
                "type": job.type,
                "status": status,
                "run_url": run_url,
                "result_urls": list(urls),
            }
            return Delivery(job.id, str(webhook.url), webhook.token, payload)

    async def _send(self, delivery: Delivery) -> None:
        for retry_delay in (*RETRY_DELAYS_SECONDS, None):
            failure = await self._post(delivery)
            if failure is None:
                return
            if retry_delay is None:
                break
            await asyncio.sleep(retry_delay)
        logger.warning("webhook for job %s failed: %s", delivery.job_id, failure)

    async def _post(self, delivery: Delivery) -> str | None:
        try:
            response = await self._http.post(
                delivery.url,
                json=delivery.payload,
                headers={"Authorization": f"Bearer {delivery.token}"},
            )
        except httpx.TransportError as error:
            return type(error).__name__
        if response.is_server_error:
            return f"status {response.status_code}"
        if response.is_error:
            logger.warning(
                "webhook for job %s refused: status %s", delivery.job_id, response.status_code
            )
        return None
