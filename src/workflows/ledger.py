from sqlalchemy import select
from sqlalchemy.orm import Session

from workflows.db import Job, LedgerEntry, LedgerKind, User, utcnow
from workflows.settings import CREDITS_PER_BEAN, FREE_DAILY_CREDITS


class InsufficientCreditsError(Exception):
    def __init__(self, needed: int, available: int) -> None:
        super().__init__(f"this needs {needed} credits and you have {available}")


def _record(
    session: Session,
    user: User,
    kind: LedgerKind,
    *,
    free_delta: int = 0,
    paid_delta: int = 0,
    job: Job | None = None,
) -> LedgerEntry:
    user.free_credits += free_delta
    user.paid_credits += paid_delta
    entry = LedgerEntry(
        user_id=user.id,
        kind=kind,
        free_delta=free_delta,
        paid_delta=paid_delta,
        job_id=job.id if job else None,
    )
    session.add(entry)
    return entry


def grant_daily(session: Session, user: User) -> None:
    today = utcnow().date()
    if user.free_day == today:
        return
    _record(session, user, LedgerKind.FREE_GRANT, free_delta=FREE_DAILY_CREDITS - user.free_credits)
    user.free_day = today


def reserve(session: Session, user: User, job: Job) -> None:
    grant_daily(session, user)
    available = user.free_credits + user.paid_credits
    if available < job.quote:
        raise InsufficientCreditsError(job.quote, available)
    job.reserved_free = min(user.free_credits, job.quote)
    job.reserved_paid = job.quote - job.reserved_free
    _record(
        session,
        user,
        LedgerKind.RESERVE,
        free_delta=-job.reserved_free,
        paid_delta=-job.reserved_paid,
        job=job,
    )


def capture(session: Session, job: Job) -> None:
    if job.owner is not None:
        _record(session, job.owner, LedgerKind.CAPTURE, job=job)


def refund(session: Session, job: Job) -> None:
    if job.owner is None or job.reserved_free + job.reserved_paid == 0:
        return
    reserved_on = job.queued_at.date() if job.queued_at else None
    free_back = job.reserved_free if job.owner.free_day == reserved_on else 0
    _record(
        session,
        job.owner,
        LedgerKind.REFUND,
        free_delta=free_back,
        paid_delta=job.reserved_paid,
        job=job,
    )
    job.reserved_free = job.reserved_paid = 0


def topup(session: Session, user: User, beans: int, beans_txn_id: str) -> bool:
    if session.scalar(select(LedgerEntry.id).where(LedgerEntry.beans_txn_id == beans_txn_id)):
        return False
    entry = _record(session, user, LedgerKind.TOPUP, paid_delta=beans * CREDITS_PER_BEAN)
    entry.beans_txn_id = beans_txn_id
    return True


def adjust(session: Session, user: User, credits: int, note: str) -> None:
    if user.paid_credits + credits < 0:
        raise InsufficientCreditsError(-credits, user.paid_credits)
    _record(session, user, LedgerKind.ADMIN_ADJUST, paid_delta=credits).note = note
