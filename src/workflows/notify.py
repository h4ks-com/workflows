import logging

import httpx

from workflows.bus import QUEUE_TOPIC, BusEvent
from workflows.db import Job, JobStatus
from workflows.settings import Settings
from workflows.state import Services

logger = logging.getLogger(__name__)

SEND_MESSAGE_PATH = "/send_message"
SEND_TIMEOUT_SECONDS = 10.0
ANNOUNCED_STATUSES = frozenset({JobStatus.RUNNING, JobStatus.SUCCEEDED, JobStatus.FAILED})
VERBS = {
    JobStatus.RUNNING: "started",
    JobStatus.SUCCEEDED: "finished",
    JobStatus.FAILED: "failed",
}


async def notify_channels(services: Services) -> None:
    with services.bus.subscribe(QUEUE_TOPIC) as queue:
        while True:
            event = await queue.get()
            await _handle(services, event)


async def _handle(services: Services, event: BusEvent) -> None:
    status = event.data.get("status")
    job_id = event.data.get("job_id")
    if status not in ANNOUNCED_STATUSES or not isinstance(job_id, int):
        return
    with services.sessions() as session:
        job = session.get(Job, job_id)
        if job is None or not job.channel:
            return
        message = _message(services.settings, job, JobStatus(status))
    await _send(services.http, services.settings, job.channel, message)


def _message(settings: Settings, job: Job, status: JobStatus) -> str:
    nick = job.nick or "there"
    verb = VERBS[status]
    return f"{nick}: your {job.type} job {verb} - {settings.base_url}/jobs/{job.id}"


async def _send(http: httpx.AsyncClient, settings: Settings, channel: str, message: str) -> None:
    try:
        response = await http.post(
            f"{settings.cloudbot_url}{SEND_MESSAGE_PATH}",
            json={"target": channel, "message": message},
            headers={"Authorization": f"Bearer {settings.cloudbot_token}"},
            timeout=SEND_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        logger.warning("cloudbot notify failed: %s", error)
