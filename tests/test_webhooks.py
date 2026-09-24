import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import suppress

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from conftest import BASE_URL, SERVICE_TOKEN, make_job, make_user
from workflows.bus import QUEUE_TOPIC, BusEvent
from workflows.db import Job, JobStatus, JsonObject
from workflows.jobs import announce
from workflows.state import Services
from workflows.webhooks import WebhookNotifier

HOOK_URL = "https://client.example/hook"
HOOK_TOKEN = "hook-secret"
SERVICE_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
SUBMIT = {"type": "song", "params": {"prompt": "cats"}}
WEBHOOK: JsonObject = {
    "url": HOOK_URL,
    "token": HOOK_TOKEN,
    "extra_params": {"target": "#music", "status": "spoofed"},
    "message_prefix": "al: ",
}
FILE_URLS = ["https://bucket.example/a.mp3", "https://bucket.example/a.json"]


@pytest.fixture
async def notifier(services: Services) -> AsyncIterator[WebhookNotifier]:
    async with httpx.AsyncClient() as http:
        yield WebhookNotifier(services.sessions, services.registry, services.bus, http, BASE_URL)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("workflows.webhooks.RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0))


def hooked_job(session: Session, services: Services, status: JobStatus) -> Job:
    job = make_job(session, services, make_user(session, "alice"))
    job.webhook = WEBHOOK
    job.status = status
    job.error = "the executor crashed"
    job.result = {"files": [{"url": url, "name": "a", "mime": "audio/mpeg"} for url in FILE_URLS]}
    session.commit()
    return job


def test_submit_job_stores_the_webhook(client: TestClient, session: Session) -> None:
    body = {**SUBMIT, "webhook": {"url": HOOK_URL, "token": HOOK_TOKEN}}

    response = client.post("/api/clients/jobs", json=body, headers=SERVICE_HEADERS)

    assert response.status_code == 201
    job = session.get_one(Job, response.json()["job"]["id"])
    assert job.webhook == {
        "url": HOOK_URL,
        "token": HOOK_TOKEN,
        "extra_params": {},
        "message_prefix": "",
    }
    assert HOOK_TOKEN not in response.text


@pytest.mark.parametrize(
    "webhook",
    [
        {"url": "ftp://client.example/hook", "token": HOOK_TOKEN},
        {"url": HOOK_URL, "token": ""},
        {"url": HOOK_URL, "token": "t" * 501},
        {"url": HOOK_URL, "token": HOOK_TOKEN, "extra_params": {str(n): "v" for n in range(11)}},
        {"url": HOOK_URL, "token": HOOK_TOKEN, "extra_params": {"k" * 201: "v"}},
        {"url": HOOK_URL, "token": HOOK_TOKEN, "extra_params": {"k": "v" * 201}},
        {"url": HOOK_URL, "token": HOOK_TOKEN, "message_prefix": "p" * 101},
    ],
)
def test_submit_job_rejects_invalid_webhooks(client: TestClient, webhook: JsonObject) -> None:
    body = {**SUBMIT, "webhook": webhook}

    response = client.post("/api/clients/jobs", json=body, headers=SERVICE_HEADERS)

    assert response.status_code == 422


@respx.mock
@pytest.mark.parametrize(
    ("status", "text", "result_urls"),
    [
        (JobStatus.QUEUED, "is in line", []),
        (JobStatus.RUNNING, "started: {run_url}", []),
        (JobStatus.SUCCEEDED, "is done: " + " ".join(FILE_URLS), FILE_URLS),
        (JobStatus.FAILED, "failed: the executor crashed ({run_url})", []),
        (JobStatus.CANCELLED, "was cancelled", []),
    ],
)
async def test_notify_posts_the_status_change(
    session: Session,
    services: Services,
    notifier: WebhookNotifier,
    status: JobStatus,
    text: str,
    result_urls: list[str],
) -> None:
    route = respx.post(HOOK_URL).respond(204)
    job = hooked_job(session, services, status)
    run_url = f"{BASE_URL}/jobs/{job.id}"

    await notifier.notify(job.id, status)

    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {HOOK_TOKEN}"
    assert json.loads(request.content) == {
        "target": "#music",
        "message": f"al: your song #{job.id} " + text.format(run_url=run_url),
        "job_id": job.id,
        "type": "song",
        "status": status,
        "run_url": run_url,
        "result_urls": result_urls,
    }


@respx.mock
async def test_notify_retries_server_errors_then_gives_up(
    session: Session,
    services: Services,
    notifier: WebhookNotifier,
    caplog: pytest.LogCaptureFixture,
) -> None:
    route = respx.post(HOOK_URL).mock(
        side_effect=[httpx.ConnectError("down"), httpx.ReadTimeout("slow")]
        + [httpx.Response(503)] * 3
    )
    job = hooked_job(session, services, JobStatus.RUNNING)

    with caplog.at_level(logging.WARNING):
        await notifier.notify(job.id, JobStatus.RUNNING)

    assert route.call_count == 4
    assert f"webhook for job {job.id} failed" in caplog.text
    assert HOOK_TOKEN not in caplog.text


@respx.mock
async def test_notify_stops_after_a_retry_succeeds(
    session: Session, services: Services, notifier: WebhookNotifier
) -> None:
    route = respx.post(HOOK_URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
    job = hooked_job(session, services, JobStatus.RUNNING)

    await notifier.notify(job.id, JobStatus.RUNNING)

    assert route.call_count == 2


@respx.mock
async def test_notify_does_not_retry_client_errors(
    session: Session,
    services: Services,
    notifier: WebhookNotifier,
    caplog: pytest.LogCaptureFixture,
) -> None:
    route = respx.post(HOOK_URL).respond(404)
    job = hooked_job(session, services, JobStatus.RUNNING)

    with caplog.at_level(logging.WARNING):
        await notifier.notify(job.id, JobStatus.RUNNING)

    assert route.call_count == 1
    assert "refused: status 404" in caplog.text


@respx.mock
async def test_notify_skips_jobs_without_a_webhook(
    session: Session, services: Services, notifier: WebhookNotifier
) -> None:
    route = respx.post(HOOK_URL).respond(204)
    job = make_job(session, services, None)

    await notifier.notify(job.id, JobStatus.CANCELLED)

    assert route.call_count == 0


@respx.mock
async def test_run_notifies_status_changes_from_the_bus(
    session: Session, services: Services, notifier: WebhookNotifier
) -> None:
    route = respx.post(HOOK_URL).respond(204)
    job = hooked_job(session, services, JobStatus.AWAITING_CONFIRMATION)
    task = asyncio.create_task(notifier.run())
    await asyncio.sleep(0)

    services.bus.publish(QUEUE_TOPIC, BusEvent("step", {"job_id": job.id}))
    announce(services.bus, job)
    job.status = JobStatus.RUNNING
    session.commit()
    announce(services.bus, job)
    for _ in range(20):
        if route.called:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert [json.loads(call.request.content)["status"] for call in route.calls] == ["running"]
