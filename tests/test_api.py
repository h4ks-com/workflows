from datetime import timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.requests import Request

from conftest import (
    SERVICE_TOKEN,
    SONG_URL,
    FakeProber,
    log_in,
    make_job,
    make_user,
    queue_job,
)
from workflows.auth import require_admin, require_service
from workflows.db import Job, JobStatus, JsonObject
from workflows.jobs import start, succeed
from workflows.state import Services

LONG_SONG_URL = "https://youtube.example/watch?v=long"
SONG = {"type": "song", "params": {"prompt": "a song about cats"}}
SERVICE_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def start_job(session: Session, job: Job) -> str:
    token = start(job)
    session.commit()
    return token


def test_healthz_and_lifespan_run_the_worker(app: FastAPI) -> None:
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_types_list_schema_steps_and_availability(client: TestClient) -> None:
    types = {item["name"]: item for item in client.get("/api/types").json()}

    assert types["parody"]["available"]
    assert not types["voice"]["available"]
    assert types["song"]["params_schema"]["properties"]["seconds"]["maximum"] == 240
    assert [step["name"] for step in types["song"]["steps"]] == ["write", "generate", "store"]


def test_quote_probes_media(client: TestClient) -> None:
    response = client.post("/api/quote", json={"type": "parody", "params": {"url": SONG_URL}})

    assert response.json() == {
        "credits": 240,
        "beans": 3,
        "probe": {"duration_seconds": 100.0, "title": "Some Song"},
        "estimate_seconds": 240,
    }


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"type": "voice", "params": {}}, 409, "voice is not available yet"),
        ({"type": "karaoke", "params": {}}, 404, "no job type named karaoke"),
        ({"type": "parody", "params": {"url": "https://x.example/y"}}, 502, None),
        ({"type": "song", "params": {"prompt": "x", "seconds": 5}}, 422, None),
    ],
)
def test_quote_rejects_bad_requests(
    client: TestClient, body: JsonObject, status: int, detail: str | None
) -> None:
    response = client.post("/api/quote", json=body)

    assert response.status_code == status
    if detail:
        assert response.json()["detail"] == detail


def test_submit_reserves_credits_and_queues(
    client: TestClient, session: Session, services: Services
) -> None:
    user = make_user(session, "alice")
    log_in(client, user)

    response = client.post("/api/jobs", json=SONG)

    assert response.status_code == 201
    job = response.json()
    assert (job["status"], job["owner"], job["quote"], job["position"]) == (
        "queued",
        "alice",
        105,
        1,
    )
    assert job["eta_seconds"] == 105
    session.refresh(user)
    assert user.free_credits == 500 - 105


def test_submit_needs_a_user(client: TestClient) -> None:
    assert client.post("/api/jobs", json=SONG).status_code == 401


def test_submit_fails_without_enough_credits(
    client: TestClient, session: Session, prober: FakeProber
) -> None:
    log_in(client, make_user(session, "alice"))
    prober.durations[LONG_SONG_URL] = 400.0

    response = client.post("/api/jobs", json={"type": "parody", "params": {"url": LONG_SONG_URL}})

    assert response.status_code == 402
    assert response.json()["detail"] == "this needs 600 credits and you have 500"


@pytest.mark.parametrize(("on_behalf_of", "status"), [("alice", 201), (None, 422), ("nobody", 404)])
def test_service_submits_on_behalf_of_a_user(
    client: TestClient, session: Session, on_behalf_of: str | None, status: int
) -> None:
    make_user(session, "alice")

    response = client.post(
        "/api/jobs", json={**SONG, "on_behalf_of": on_behalf_of}, headers=SERVICE_HEADERS
    )

    assert response.status_code == status


def test_queue_orders_jobs_with_etas(
    client: TestClient, session: Session, services: Services
) -> None:
    user = make_user(session, "alice", paid_credits=1000)
    running = queue_job(session, services, user)
    start_job(session, running)
    queued = queue_job(session, services, user)

    queue = client.get("/api/queue").json()

    assert not queue["paused"]
    assert queue["running"]["id"] == running.id
    assert queue["running"]["eta_seconds"] == 60
    assert [(job["id"], job["position"]) for job in queue["queued"]] == [(queued.id, 1)]
    assert (queue["queued"][0]["starts_in_seconds"], queue["queued"][0]["eta_seconds"]) == (60, 120)


def test_list_jobs_filters_by_user(
    client: TestClient, session: Session, services: Services
) -> None:
    alice = make_user(session, "alice")
    bob = make_user(session, "bob")
    queue_job(session, services, alice)
    queue_job(session, services, bob)
    queue_job(session, services, bob)

    assert [job["owner"] for job in client.get("/api/jobs?user=bob&limit=1").json()] == ["bob"]
    assert len(client.get("/api/jobs").json()) == 3


