from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from conftest import SESSION_SECRET, FakeProber, log_in, make_user
from workflows.app import create_app
from workflows.db import User
from workflows.settings import Settings
from workflows.state import Services

DEV_BASE_URL = "http://localhost:8000"
LOGTO_URL = "https://logto.example/oidc/authorize?state=x"


@pytest.fixture
def dev_app(tmp_path: Path) -> Iterator[FastAPI]:
    settings = Settings(
        session_secret=SESSION_SECRET,
        database_url=f"sqlite:///{tmp_path}/dev/workflows.db",
        base_url=DEV_BASE_URL,
        dev_login=True,
    )
    app = create_app(settings, FakeProber())
    yield app
    app.state.services.sessions.kw["bind"].dispose()


@pytest.fixture
def dev_client(dev_app: FastAPI) -> TestClient:
    return TestClient(dev_app)


@pytest.fixture
def logto_app(tmp_path: Path) -> Iterator[FastAPI]:
    settings = Settings(
        session_secret=SESSION_SECRET,
        database_url=f"sqlite:///{tmp_path}/logto/workflows.db",
        base_url=DEV_BASE_URL,
        logto_endpoint="https://logto.example",
        logto_app_id="app-id",
        logto_app_secret="app-secret",
    )
    app = create_app(settings, FakeProber())
    yield app
    app.state.services.sessions.kw["bind"].dispose()


@pytest.fixture
def logto_client(logto_app: FastAPI) -> TestClient:
    return TestClient(logto_app)


def test_dev_login_disabled_when_base_url_is_https() -> None:
    with pytest.raises(ValueError, match="DEV_LOGIN"):
        Settings(
            session_secret=SESSION_SECRET, base_url="https://workflows.example", dev_login=True
        )


def test_dev_login_creates_a_local_user(dev_client: TestClient, dev_app: FastAPI) -> None:
    services: Services = dev_app.state.services

    response = dev_client.get("/login?as=alice", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/"
    with services.sessions() as session:
        users = session.scalars(select(User)).all()
        assert [(user.logto_sub, user.username) for user in users] == [("dev:alice", "alice")]


def test_login_needs_logto_when_dev_login_is_off(client: TestClient) -> None:
    assert client.get("/login").status_code == 503


def test_logout_clears_the_session(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice"))

    response = client.get("/logout", follow_redirects=False)

    assert response.headers["location"] == "/"
    assert "session=null" in response.headers["set-cookie"]
    assert "1970" in response.headers["set-cookie"]


def test_login_redirects_to_logto(
    logto_client: TestClient, logto_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    services: Services = logto_app.state.services
    assert services.oauth is not None
    redirect = AsyncMock(return_value=RedirectResponse(LOGTO_URL))
    monkeypatch.setattr(services.oauth.logto, "authorize_redirect", redirect)

    response = logto_client.get("/login?next=/wallet", follow_redirects=False)

    assert response.headers["location"] == LOGTO_URL
    assert redirect.await_args is not None
    assert redirect.await_args.args[1] == f"{DEV_BASE_URL}/auth/callback"


def test_callback_creates_the_user_and_honors_next(
    logto_client: TestClient, logto_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    services: Services = logto_app.state.services
    assert services.oauth is not None
    monkeypatch.setattr(
        services.oauth.logto,
        "authorize_redirect",
        AsyncMock(return_value=RedirectResponse(LOGTO_URL)),
    )
    monkeypatch.setattr(
        services.oauth.logto,
        "authorize_access_token",
        AsyncMock(return_value={"userinfo": {"sub": "logto-sub-1", "username": "bob"}}),
    )

    with logto_client as active:
        active.get("/login?next=/wallet")
        response = active.get("/auth/callback", follow_redirects=False)

    assert response.headers["location"] == "/wallet"
    with services.sessions() as session:
        user = session.scalar(select(User).where(User.logto_sub == "logto-sub-1"))
        assert user is not None
        assert user.username == "bob"


def test_callback_rejects_missing_username(
    logto_client: TestClient, logto_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    services: Services = logto_app.state.services
    assert services.oauth is not None
    monkeypatch.setattr(
        services.oauth.logto,
        "authorize_access_token",
        AsyncMock(return_value={"userinfo": {"sub": "logto-sub-2"}}),
    )

    response = logto_client.get("/auth/callback")

    assert response.status_code == 400


def test_callback_needs_logto_configured(client: TestClient) -> None:
    assert client.get("/auth/callback").status_code == 503


def test_dev_bypass_needs_the_as_query_param(tmp_path: Path) -> None:
    settings = Settings(
        session_secret=SESSION_SECRET,
        database_url=f"sqlite:///{tmp_path}/both/workflows.db",
        base_url=DEV_BASE_URL,
        dev_login=True,
        logto_endpoint="https://logto.example",
        logto_app_id="app-id",
        logto_app_secret="app-secret",
    )
    app = create_app(settings, FakeProber())
    client = TestClient(app)

    response = client.get("/login?as=carol", follow_redirects=False)

    assert response.headers["location"] == "/"
    app.state.services.sessions.kw["bind"].dispose()
