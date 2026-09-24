import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conftest import (
    BASE_URL,
    SONG_URL,
    FakeProber,
    assert_ledger_matches,
    make_job,
    make_user,
    queue_job,
    session_cookie,
)
from workflows.db import LedgerEntry, LedgerKind, User
from workflows.jobs import cancel
from workflows.ledger import (
    InsufficientCreditsError,
    adjust,
    capture,
    grant_daily,
    refund,
    reserve,
    topup,
)
from workflows.settings import FREE_DAILY_CREDITS
from workflows.state import Services

TOMORROW = datetime.now(UTC) + timedelta(days=1)
PARODY = {"type": "parody", "params": {"url": SONG_URL}}
PARODY_QUOTE = 240
LONG_SONG_URL = "https://youtube.example/watch?v=long"
LONG_PARODY = {"type": "parody", "params": {"url": LONG_SONG_URL}}


def test_daily_grant_sets_the_free_balance_once_per_day(
    session: Session, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_user(session, "alice")
    queue_job(session, services, user, credits=200)
    grant_daily(session, user)
    assert user.free_credits == FREE_DAILY_CREDITS - 200

    monkeypatch.setattr("workflows.ledger.utcnow", lambda: TOMORROW)
    grant_daily(session, user)

    assert user.free_credits == FREE_DAILY_CREDITS
    assert user.free_day == TOMORROW.date()
    assert_ledger_matches(session, user)


def test_reserve_spends_free_credits_before_paid(session: Session, services: Services) -> None:
    user = make_user(session, "alice", paid_credits=300)
    job = make_job(session, services, user, credits=600)

    reserve(session, user, job)
    session.commit()

    assert (job.reserved_free, job.reserved_paid) == (500, 100)
    assert (user.free_credits, user.paid_credits) == (0, 200)
    assert_ledger_matches(session, user)


def test_reserve_fails_without_enough_credits(session: Session, services: Services) -> None:
    user = make_user(session, "alice")
    job = make_job(session, services, user, credits=501)

    with pytest.raises(InsufficientCreditsError, match="needs 501 credits and you have 500"):
        reserve(session, user, job)


def test_refund_returns_free_credits_on_the_same_day(session: Session, services: Services) -> None:
    user = make_user(session, "alice", paid_credits=100)
    job = queue_job(session, services, user, credits=550)

    refund(session, job)
    refund(session, job)
    session.commit()

    assert (user.free_credits, user.paid_credits) == (500, 100)
    assert_ledger_matches(session, user)


def test_refund_drops_free_credits_from_an_expired_day(
    session: Session, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_user(session, "alice", paid_credits=100)
    job = queue_job(session, services, user, credits=550)
    monkeypatch.setattr("workflows.ledger.utcnow", lambda: TOMORROW)
    grant_daily(session, user)

    cancel(session, job)
    session.commit()

    assert (user.free_credits, user.paid_credits) == (500, 100)
    assert_ledger_matches(session, user)


def test_capture_and_refund_skip_ownerless_jobs(session: Session, services: Services) -> None:
    job = make_job(session, services, None)

    capture(session, job)
    refund(session, job)

    assert session.scalar(select(func.count(LedgerEntry.id))) == 0


def test_capture_records_the_spend(session: Session, services: Services) -> None:
    user = make_user(session, "alice")
    job = queue_job(session, services, user)

    capture(session, job)
    session.commit()

    kinds = session.scalars(select(LedgerEntry.kind).order_by(LedgerEntry.id)).all()
    assert kinds == [LedgerKind.FREE_GRANT, LedgerKind.RESERVE, LedgerKind.CAPTURE]


def test_topup_is_idempotent_on_the_beans_transaction(session: Session) -> None:
    user = make_user(session, "alice")

    assert topup(session, user, 3, "txn-1")
    session.commit()
    assert not topup(session, user, 3, "txn-1")

    assert user.paid_credits == 300


def test_admin_adjust_cannot_go_negative(session: Session) -> None:
    user = make_user(session, "alice", paid_credits=50)

    adjust(session, user, -20, "correction")
    with pytest.raises(InsufficientCreditsError):
        adjust(session, user, -40, "too much")

    assert user.paid_credits == 30


def api_client(app: FastAPI, user: User) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        cookies={"session": session_cookie(user)},
        headers={"X-Requested-With": "fetch"},
    )


def free_grants(session: Session, user: User) -> int:
    query = select(func.count(LedgerEntry.id)).where(
        LedgerEntry.user_id == user.id, LedgerEntry.kind == LedgerKind.FREE_GRANT
    )
    return session.scalar(query) or 0


async def test_concurrent_submissions_each_pay_their_quote(
    app: FastAPI, session: Session, prober: FakeProber
) -> None:
    user = make_user(session, "alice", paid_credits=1000)
    prober.delay_seconds = 0.1

    async with api_client(app, user) as client:
        first, second = await asyncio.gather(
            client.post("/api/jobs", json=PARODY), client.post("/api/jobs", json=PARODY)
        )

    assert (first.status_code, second.status_code) == (201, 201)
    session.refresh(user)
    assert user.free_credits + user.paid_credits == 1500 - 2 * PARODY_QUOTE
    assert free_grants(session, user) == 1
    assert_ledger_matches(session, user)


async def test_topup_during_a_probe_is_kept(
    app: FastAPI, session: Session, services: Services, prober: FakeProber
) -> None:
    user = make_user(session, "alice", paid_credits=200)
    prober.durations[LONG_SONG_URL] = 400.0
    prober.delay_seconds = 0.1

    async def credit_during_probe() -> None:
        await asyncio.sleep(0.05)
        with services.sessions.begin() as other:
            topup(other, other.get_one(User, user.id), 3, "txn-1")

    async with api_client(app, user) as client:
        response, _ = await asyncio.gather(
            client.post("/api/jobs", json=LONG_PARODY), credit_during_probe()
        )

    assert response.status_code == 201
    session.refresh(user)
    assert (user.free_credits, user.paid_credits) == (0, 200 + 300 - (600 - 500))
    assert_ledger_matches(session, user)
