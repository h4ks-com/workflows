from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from conftest import log_in, make_user, queue_job
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
    }
    assert response["worker_paused"] is False
    assert response["beans_poller_last_success"] is None
