import asyncio
import json
from base64 import b64encode
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from workflows.app import create_app
from workflows.auth import SESSION_USER_KEY
from workflows.db import Job, LedgerEntry, User
from workflows.jobs import create_job, enqueue
from workflows.jobtypes import Probe, ProbeError, Quote, SongParams
from workflows.ledger import adjust
from workflows.settings import Settings
from workflows.state import Services

SESSION_SECRET = "test-secret"
SERVICE_TOKEN = "service-token"
EXECUTOR_TOKEN = "executor-token"
BASE_URL = "https://workflows.example"
PARODY_EXECUTOR = "https://n8n.example/webhook/parody"
SONG_EXECUTOR = "https://n8n.example/webhook/song"
SONG_URL = "https://youtube.example/watch?v=song"
FETCH_HEADERS = {"X-Requested-With": "fetch"}


class FakeProber:
    def __init__(self) -> None:
        self.durations: dict[str, float] = {SONG_URL: 100.0}
        self.delay_seconds = 0.0

    async def info(self, url: str) -> Probe:
        await asyncio.sleep(self.delay_seconds)
        if url not in self.durations:
            raise ProbeError(f"could not read {url}")
        return Probe(self.durations[url], "Some Song")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        session_secret=SESSION_SECRET,
        database_url=f"sqlite:///{tmp_path}/db/workflows.db",
        base_url=BASE_URL,
        service_token=SERVICE_TOKEN,
        admin_users=frozenset({"root"}),
        executor_urls={"parody": PARODY_EXECUTOR, "song": SONG_EXECUTOR},
        executor_token=EXECUTOR_TOKEN,
    )


@pytest.fixture
def prober() -> FakeProber:
    return FakeProber()


@pytest.fixture
def app(settings: Settings, prober: FakeProber) -> Iterator[FastAPI]:
    app = create_app(settings, prober)
    yield app
    engine: Engine = app.state.services.sessions.kw["bind"]
    engine.dispose()


@pytest.fixture
def services(app: FastAPI) -> Services:
    services: Services = app.state.services
    return services


@pytest.fixture
def session(services: Services) -> Iterator[Session]:
    with services.sessions() as session:
        yield session


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="https://testserver", headers=FETCH_HEADERS)


def make_user(session: Session, username: str, paid_credits: int = 0) -> User:
    user = User(logto_sub=f"sub-{username}", username=username)
    session.add(user)
    session.commit()
    if paid_credits:
        adjust(session, user, paid_credits, "test balance")
        session.commit()
    return user


def ledger_sums(session: Session, user: User) -> tuple[int, int]:
    free, paid = session.execute(
        select(func.sum(LedgerEntry.free_delta), func.sum(LedgerEntry.paid_delta)).where(
            LedgerEntry.user_id == user.id
        )
    ).one()
    return free or 0, paid or 0


def assert_ledger_matches(session: Session, user: User) -> None:
    session.refresh(user)
    assert ledger_sums(session, user) == (user.free_credits, user.paid_credits)


def make_job(session: Session, services: Services, owner: User | None, credits: int = 100) -> Job:
    params = SongParams(prompt="a song about cats")
    job = create_job(session, services.registry["song"], params, Quote(credits, 60, None), owner)
    session.commit()
    return job


def queue_job(session: Session, services: Services, owner: User, credits: int = 100) -> Job:
    job = make_job(session, services, owner, credits)
    enqueue(session, job, owner)
    session.commit()
    return job


def session_cookie(user: User) -> str:
    data = b64encode(json.dumps({SESSION_USER_KEY: user.id}).encode())
    return TimestampSigner(SESSION_SECRET).sign(data).decode()


def log_in(client: TestClient, user: User) -> None:
    client.cookies.set("session", session_cookie(user))