def test_get_job_includes_events(client: TestClient, session: Session, services: Services) -> None:
    job = queue_job(session, services, make_user(session, "alice"))
    token = start_job(session, job)
    headers = {"Authorization": f"Bearer {token}"}
    client.post(
        f"/api/jobs/{job.id}/events", json={"kind": "log", "message": "hi"}, headers=headers
    )

    detail = client.get(f"/api/jobs/{job.id}").json()

    assert detail["events"][0]["kind"] == "log"
    assert detail["events"][0]["data"] == {"message": "hi"}
    assert detail["position"] == 0
    assert client.get("/api/jobs/999").status_code == 404


def test_executor_events_drive_the_job(
    client: TestClient, session: Session, services: Services
) -> None:
    user = make_user(session, "alice")
    job = queue_job(session, services, user)
    headers = {"Authorization": f"Bearer {start_job(session, job)}"}
    url = f"/api/jobs/{job.id}/events"
    result = {"kind": "result", "files": [{"url": "https://s3/x.wav", "name": "x", "mime": "a/w"}]}

    assert client.post(url, json={"kind": "log", "message": "hi"}).status_code == 401
    assert (
        client.post(url, json={"kind": "step", "step": "nope"}, headers=headers).status_code == 422
    )
    step = {"kind": "step", "step": "generate", "done": 1, "total": 2}
    assert client.post(url, json=step, headers=headers).status_code == 204
    assert client.get(url.removesuffix("/events")).json()["progress"]["fraction"] == 0.55
    assert client.post(url, json=result, headers=headers).status_code == 204
    assert client.post(url, json=result, headers=headers).status_code == 409

    view = client.get(url.removesuffix("/events")).json()
    assert view["status"] == "succeeded"
    assert view["progress"]["fraction"] == 1.0
    assert view["result"]["files"][0]["url"] == "https://s3/x.wav"


def test_executor_error_fails_and_refunds(
    client: TestClient, session: Session, services: Services
) -> None:
    user = make_user(session, "alice")
    job = queue_job(session, services, user)
    headers = {"Authorization": f"Bearer {start_job(session, job)}"}

    client.post(
        f"/api/jobs/{job.id}/events", json={"kind": "error", "message": "oom"}, headers=headers
    )

    session.refresh(job)
    session.refresh(user)
    assert (job.status, job.error, user.free_credits) == (JobStatus.FAILED, "oom", 500)


def test_owner_cancels_a_queued_job(
    client: TestClient, session: Session, services: Services
) -> None:
    user = make_user(session, "alice")
    job = queue_job(session, services, user)
    log_in(client, user)

    response = client.post(f"/api/jobs/{job.id}/cancel")

    assert response.json()["status"] == "cancelled"
    assert client.post(f"/api/jobs/{job.id}/cancel").status_code == 409


def test_cancel_permissions(client: TestClient, session: Session, services: Services) -> None:
    owner = make_user(session, "alice")
    job = queue_job(session, services, owner)
    start_job(session, job)

    log_in(client, make_user(session, "bob"))
    assert client.post(f"/api/jobs/{job.id}/cancel").status_code == 403
    log_in(client, owner)
    assert client.post(f"/api/jobs/{job.id}/cancel").status_code == 409
    log_in(client, make_user(session, "root"))
    assert client.post(f"/api/jobs/{job.id}/cancel").json()["status"] == "cancelled"


def test_average_of_recent_successes_drives_etas(
    client: TestClient, session: Session, services: Services
) -> None:
    user = make_user(session, "alice", paid_credits=1000)
    done = queue_job(session, services, user)
    start_job(session, done)
    succeed(session, done, {})
    assert done.started_at is not None
    done.finished_at = done.started_at + timedelta(seconds=90)
    session.commit()
    queue_job(session, services, user)

    assert client.get("/api/queue").json()["queued"][0]["eta_seconds"] == 90


def test_unconfirmed_job_has_no_owner(
    client: TestClient, session: Session, services: Services
) -> None:
    job = make_job(session, services, None)

    view = client.get(f"/api/jobs/{job.id}").json()

    assert (view["status"], view["owner"], view["position"]) == (
        "awaiting_confirmation",
        None,
        None,
    )


async def test_admin_and_service_dependencies(session: Session, services: Services) -> None:
    alice = make_user(session, "alice")
    root = make_user(session, "root")
    service_request = Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {SERVICE_TOKEN}".encode())]}
    )

    assert await require_admin(root, services) is root
    await require_service(service_request, services)
    with pytest.raises(HTTPException):
        await require_admin(alice, services)
    with pytest.raises(HTTPException):
        await require_service(Request({"type": "http", "headers": []}), services)
