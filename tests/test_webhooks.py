import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import suppress

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import BASE_URL, FETCH_HEADERS, SERVICE_TOKEN, make_job, make_user, session_cookie
from workflows.bus import QUEUE_TOPIC, BusEvent
from workflows.db import Job, JobStatus, JsonObject, Subscription, utcnow
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
    job.queued_at = utcnow()
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


SUB_URLS = ["https://one.example/hook", "https://two.example/hook"]


def subscription(url: str, prefix: str = "") -> JsonObject:
    return {
        "url": url,
        "token": HOOK_TOKEN,
        "extra_params": {"target": "#all"},
        "message_prefix": prefix,
    }


def subscribe(client: TestClient, url: str, prefix: str = "") -> None:
    response = client.put(
        "/api/clients/subscription", json=subscription(url, prefix), headers=SERVICE_HEADERS
    )
    assert response.status_code == 200


def test_subscription_endpoints_need_the_service_token(client: TestClient) -> None:
    put = client.put("/api/clients/subscription", json={"url": HOOK_URL, "token": HOOK_TOKEN})
    delete = client.delete("/api/clients/subscription", params={"url": HOOK_URL})

    assert (put.status_code, delete.status_code) == (401, 401)


def test_put_subscription_upserts_by_url(client: TestClient, session: Session) -> None:
    subscribe(client, HOOK_URL, "a: ")
    second = client.put(
        "/api/clients/subscription", json=subscription(HOOK_URL, "b: "), headers=SERVICE_HEADERS
    )

    assert second.status_code == 200
    assert second.json() == {
        "url": HOOK_URL,
        "extra_params": {"target": "#all"},
        "message_prefix": "b: ",
    }
    assert HOOK_TOKEN not in second.text
    stored = session.scalars(select(Subscription)).one()
    assert (stored.url, stored.token, stored.message_prefix) == (HOOK_URL, HOOK_TOKEN, "b: ")


def test_delete_subscription(client: TestClient, session: Session) -> None:
    subscribe(client, HOOK_URL)
    params = {"url": HOOK_URL}

    deleted = client.delete("/api/clients/subscription", params=params, headers=SERVICE_HEADERS)
    missing = client.delete("/api/clients/subscription", params=params, headers=SERVICE_HEADERS)

    assert (deleted.status_code, missing.status_code) == (204, 404)
    assert session.scalars(select(Subscription)).all() == []


@respx.mock
@pytest.mark.parametrize(
    ("status", "text"),
    [
        (JobStatus.QUEUED, "mattf submitted a song, #{id}: {run_url}"),
        (JobStatus.RUNNING, "mattf's song #{id} started"),
        (JobStatus.SUCCEEDED, "mattf's song #{id} is done: " + " ".join(FILE_URLS)),
        (JobStatus.FAILED, "mattf's song #{id} failed: the executor crashed"),
        (JobStatus.CANCELLED, "mattf's song #{id} was cancelled"),
    ],
)
async def test_notify_posts_every_status_to_every_subscription(
    client: TestClient,
    session: Session,
    services: Services,
    notifier: WebhookNotifier,
    status: JobStatus,
    text: str,
) -> None:
    routes = [respx.post(url).respond(204) for url in SUB_URLS]
    for url in SUB_URLS:
        subscribe(client, url, "hi: ")
    job = make_job(session, services, make_user(session, "mattf"))
    job.queued_at = utcnow()
    job.status = status
    job.error = "the executor crashed"
    job.result = {"files": [{"url": url, "name": "a", "mime": "audio/mpeg"} for url in FILE_URLS]}
    session.commit()
    run_url = f"{BASE_URL}/jobs/{job.id}"

    await notifier.notify(job.id, status)

    for route in routes:
        request = route.calls.last.request
        assert request.headers["Authorization"] == f"Bearer {HOOK_TOKEN}"
        assert json.loads(request.content) == {
            "target": "#all",
            "message": "hi: " + text.format(id=job.id, run_url=run_url),
            "job_id": job.id,
            "type": "song",
            "status": status,
            "run_url": run_url,
            "result_urls": FILE_URLS if status == JobStatus.SUCCEEDED else [],
        }


@respx.mock
async def test_notify_names_someone_for_jobs_without_an_owner(
    client: TestClient, session: Session, services: Services, notifier: WebhookNotifier
) -> None:
    route = respx.post(HOOK_URL).respond(204)
    subscribe(client, HOOK_URL)
    job = make_job(session, services, None)
    job.queued_at = utcnow()
    session.commit()

    await notifier.notify(job.id, JobStatus.CANCELLED)

    assert json.loads(route.calls.last.request.content)["message"] == (
        f"someone's song #{job.id} was cancelled"
    )


@respx.mock
async def test_notify_sends_both_the_webhook_and_subscriptions(
    client: TestClient, session: Session, services: Services, notifier: WebhookNotifier
) -> None:
    hook_route = respx.post(HOOK_URL).respond(204)
    sub_route = respx.post(SUB_URLS[0]).respond(204)
    subscribe(client, SUB_URLS[0])
    job = hooked_job(session, services, JobStatus.RUNNING)

    await notifier.notify(job.id, JobStatus.RUNNING)
    await notifier.notify(job.id, JobStatus.QUEUED)

    assert json.loads(hook_route.calls.last.request.content)["message"] == (
        f"al: your song #{job.id} started: {BASE_URL}/jobs/{job.id}"
    )
    assert hook_route.call_count == 1
    assert sub_route.call_count == 2


@respx.mock
async def test_web_submission_notifies_subscriptions_that_it_was_queued(
    app: FastAPI, client: TestClient, session: Session, notifier: WebhookNotifier
) -> None:
    route = respx.post(HOOK_URL).respond(204)
    subscribe(client, HOOK_URL)
    user = make_user(session, "mattf", paid_credits=10_000)
    task = asyncio.create_task(notifier.run())
    await asyncio.sleep(0)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers=FETCH_HEADERS,
        cookies={"session": session_cookie(user)},
    ) as web:
        response = await web.post("/api/jobs", json=SUBMIT)
    for _ in range(20):
        if route.called:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    job_id = response.json()["id"]
    assert json.loads(route.calls.last.request.content)["message"] == (
        f"mattf submitted a song, #{job_id}: {BASE_URL}/jobs/{job_id}"
    )


@respx.mock
async def test_notify_skips_subscriptions_for_jobs_never_paid(
    client: TestClient, session: Session, services: Services, notifier: WebhookNotifier
) -> None:
    route = respx.post(HOOK_URL).respond(204)
    subscribe(client, HOOK_URL)
    job = make_job(session, services, None)
    job.status = JobStatus.CANCELLED
    session.commit()

    await notifier.notify(job.id, JobStatus.CANCELLED)

    assert not route.called
