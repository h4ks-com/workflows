from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import SERVICE_TOKEN
from conftest import log_in
from conftest import make_user
from workflows.db import ExternalIdentity
from workflows.db import Job
from workflows.db import JobStatus
from workflows.db import LinkRequest
from workflows.db import utcnow
from workflows.runs.jobs import hash_token
from workflows.state import Services

SERVICE_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
SUBMIT = {"type": "song", "params": {"prompt": "cats"}}


def test_create_link_returns_a_one_time_link_url(client: TestClient, session: Session) -> None:
    response = client.post(
        "/api/clients/links", json={"identity": "irc:al"}, headers=SERVICE_HEADERS
    )

    assert response.status_code == 200
    assert "/link/" in response.json()["link_url"]
    stored = session.scalar(select(LinkRequest))
    assert stored is not None
    assert stored.identity == "irc:al"


def test_create_link_rejects_invalid_identity(client: TestClient) -> None:
    response = client.post(
        "/api/clients/links", json={"identity": "has a space"}, headers=SERVICE_HEADERS
    )

    assert response.status_code == 422


def test_get_identity_requires_the_service_token(client: TestClient) -> None:
    assert client.get("/api/clients/identities/irc:al").status_code == 401


def test_get_identity(client: TestClient, session: Session) -> None:
    response = client.get("/api/clients/identities/nobody", headers=SERVICE_HEADERS)
    assert response.status_code == 404

    user = make_user(session, "alice", paid_credits=20)
    session.add(ExternalIdentity(identity="irc:al", user_id=user.id))
    session.commit()

    response = client.get("/api/clients/identities/irc:al", headers=SERVICE_HEADERS)

    assert response.json() == {"username": "alice", "free_credits": 500, "paid_credits": 20}


def test_link_page_redirects_anonymous_users(client: TestClient) -> None:
    response = client.get("/link/whatever", follow_redirects=False)

    assert response.headers["location"] == "/login?next=/link/whatever"


def test_link_flow(client: TestClient, session: Session) -> None:
    user = make_user(session, "alice")
    link_response = client.post(
        "/api/clients/links", json={"identity": "irc:bob"}, headers=SERVICE_HEADERS
    )
    token = link_response.json()["link_url"].rsplit("/", 1)[-1]
    log_in(client, user)

    page = client.get(f"/link/{token}")
    assert page.status_code == 200
    csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]

    response = client.post(f"/link/{token}", data={"csrf_token": csrf}, follow_redirects=False)

    assert response.headers["location"] == "/wallet"
    link = session.scalar(select(ExternalIdentity).where(ExternalIdentity.identity == "irc:bob"))
    assert link is not None
    assert link.user_id == user.id
    assert client.post(f"/link/{token}", data={"csrf_token": csrf}).status_code == 404


def test_link_rejects_expired_tokens(client: TestClient, session: Session) -> None:
    session.add(
        LinkRequest(
            identity="irc:old",
            token_hash=hash_token("expired-token"),
            expires_at=utcnow() - timedelta(hours=2),
        )
    )
    session.commit()
    log_in(client, make_user(session, "alice"))

    assert client.get("/link/expired-token").status_code == 404


async def test_expired_awaiting_confirmation_jobs_are_cancelled(
    session: Session, services: Services
) -> None:
    stale_job = Job(
        type="song",
        params={"prompt": "cats"},
        status=JobStatus.AWAITING_CONFIRMATION,
        quote=100,
        estimate_seconds=100,
        created_at=utcnow() - timedelta(hours=2),
    )
    session.add(stale_job)
    session.commit()

    await services.worker.tick()

    session.expire_all()
    assert stale_job.status == JobStatus.CANCELLED


def test_link_refuses_an_identity_linked_elsewhere(client: TestClient, session: Session) -> None:
    owner = make_user(session, "alice")
    session.add(ExternalIdentity(identity="irc:al", user_id=owner.id))
    session.commit()
    link_url = client.post(
        "/api/clients/links", json={"identity": "irc:al"}, headers=SERVICE_HEADERS
    ).json()["link_url"]
    token = link_url.rsplit("/", 1)[-1]
    log_in(client, make_user(session, "mallory"))
    csrf = client.get(f"/link/{token}").text.split('name="csrf_token" value="')[1].split('"')[0]

    assert client.post(f"/link/{token}", data={"csrf_token": csrf}).status_code == 409
