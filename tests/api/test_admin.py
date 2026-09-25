from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from conftest import MINIO_ENDPOINT, FakeStorage, log_in, make_user, queue_job, with_storage
from workflows.db import JobEvent, JsonObject
from workflows.runs.jobs import start, succeed
from workflows.state import Services


def test_admin_endpoints_require_admin(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice"))

    assert client.post("/api/admin/queue/pause").status_code == 403


def test_admin_cancels_any_job_and_refunds(
    client: TestClient, session: Session, services: Services
) -> None:
    owner = make_user(session, "alice")
    job = queue_job(session, services, owner)
    log_in(client, make_user(session, "root"))

    response = client.post(f"/api/admin/jobs/{job.id}/cancel")

    assert response.json()["status"] == "cancelled"
    session.refresh(owner)
    assert owner.free_credits == 500


def test_admin_adjusts_credits(client: TestClient, session: Session) -> None:
    make_user(session, "alice", paid_credits=10)
    log_in(client, make_user(session, "root"))

    response = client.post("/api/admin/users/alice/credits", json={"credits": 50, "note": "gift"})

    assert response.json() == {"username": "alice", "free_credits": 0, "paid_credits": 60}


def test_admin_adjust_unknown_user(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "root"))

    response = client.post("/api/admin/users/nobody/credits", json={"credits": 1, "note": "x"})

    assert response.status_code == 404


def test_admin_pauses_and_resumes_the_queue(
    client: TestClient, session: Session, services: Services
) -> None:
    log_in(client, make_user(session, "root"))

    assert client.post("/api/admin/queue/pause").json() == {"paused": True}
    assert services.worker.paused is True
    assert client.post("/api/admin/queue/resume").json() == {"paused": False}
    assert services.worker.paused is False


def test_admin_health_reports_executors_and_poller(
    client: TestClient, session: Session, services: Services
) -> None:
    log_in(client, make_user(session, "root"))

    response = client.get("/api/admin/health").json()

    assert response["executors"] == {
        "parody": True,
        "song": True,
        "voice": False,
        "podcast": False,
        "image": False,
    }
    assert response["worker_paused"] is False
    assert response["beans_poller_last_success"] is None


def test_admin_remove_job_files_without_storage_is_409(
    client: TestClient, session: Session, services: Services
) -> None:
    job = queue_job(session, services, make_user(session, "alice"))
    start(job)
    succeed(session, job, {"files": [{"url": "x", "name": "x", "mime": "audio/mpeg"}]})
    session.commit()
    log_in(client, make_user(session, "root"))

    response = client.post(f"/api/admin/jobs/{job.id}/remove")

    assert response.status_code == 409
    assert response.json()["detail"] == "storage is not configured"


def test_admin_removes_job_files(
    client: TestClient, session: Session, services: Services, app: FastAPI
) -> None:
    storage = FakeStorage()
    with_storage(app, services, storage)
    job = queue_job(session, services, make_user(session, "alice"))
    start(job)
    succeed(
        session,
        job,
        {
            "files": [
                {
                    "url": f"https://{MINIO_ENDPOINT}/workflows/song.mp3",
                    "name": "song.mp3",
                    "mime": "audio/mpeg",
                }
            ],
            "title": "My Song",
            "metadata_url": f"https://{MINIO_ENDPOINT}/workflows/song.json",
        },
    )
    session.commit()
    log_in(client, make_user(session, "root"))

    response = client.post(f"/api/admin/jobs/{job.id}/remove")

    assert response.status_code == 200
    body = response.json()
    assert body["result"] is None
    assert body["removed_at"] is not None
    assert set(storage.removed) == {("workflows", "song.mp3"), ("workflows", "song.json")}
    session.refresh(job)
    assert job.result is None
    assert job.removed_at is not None
    assert job.events[-1].kind == "log"
    assert job.events[-1].data["message"] == "removed by an admin"


def test_admin_removal_hides_the_result_event(
    client: TestClient, session: Session, services: Services, app: FastAPI
) -> None:
    with_storage(app, services, FakeStorage())
    job = queue_job(session, services, make_user(session, "alice"))
    start(job)
    result: JsonObject = {
        "files": [
            {"url": f"https://{MINIO_ENDPOINT}/workflows/a.png", "name": "a", "mime": "image/png"}
        ],
        "title": "secret",
    }
    session.add(JobEvent(job_id=job.id, kind="result", data=result))
    succeed(session, job, result)
    session.commit()
    log_in(client, make_user(session, "root"))

    client.post(f"/api/admin/jobs/{job.id}/remove")

    events = client.get(f"/api/jobs/{job.id}").json()["events"]
    assert "secret" not in str(events)


def test_admin_remove_job_files_skips_unmatched_urls(
    client: TestClient, session: Session, services: Services, app: FastAPI
) -> None:
    storage = FakeStorage()
    with_storage(app, services, storage)
    job = queue_job(session, services, make_user(session, "alice"))
    start(job)
    succeed(
        session,
        job,
        {"files": [{"url": "https://other.example/bucket/song.mp3", "name": "x", "mime": "x"}]},
    )
    session.commit()
    log_in(client, make_user(session, "root"))

    response = client.post(f"/api/admin/jobs/{job.id}/remove")

    assert response.status_code == 200
    assert storage.removed == []


def test_admin_remove_job_files_missing_job_is_404(
    client: TestClient, session: Session, services: Services, app: FastAPI
) -> None:
    with_storage(app, services, FakeStorage())
    log_in(client, make_user(session, "root"))

    assert client.post("/api/admin/jobs/999999/remove").status_code == 404
