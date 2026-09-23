import os
import time
import uuid
from collections.abc import Iterator
from typing import cast

import httpx
import pytest

type JsonObject = dict[str, object]

E2E_BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8000")
JOB_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.2
FREE_DAILY_CREDITS = 500
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
HTTP_OK = 200
HTTP_UNAUTHORIZED = 401

pytestmark = pytest.mark.e2e


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=E2E_BASE_URL,
        timeout=10.0,
        follow_redirects=True,
        headers={"X-Requested-With": "fetch"},
    ) as client:
        yield client


def dev_login(client: httpx.Client) -> str:
    username = f"e2e-{uuid.uuid4().hex[:8]}"
    response = client.get("/login", params={"as": username}, follow_redirects=False)
    assert response.is_redirect
    return username


def song_params(prompt: str, seconds: int = 60) -> dict[str, object]:
    return {"prompt": prompt, "seconds": seconds, "model": "ace-step"}


def song_quote_credits(seconds: int = 60) -> int:
    return round(0.3 * seconds + 60)


def submit_song(client: httpx.Client, prompt: str, seconds: int = 60) -> JsonObject:
    response = client.post(
        "/api/jobs", json={"type": "song", "params": song_params(prompt, seconds)}
    )
    response.raise_for_status()
    return cast(JsonObject, response.json())


def poll_job(client: httpx.Client, job_id: int, timeout: float = JOB_TIMEOUT_SECONDS) -> JsonObject:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        response.raise_for_status()
        job = cast(JsonObject, response.json())
        if job["status"] in TERMINAL_STATUSES:
            return job
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"job {job_id} did not reach a terminal status within {timeout}s")


def balance(client: httpx.Client) -> int:
    response = client.get("/api/me")
    response.raise_for_status()
    body = response.json()
    return int(body["free_credits"]) + int(body["paid_credits"])


def test_healthz(client: httpx.Client) -> None:
    response = client.get("/healthz")
    assert response.status_code == HTTP_OK
    assert response.json() == {"status": "ok"}


def test_types(client: httpx.Client) -> None:
    response = client.get("/api/types")
    assert response.status_code == HTTP_OK
    names = {job_type["name"] for job_type in response.json()}
    assert {"parody", "song", "voice", "podcast"} <= names


def test_dev_login_grants_free_credits(client: httpx.Client) -> None:
    dev_login(client)
    response = client.get("/api/me")
    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["free_credits"] == FREE_DAILY_CREDITS
    assert body["paid_credits"] == 0


def test_quote_song(client: httpx.Client) -> None:
    dev_login(client)
    response = client.post(
        "/api/quote", json={"type": "song", "params": song_params("a song about cats")}
    )
    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["credits"] == song_quote_credits()


def test_job_succeeds_and_captures_credits(client: httpx.Client) -> None:
    dev_login(client)
    before = balance(client)
    job = submit_song(client, "a song about cats")
    assert job["status"] == "queued"
    finished = poll_job(client, int(job["id"]))
    assert finished["status"] == "succeeded"
    assert finished["result"]["files"]
    assert balance(client) == before - song_quote_credits()


def test_job_fails_and_refunds_credits(client: httpx.Client) -> None:
    dev_login(client)
    before = balance(client)
    job = submit_song(client, "FAIL this song")
    finished = poll_job(client, int(job["id"]))
    assert finished["status"] == "failed"
    assert finished["error"]
    assert balance(client) == before


def test_only_one_job_runs_at_a_time(client: httpx.Client) -> None:
    dev_login(client)
    first = submit_song(client, "song one")
    second = submit_song(client, "song two")
    both_running_at_once = False
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status_first = client.get(f"/api/jobs/{first['id']}").json()["status"]
        status_second = client.get(f"/api/jobs/{second['id']}").json()["status"]
        if status_first == "running" and status_second == "running":
            both_running_at_once = True
        if status_first in TERMINAL_STATUSES and status_second in TERMINAL_STATUSES:
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    else:
        raise TimeoutError("jobs did not both finish in time")
    assert not both_running_at_once


def test_executor_callback_rejects_wrong_token(client: httpx.Client) -> None:
    dev_login(client)
    job = submit_song(client, "song for a bad callback")
    response = client.post(
        f"/api/jobs/{job['id']}/events",
        json={"kind": "log", "message": "hello"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == HTTP_UNAUTHORIZED
