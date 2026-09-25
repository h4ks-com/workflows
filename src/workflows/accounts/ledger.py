from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import Session

from workflows.db import Job
from workflows.db import LedgerEntry
from workflows.db import LedgerKind
from workflows.db import User
from workflows.db import utcnow
from workflows.settings import CREDITS_PER_BEAN
from workflows.settings import FREE_DAILY_CREDITS

BALANCE_COLUMNS = ["free_credits", "paid_credits", "free_day"]


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
    session.execute(
        update(User)
        .where(User.id == user.id)
        .values(
            free_credits=User.free_credits + free_delta,
            paid_credits=User.paid_credits + paid_delta,
        )
        .execution_options(synchronize_session=False)
    )
    session.refresh(user, BALANCE_COLUMNS)
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
    """Reset the free balance to the daily allowance on the user's first action of a UTC day."""
    today = utcnow().date()
    session.refresh(user, BALANCE_COLUMNS)
    if user.free_day == today:
        return
    session.execute(
        update(User)
        .where(User.id == user.id)
        .values(free_day=today)
        .execution_options(synchronize_session=False)
    )
    _record(session, user, LedgerKind.FREE_GRANT, free_delta=FREE_DAILY_CREDITS - user.free_credits)


def reserve(session: Session, user: User, job: Job) -> None:
    """Hold the job's quote, spending free credits first.

    :raises InsufficientCreditsError: when free and paid credits together fall short.
    """
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
    """Record that the job's held credits are spent."""
    if job.owner is not None:
        _record(session, job.owner, LedgerKind.CAPTURE, job=job)


def refund(session: Session, job: Job) -> None:
    """Return the job's held credits; free credits come back only on the day they were held."""
    if job.owner is None or job.reserved_free + job.reserved_paid == 0:
        return
    session.refresh(job.owner, BALANCE_COLUMNS)
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
    """Add paid credits for a Beans transfer, once per transfer id.

    :return: False when the transfer was already counted.
    """
    if session.scalar(select(LedgerEntry.id).where(LedgerEntry.beans_txn_id == beans_txn_id)):
        return False
    entry = _record(session, user, LedgerKind.TOPUP, paid_delta=beans * CREDITS_PER_BEAN)
    entry.beans_txn_id = beans_txn_id
    return True


def adjust(session: Session, user: User, credits: int, note: str) -> None:
    """Add or take paid credits as an admin, with a note.

    :raises InsufficientCreditsError: when it would take more than the user has.
    """
    session.refresh(user, BALANCE_COLUMNS)
    if user.paid_credits + credits < 0:
        raise InsufficientCreditsError(-credits, user.paid_credits)
    _record(session, user, LedgerKind.ADMIN_ADJUST, paid_delta=credits).note = note
