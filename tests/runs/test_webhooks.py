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

from conftest import BASE_URL, FETCH_HEADERS, SERVICE_TOKEN, make_job, make_user, session_cookie
from workflows.db import Job, JobStatus, JsonObject, Subscription, utcnow
from workflows.runs.bus import QUEUE_TOPIC, BusEvent
from workflows.runs.jobs import announce
from workflows.runs.webhooks import Webhook, WebhookNotifier, build_delivery
from workflows.state import Services

HOOK_URL = "https://client.example/hook"
HOOK_TOKEN = "hook-secret"
SIGNING_KEY = "a-signing-key-16"
SERVICE_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
SUBMIT = {"type": "song", "params": {"prompt": "cats"}}


def expected_signature(payload: JsonObject) -> str:
    digest = hmac.new(
        SIGNING_KEY.encode(), json.dumps(payload, sort_keys=True).encode(), hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


WEBHOOK: JsonObject = {
    "url": HOOK_URL,
    "token": HOOK_TOKEN,
    "extra_params": {"target": "#music", "status": "spoofed"},
    "message_prefix": "al: ",
}
FILE_URLS = ["https://bucket.example/a.mp3", "https://bucket.example/a.json"]
PLAYER_URL = "https://player.example/?a"
RESULT_URLS = [*FILE_URLS, PLAYER_URL]


@pytest.fixture
async def notifier(services: Services) -> AsyncIterator[WebhookNotifier]:
    async with httpx.AsyncClient() as http:
        yield WebhookNotifier(services.sessions, services.catalog, services.bus, http, BASE_URL)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("workflows.runs.webhooks.RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0))


def hooked_job(session: Session, services: Services, status: JobStatus) -> Job:
    job = make_job(session, services, make_user(session, "alice"))
    job.queued_at = utcnow()
    job.webhook = WEBHOOK
    job.status = status
    job.error = "the executor crashed"
    job.result = {
        "files": [{"url": url, "name": "a", "mime": "audio/mpeg"} for url in FILE_URLS],
        "links": [{"label": "open", "url": PLAYER_URL}],
    }
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
        (JobStatus.SUCCEEDED, "is done: " + " ".join(RESULT_URLS), RESULT_URLS),
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
    user = make_user(session, "carol", paid_credits=10_000)
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


def test_build_delivery_keeps_the_message_on_one_line() -> None:
    hook = Webhook.model_validate({"url": HOOK_URL, "token": HOOK_TOKEN, "message_prefix": "hi:\n"})
    delivery = build_delivery("x", hook, "failed:\r\nQUIT", {})
    assert delivery.payload["message"] == "hi: failed: QUIT"


def test_webhook_token_must_be_printable_ascii() -> None:
    with pytest.raises(ValueError, match="pattern"):
        Webhook(url="https://hook.example", token="café")
