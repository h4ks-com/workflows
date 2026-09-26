import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import suppress

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import BASE_URL
from conftest import FETCH_HEADERS
from conftest import SERVICE_TOKEN
from conftest import csrf_from
from conftest import make_job
from conftest import make_user
from conftest import session_cookie
from workflows.db import Job
from workflows.db import JobStatus
from workflows.db import JsonObject
from workflows.db import Subscription
from workflows.db import utcnow
from workflows.runs.bus import QUEUE_TOPIC
from workflows.runs.bus import BusEvent
from workflows.runs.jobs import announce
from workflows.runs.webhooks import WebhookNotifier
from workflows.state import Services

HOOK_URL = "https://client.example/hook"
SIGNING_KEY = "a-signing-key-16"
SERVICE_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def expected_signature(payload: JsonObject) -> str:
    digest = hmac.new(
        SIGNING_KEY.encode(), json.dumps(payload, sort_keys=True).encode(), hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


FILE_URLS = ["https://bucket.example/a.mp3", "https://bucket.example/a.json"]


@pytest.fixture
async def notifier(services: Services) -> AsyncIterator[WebhookNotifier]:
    async with httpx.AsyncClient() as http:
        yield WebhookNotifier(services.sessions, services.catalog, services.bus, http, BASE_URL)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("workflows.runs.webhooks.RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0))


SUB_URLS = ["https://one.example/hook", "https://two.example/hook"]


def subscription(url: str) -> JsonObject:
    return {"url": url, "signing_key": SIGNING_KEY}


def subscribe(client: TestClient, url: str) -> None:
    response = client.put(
        "/api/clients/subscription", json=subscription(url), headers=SERVICE_HEADERS
    )
    assert response.status_code == 200


def test_subscription_endpoints_need_the_service_token(client: TestClient) -> None:
    put = client.put("/api/clients/subscription", json=subscription(HOOK_URL))
    delete = client.delete("/api/clients/subscription", params={"url": HOOK_URL})

    assert (put.status_code, delete.status_code) == (401, 401)


def test_put_subscription_upserts_by_url(client: TestClient, session: Session) -> None:
    subscribe(client, HOOK_URL)
    second = client.put(
        "/api/clients/subscription",
        json={"url": HOOK_URL, "signing_key": "a-different-key-16"},
        headers=SERVICE_HEADERS,
    )

    assert second.status_code == 200
    assert second.json() == {"url": HOOK_URL}
    assert "a-different-key-16" not in second.text
    stored = session.scalars(select(Subscription)).one()
    assert (stored.url, stored.signing_key) == (HOOK_URL, "a-different-key-16")


def test_put_subscription_rejects_a_short_signing_key(client: TestClient) -> None:
    response = client.put(
        "/api/clients/subscription",
        json={"url": HOOK_URL, "signing_key": "short"},
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 422


def test_delete_subscription(client: TestClient, session: Session) -> None:
    subscribe(client, HOOK_URL)
    params = {"url": HOOK_URL}

    deleted = client.delete("/api/clients/subscription", params=params, headers=SERVICE_HEADERS)
    missing = client.delete("/api/clients/subscription", params=params, headers=SERVICE_HEADERS)

    assert (deleted.status_code, missing.status_code) == (204, 404)
    assert session.scalars(select(Subscription)).all() == []


@respx.mock
@pytest.mark.parametrize(
    "status",
    [
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    ],
)
async def test_notify_posts_every_status_to_every_subscription(
    client: TestClient,
    session: Session,
    services: Services,
    notifier: WebhookNotifier,
    status: JobStatus,
) -> None:
    routes = [respx.post(url).respond(204) for url in SUB_URLS]
    for url in SUB_URLS:
        subscribe(client, url)
    job = make_job(session, services, make_user(session, "carol"))
    job.queued_at = utcnow()
    job.status = status
    job.error = "the executor crashed"
    job.result = {
        "files": [{"url": url, "name": "a", "mime": "audio/mpeg"} for url in FILE_URLS],
        "title": "Cat Song",
    }
    session.commit()
    run_url = f"{BASE_URL}/jobs/{job.id}"
    result_urls: list[JsonValue] = list(FILE_URLS) if status == JobStatus.SUCCEEDED else []
    expected_payload: JsonObject = {
        "event": "job",
        "job_id": job.id,
        "type": "song",
        "type_title": "Song",
        "status": status,
        "owner": "carol",
        "title": "Cat Song" if status == JobStatus.SUCCEEDED else None,
        "run_url": run_url,
        "result_urls": result_urls,
        "error": "the executor crashed" if status == JobStatus.FAILED else None,
    }

    await notifier.notify(job.id, status)

    for route in routes:
        request = route.calls.last.request
        assert json.loads(request.content) == expected_payload
        assert request.headers["X-Webhook-Signature"] == expected_signature(expected_payload)


@respx.mock
async def test_notify_names_no_owner_as_null_for_subscriptions(
    client: TestClient, session: Session, services: Services, notifier: WebhookNotifier
) -> None:
    route = respx.post(HOOK_URL).respond(204)
    subscribe(client, HOOK_URL)
    job = make_job(session, services, None)
    job.queued_at = utcnow()
    session.commit()

    await notifier.notify(job.id, JobStatus.CANCELLED)

    assert json.loads(route.calls.last.request.content)["owner"] is None


@respx.mock
async def test_web_submission_notifies_subscriptions_that_it_was_queued(
    app: FastAPI, client: TestClient, session: Session, notifier: WebhookNotifier
) -> None:
    route = respx.post(HOOK_URL).respond(204)
    subscribe(client, HOOK_URL)
    user = make_user(session, "carol", paid_credits=10_000)
    task = asyncio.create_task(notifier.run())
    await asyncio.sleep(0)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers=FETCH_HEADERS,
        cookies={"session": session_cookie(user)},
    ) as web:
        page = await web.get("/submit/song")
        form = {"csrf_token": csrf_from(page.text), "prompt": "cats", "seconds": "150"}
        response = await web.post("/submit/song", data=form, follow_redirects=False)
    for _ in range(20):
        if route.called:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    job_id = int(response.headers["location"].removeprefix("/jobs/"))
    payload = json.loads(route.calls.last.request.content)
    assert (payload["job_id"], payload["status"], payload["owner"]) == (job_id, "queued", "carol")


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


def queued_job(session: Session, services: Services) -> Job:
    job = make_job(session, services, make_user(session, "alice"))
    job.queued_at = utcnow()
    session.commit()
    return job


@respx.mock
async def test_notify_retries_server_errors_then_gives_up(
    client: TestClient,
    session: Session,
    services: Services,
    notifier: WebhookNotifier,
    caplog: pytest.LogCaptureFixture,
) -> None:
    route = respx.post(HOOK_URL).mock(
        side_effect=[httpx.ConnectError("down"), httpx.ReadTimeout("slow")]
        + [httpx.Response(503)] * 3
    )
    subscribe(client, HOOK_URL)
    job = queued_job(session, services)

    with caplog.at_level(logging.WARNING):
        await notifier.notify(job.id, JobStatus.RUNNING)

    assert route.call_count == 4
    assert f"subscription for job {job.id} failed" in caplog.text
    assert SIGNING_KEY not in caplog.text


@respx.mock
async def test_notify_stops_after_a_retry_succeeds(
    client: TestClient, session: Session, services: Services, notifier: WebhookNotifier
) -> None:
    route = respx.post(HOOK_URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
    subscribe(client, HOOK_URL)
    job = queued_job(session, services)

    await notifier.notify(job.id, JobStatus.RUNNING)

    assert route.call_count == 2


@respx.mock
async def test_notify_does_not_retry_client_errors(
    client: TestClient,
    session: Session,
    services: Services,
    notifier: WebhookNotifier,
    caplog: pytest.LogCaptureFixture,
) -> None:
    route = respx.post(HOOK_URL).respond(404)
    subscribe(client, HOOK_URL)
    job = queued_job(session, services)

    with caplog.at_level(logging.WARNING):
        await notifier.notify(job.id, JobStatus.RUNNING)

    assert route.call_count == 1
    assert "refused: status 404" in caplog.text


@respx.mock
async def test_run_notifies_status_changes_from_the_bus(
    client: TestClient, session: Session, services: Services, notifier: WebhookNotifier
) -> None:
    route = respx.post(HOOK_URL).respond(204)
    subscribe(client, HOOK_URL)
    job = queued_job(session, services)
    job.status = JobStatus.AWAITING_CONFIRMATION
    session.commit()
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
