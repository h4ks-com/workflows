import asyncio
import logging
import secrets
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from workflows.db import Draft
from workflows.db import JsonObject
from workflows.db import utcnow

logger = logging.getLogger(__name__)

DRAFT_LIFETIME = timedelta(days=7)
SWEEP_SECONDS = 3600
DRAFT_ID_BYTES = 6
DRAFT_ID_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"


def live_draft_count(session: Session) -> int:
    return session.scalar(select(func.count(Draft.id)).where(Draft.expires_at > utcnow())) or 0


def create_draft(session: Session, type_name: str, params: JsonObject) -> Draft:
    """Store a filled form under a short random token that expires after `DRAFT_LIFETIME`."""
    draft = Draft(
        token=secrets.token_urlsafe(DRAFT_ID_BYTES),
        type=type_name,
        params=params,
        expires_at=utcnow() + DRAFT_LIFETIME,
    )
    session.add(draft)
    return draft


def live_draft(session: Session, token: str) -> Draft | None:
    """Return the draft behind `token`, deleting it when it has expired."""
    draft = session.scalar(select(Draft).where(Draft.token == token))
    if draft is None or draft.expires_at > utcnow():
        return draft
    session.delete(draft)
    return None


def delete_draft(session: Session, token: str) -> None:
    session.execute(delete(Draft).where(Draft.token == token))


def delete_expired_drafts(session: Session) -> None:
    session.execute(delete(Draft).where(Draft.expires_at <= utcnow()))


async def sweep_drafts(sessions: sessionmaker[Session]) -> None:
    """Delete expired drafts every `SWEEP_SECONDS`."""
    while True:
        try:
            with sessions.begin() as session:
                delete_expired_drafts(session)
        except SQLAlchemyError:
            logger.exception("draft sweep failed")
        await asyncio.sleep(SWEEP_SECONDS)
