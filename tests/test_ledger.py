from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conftest import make_job, make_user, queue_job
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


def ledger_sums(session: Session, user: User) -> tuple[int, int]:
    free, paid = session.execute(
        select(func.sum(LedgerEntry.free_delta), func.sum(LedgerEntry.paid_delta)).where(
            LedgerEntry.user_id == user.id
        )
    ).one()
    return free or 0, paid or 0


def test_daily_grant_sets_the_free_balance_once_per_day(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_user(session, "alice")
    grant_daily(session, user)
    user.free_credits -= 200
    grant_daily(session, user)
    assert user.free_credits == FREE_DAILY_CREDITS - 200

    monkeypatch.setattr("workflows.ledger.utcnow", lambda: TOMORROW)
    grant_daily(session, user)

    assert user.free_credits == FREE_DAILY_CREDITS
    assert user.free_day == TOMORROW.date()


def test_reserve_spends_free_credits_before_paid(session: Session, services: Services) -> None:
    user = make_user(session, "alice", paid_credits=300)
    job = make_job(session, services, user, credits=600)

    reserve(session, user, job)
    session.commit()

    assert (job.reserved_free, job.reserved_paid) == (500, 100)
    assert (user.free_credits, user.paid_credits) == (0, 200)
    assert ledger_sums(session, user) == (0, -100)


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
    assert ledger_sums(session, user) == (500, 0)


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
    assert ledger_sums(session, user) == (500, 0)


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
