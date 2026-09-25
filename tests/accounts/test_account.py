from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from conftest import log_in, make_user
from workflows.accounts.account import get_or_create_user
from workflows.db import ExternalIdentity


def test_get_or_create_user_creates_then_syncs_username(session: Session) -> None:
    created = get_or_create_user(session, "sub-1", "alice")
    session.commit()

    fetched = get_or_create_user(session, "sub-1", "alice2")

    assert fetched.id == created.id
    assert fetched.username == "alice2"


def test_me_grants_daily_credits_and_lists_linked_identities(
    client: TestClient, session: Session
) -> None:
    user = make_user(session, "alice", paid_credits=50)
    session.add(ExternalIdentity(identity="irc:alice", user_id=user.id))
    session.commit()
    log_in(client, user)

    response = client.get("/api/me")

    assert response.json() == {
        "username": "alice",
        "free_credits": 500,
        "paid_credits": 50,
        "admin": False,
        "identities": ["irc:alice"],
    }


def test_me_marks_admins(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "root"))

    assert client.get("/api/me").json()["admin"] is True


def test_me_requires_login(client: TestClient) -> None:
    assert client.get("/api/me").status_code == 401


def test_ledger_lists_entries_newest_first(client: TestClient, session: Session) -> None:
    user = make_user(session, "alice")
    log_in(client, user)
    client.get("/api/me")

    entries = client.get("/api/me/ledger").json()

    assert [entry["kind"] for entry in entries] == ["free_grant"]
    assert entries[0]["free_delta"] == 500


def test_topup_returns_the_beans_transfer_url(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice"))

    response = client.post("/api/topups", json={"beans": 5})

    assert response.json() == {"beans_url": "https://beans.h4ks.com/transfer/alice/workflows/5"}


def test_topup_rejects_out_of_range_amounts(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice"))

    assert client.post("/api/topups", json={"beans": 0}).status_code == 422


def test_unlink_identity(client: TestClient, session: Session) -> None:
    user = make_user(session, "alice")
    session.add(ExternalIdentity(identity="irc:alice", user_id=user.id))
    session.commit()
    log_in(client, user)

    assert client.delete("/api/me/identities/irc:alice").status_code == 204
    assert client.delete("/api/me/identities/irc:alice").status_code == 404
    assert client.get("/api/me").json()["identities"] == []
