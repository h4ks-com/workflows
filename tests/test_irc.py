from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import SERVICE_TOKEN, FakeProber, log_in, make_user
from workflows.db import IrcLink, Job, JobStatus, LinkRequest, utcnow
from workflows.jobs import hash_token
from workflows.state import Services

SERVICE_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
SUBMIT = {"nick": "al_irc", "channel": "#h4ks", "type": "song", "params": {"prompt": "cats"}}


def test_irc_submit_needs_the_service_token(client: TestClient) -> None:
    assert client.post("/api/irc/submit", json=SUBMIT).status_code == 401


def test_irc_submit_unlinked_account_needs_confirmation(
    client: TestClient, session: Session
) -> None:
    body = {**SUBMIT, "irc_account": "al_irc_acct"}

    response = client.post("/api/irc/submit", json=body, headers=SERVICE_HEADERS)

    assert response.status_code == 201
    payload = response.json()
    assert payload["job"]["status"] == "awaiting_confirmation"
    assert "/confirm/" in payload["confirm_url"]


def test_irc_submit_linked_account_enqueues_immediately(
    client: TestClient, session: Session
) -> None:
    user = make_user(session, "alice")
    session.add(IrcLink(irc_account="al_irc_acct", user_id=user.id))
    session.commit()
    body = {**SUBMIT, "irc_account": "al_irc_acct"}

    response = client.post("/api/irc/submit", json=body, headers=SERVICE_HEADERS)

    assert response.status_code == 201
    payload = response.json()
    assert payload["job"]["status"] == "queued"
    assert payload["job"]["owner"] == "alice"
    assert payload["confirm_url"] is None


def test_irc_submit_linked_account_insufficient_credits(
    client: TestClient, session: Session, prober: FakeProber
) -> None:
    user = make_user(session, "alice")
    session.add(IrcLink(irc_account="al_irc_acct", user_id=user.id))
    session.commit()
    long_url = "https://youtube.example/watch?v=long"
    prober.durations[long_url] = 400.0
    body = {
        **SUBMIT,
        "irc_account": "al_irc_acct",
        "type": "parody",
        "params": {"url": long_url},
    }

    response = client.post("/api/irc/submit", json=body, headers=SERVICE_HEADERS)

    assert response.status_code == 402
    assert "top up at" in response.json()["detail"]


def test_irc_link_returns_a_one_time_link_url(client: TestClient, session: Session) -> None:
    response = client.post(
        "/api/irc/link",
        json={"irc_account": "al_irc_acct", "nick": "al_irc"},
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 200
    assert "/link/" in response.json()["link_url"]
    stored = session.scalar(select(LinkRequest))
    assert stored is not None
    assert stored.irc_account == "al_irc_acct"


def test_irc_whois(client: TestClient, session: Session) -> None:
    assert client.get("/api/irc/whois/nobody", headers=SERVICE_HEADERS).status_code == 404

    user = make_user(session, "alice", paid_credits=20)
    session.add(IrcLink(irc_account="al_irc_acct", user_id=user.id))
    session.commit()

    response = client.get("/api/irc/whois/al_irc_acct", headers=SERVICE_HEADERS)

    assert response.json() == {"username": "alice", "free_credits": 0, "paid_credits": 20}


def test_confirm_page_redirects_anonymous_users_to_login(client: TestClient) -> None:
    response = client.get("/confirm/whatever", follow_redirects=False)

    assert response.headers["location"] == "/login?next=/confirm/whatever"


def test_confirm_page_rejects_unknown_tokens(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice"))

    assert client.get("/confirm/nope").status_code == 404


def test_confirm_flow_enqueues_and_links(client: TestClient, session: Session) -> None:
    user = make_user(session, "alice")
    submit = client.post(
        "/api/irc/submit",
        json={**SUBMIT, "irc_account": "al_irc_acct"},
        headers=SERVICE_HEADERS,
    )
    confirm_url = submit.json()["confirm_url"]
    token = confirm_url.rsplit("/", 1)[-1]
    log_in(client, user)

    page = client.get(f"/confirm/{token}")
    assert page.status_code == 200
    csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]

    response = client.post(
        f"/confirm/{token}",
        data={"csrf_token": csrf, "link_account": "true"},
        follow_redirects=False,
    )

    job_id = submit.json()["job"]["id"]
    assert response.headers["location"] == f"/jobs/{job_id}"
    session.expire_all()
    job = session.get(Job, job_id)
    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.owner_id == user.id
    link = session.scalar(select(IrcLink).where(IrcLink.irc_account == "al_irc_acct"))
    assert link is not None
    assert link.user_id == user.id


def test_confirm_requires_a_valid_csrf_token(client: TestClient, session: Session) -> None:
    user = make_user(session, "alice")
    submit = client.post(
        "/api/irc/submit",
        json={**SUBMIT, "irc_account": "al_irc_acct2"},
        headers=SERVICE_HEADERS,
    )
    token = submit.json()["confirm_url"].rsplit("/", 1)[-1]
    log_in(client, user)

    response = client.post(f"/confirm/{token}", data={"csrf_token": "wrong"})

    assert response.status_code == 403


def test_link_page_redirects_anonymous_users(client: TestClient) -> None:
    response = client.get("/link/whatever", follow_redirects=False)

    assert response.headers["location"] == "/login?next=/link/whatever"


def test_link_flow(client: TestClient, session: Session) -> None:
    user = make_user(session, "alice")
    link_response = client.post(
        "/api/irc/link", json={"irc_account": "bob_irc", "nick": "bob"}, headers=SERVICE_HEADERS
    )
    token = link_response.json()["link_url"].rsplit("/", 1)[-1]
    log_in(client, user)

    page = client.get(f"/link/{token}")
    assert page.status_code == 200
    csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]

    response = client.post(f"/link/{token}", data={"csrf_token": csrf}, follow_redirects=False)

    assert response.headers["location"] == "/wallet"
    link = session.scalar(select(IrcLink).where(IrcLink.irc_account == "bob_irc"))
    assert link is not None
    assert link.user_id == user.id
    assert client.post(f"/link/{token}", data={"csrf_token": csrf}).status_code == 404


def test_link_rejects_expired_tokens(client: TestClient, session: Session) -> None:
    session.add(
        LinkRequest(
            irc_account="old_irc",
            nick="old",
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
