import asyncio
import logging
from dataclasses import dataclass
from typing import Annotated

import httpx
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from workflows.bus import QUEUE_TOPIC, EventBus
from workflows.db import Job, JobStatus, JsonObject, Subscription
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
SUBSCRIBED_STATUSES = NOTIFIED_STATUSES | {JobStatus.QUEUED}

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
    label: str
    url: str
    token: str
    payload: JsonObject


def build_delivery(label: str, hook: Webhook, message: str, details: JsonObject) -> Delivery:
    payload: JsonObject = {**hook.extra_params, "message": hook.message_prefix + message, **details}
    return Delivery(label, str(hook.url), hook.token, payload)


def announcement(job: Job, status: JobStatus, title: str, run_url: str, urls: list[str]) -> str:
    owner = job.owner.username if job.owner else "someone"
    if status == JobStatus.QUEUED:
        return f"{owner} submitted a {title}, #{job.id}: {run_url}"
    subject = f"{owner}'s {title} #{job.id}"
    match status:
        case JobStatus.RUNNING:
            return f"{subject} started"
        case JobStatus.SUCCEEDED:
            return f"{subject} is done: {' '.join(urls) or run_url}"
        case JobStatus.FAILED:
            return f"{subject} failed: {job.error}"
        case _:
            return f"{subject} was cancelled"


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
                    if change.status in SUBSCRIBED_STATUSES:
                        deliveries.create_task(self.notify(change.job_id, change.status))

    async def notify(self, job_id: int, status: JobStatus) -> None:
        try:
            deliveries = self._deliveries(job_id, status)
        except SQLAlchemyError:
            logger.exception("could not read job %s for its webhooks", job_id)
            return
        async with asyncio.TaskGroup() as sends:
            for each in deliveries:
                sends.create_task(self._send(each))

    def _deliveries(self, job_id: int, status: JobStatus) -> list[Delivery]:
        with self._sessions() as session:
            job = session.get_one(Job, job_id)
            title = job_type_for(self._registry, job.type).title.lower()
            run_url = f"{self._base_url}/jobs/{job.id}"
            urls = result_urls(job, status)
            details: JsonObject = {
                "job_id": job.id,
                "type": job.type,
                "status": status,
                "run_url": run_url,
                "result_urls": list(urls),
            }
            public_message = announcement(job, status, title, run_url, urls)
            deliveries = [
                build_delivery(
                    f"subscription for job {job.id}",
                    Webhook.model_validate(subscription, from_attributes=True),
                    public_message,
                    details,
                )
                for subscription in session.scalars(select(Subscription))
                if job.queued_at is not None
            ]
            if job.webhook is not None and status in NOTIFIED_STATUSES:
                message = f"your {title} #{job.id} {describe(job, status, run_url, urls)}"
                webhook = Webhook.model_validate(job.webhook)
                deliveries.append(
                    build_delivery(f"webhook for job {job.id}", webhook, message, details)
                )
            return deliveries

    async def _send(self, delivery: Delivery) -> None:
        for retry_delay in (*RETRY_DELAYS_SECONDS, None):
            failure = await self._post(delivery)
            if failure is None:
                return
            if retry_delay is None:
                break
            await asyncio.sleep(retry_delay)
        logger.warning("%s failed: %s", delivery.label, failure)

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
            logger.warning("%s refused: status %s", delivery.label, response.status_code)
        return None
