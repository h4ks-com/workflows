from dataclasses import replace

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from conftest import make_user, queue_job
from workflows.bus import BusEvent
from workflows.db import JobStatus
from workflows.notify import _handle, _message
from workflows.state import Services

CLOUDBOT_SEND_URL = "https://cloudbot.example/send_message"


@pytest.fixture
def services(services: Services) -> Services:
    settings = replace(
        services.settings, cloudbot_url="https://cloudbot.example", cloudbot_token="cloudbot-token"
    )
    return replace(services, settings=settings)


def test_message_names_the_nick_and_the_run_page(session: Session, services: Services) -> None:
    job = queue_job(session, services, make_user(session, "alice"))
    job.nick = "al_irc"

    message = _message(services.settings, job, JobStatus.SUCCEEDED)

    assert message == f"al_irc: your song job finished - {services.settings.base_url}/jobs/{job.id}"


@respx.mock
async def test_handle_announces_channel_jobs(session: Session, services: Services) -> None:
    route = respx.post(CLOUDBOT_SEND_URL).respond(json={"status": "sent"})
    job = queue_job(session, services, make_user(session, "alice"))
    job.channel = "#h4ks"
    job.nick = "al_irc"
    session.commit()

    await _handle(services, BusEvent("status", {"job_id": job.id, "status": JobStatus.RUNNING}))

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer cloudbot-token"


async def test_handle_skips_jobs_without_a_channel(session: Session, services: Services) -> None:
    job = queue_job(session, services, make_user(session, "alice"))

    await _handle(services, BusEvent("status", {"job_id": job.id, "status": JobStatus.RUNNING}))


async def test_handle_skips_unannounced_statuses_and_missing_jobs(services: Services) -> None:
    await _handle(services, BusEvent("status", {"job_id": 999, "status": JobStatus.QUEUED}))
    await _handle(services, BusEvent("status", {"job_id": "not-an-id", "status": "running"}))


@respx.mock
async def test_handle_swallows_cloudbot_errors(session: Session, services: Services) -> None:
    respx.post(CLOUDBOT_SEND_URL).mock(return_value=httpx.Response(500))
    job = queue_job(session, services, make_user(session, "alice"))
    job.channel = "#h4ks"
    session.commit()

    await _handle(services, BusEvent("status", {"job_id": job.id, "status": JobStatus.FAILED}))
