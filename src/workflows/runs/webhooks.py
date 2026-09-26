import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass

import httpx
from pydantic import BaseModel
from pydantic import Field
from pydantic import HttpUrl
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from workflows.db import Job
from workflows.db import JobStatus
from workflows.db import JsonObject
from workflows.db import Subscription
from workflows.jobtypes.catalog import Catalog
from workflows.jobtypes.catalog import JobType
from workflows.runs.bus import QUEUE_TOPIC
from workflows.runs.bus import EventBus
from workflows.runs.jobs import STATUS_EVENT
from workflows.runs.jobs import StoredResult

logger = logging.getLogger(__name__)

WEBHOOK_TIMEOUT_SECONDS = 10.0
RETRY_DELAYS_SECONDS = (1.0, 4.0, 16.0)
SUBSCRIBED_STATUSES = frozenset(
    {
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }
)


class Subscribe(BaseModel):
    url: HttpUrl = Field(description="URL that receives a signed POST on every job event.")
    signing_key: str = Field(
        min_length=16,
        max_length=200,
        description="HMAC-SHA256 key used to sign the payload, sent as `X-Webhook-Signature`.",
    )


class StatusChange(BaseModel):
    job_id: int
    status: JobStatus


@dataclass(frozen=True)
class Delivery:
    label: str
    url: str
    payload: JsonObject
    headers: dict[str, str]


def sign(signing_key: str, payload: JsonObject) -> str:
    digest = hmac.new(
        signing_key.encode(), json.dumps(payload, sort_keys=True).encode(), hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def job_event_payload(
    job: Job, job_type: JobType, status: JobStatus, run_url: str, urls: list[str]
) -> JsonObject:
    return {
        "event": "job",
        "job_id": job.id,
        "type": job.type,
        "type_title": job_type.title,
        "status": status,
        "owner": job.owner.username if job.owner else None,
        "title": result_title(job, status),
        "run_url": run_url,
        "result_urls": list(urls),
        "error": job.error if status == JobStatus.FAILED else None,
    }


def result_title(job: Job, status: JobStatus) -> str | None:
    if status != JobStatus.SUCCEEDED or job.result is None:
        return None
    title = job.result.get("title")
    return title if isinstance(title, str) else None


def build_subscription_delivery(
    label: str, subscription: Subscription, payload: JsonObject
) -> Delivery:
    headers = {"X-Webhook-Signature": sign(subscription.signing_key, payload)}
    return Delivery(label, subscription.url, payload, headers)


def result_urls(job: Job, status: JobStatus) -> list[str]:
    if status != JobStatus.SUCCEEDED or job.result is None:
        return []
    stored = StoredResult.model_validate(job.result)
    return [file.url for file in stored.files] + [link.url for link in stored.links]


class WebhookNotifier:
    """Tells every subscription about each status change of a queued job, best effort."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        catalog: Catalog,
        bus: EventBus,
        http: httpx.AsyncClient,
        base_url: str,
    ) -> None:
        self._sessions = sessions
        self._catalog = catalog
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
            logger.exception("could not read job %s for its subscriptions", job_id)
            return
        async with asyncio.TaskGroup() as sends:
            for each in deliveries:
                sends.create_task(self._send(each))

    def _deliveries(self, job_id: int, status: JobStatus) -> list[Delivery]:
        with self._sessions() as session:
            job = session.get_one(Job, job_id)
            if job.queued_at is None:
                return []
            job_type = self._catalog.find(job.type)
            run_url = f"{self._base_url}/jobs/{job.id}"
            payload = job_event_payload(job, job_type, status, run_url, result_urls(job, status))
            label = f"subscription for job {job.id}"
            return [
                build_subscription_delivery(label, subscription, payload)
                for subscription in session.scalars(select(Subscription))
            ]

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
                delivery.url, json=delivery.payload, headers=delivery.headers
            )
        except httpx.TransportError as error:
            return type(error).__name__
        if response.is_server_error:
            return f"status {response.status_code}"
        if response.is_error:
            logger.warning("%s refused: status %s", delivery.label, response.status_code)
        return None
