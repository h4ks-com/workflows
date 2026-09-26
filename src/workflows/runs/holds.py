from datetime import timedelta

from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

from workflows.db import QueueHold
from workflows.db import utcnow


def active_hold(session: Session) -> QueueHold | None:
    """Return the hold that keeps the worker from starting jobs now, if any."""
    now = utcnow()
    query = (
        select(QueueHold)
        .where(
            QueueHold.released_at.is_(None),
            or_(QueueHold.until.is_(None), QueueHold.until > now),
        )
        .order_by(QueueHold.id.desc())
        .limit(1)
    )
    return session.scalar(query)


def hold_queue(session: Session, reason: str, minutes: int | None) -> QueueHold:
    """Hold the queue for `minutes`, or until released when `minutes` is None, replacing any hold.

    :return: the new hold.
    """
    release_queue(session)
    until = utcnow() + timedelta(minutes=minutes) if minutes is not None else None
    hold = QueueHold(reason=reason, until=until)
    session.add(hold)
    return hold


def release_queue(session: Session) -> None:
    """End every hold now."""
    now = utcnow()
    for hold in session.scalars(select(QueueHold).where(QueueHold.released_at.is_(None))):
        hold.released_at = now


def held_seconds(hold: QueueHold | None) -> float:
    """Seconds until a hold ends by itself; zero without a hold or for one with no end."""
    if hold is None or hold.until is None:
        return 0.0
    return max(0.0, (hold.until - utcnow()).total_seconds())
