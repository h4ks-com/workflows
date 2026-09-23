from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from workflows.auth import LoggedInUser, is_admin
from workflows.db import IrcLink, LedgerEntry, User
from workflows.ledger import grant_daily
from workflows.state import AppServices, Db

MAX_TOPUP_BEANS = 1000
LEDGER_LIMIT = 100

router = APIRouter(prefix="/api")


class MeView(BaseModel):
    username: str = Field(description="Logto username.")
    free_credits: int = Field(description="Daily free credits left today.")
    paid_credits: int = Field(description="Credits bought with beans.")
    admin: bool = Field(description="Whether the user is a workflows admin.")
    irc_accounts: list[str] = Field(description="IRC accounts linked to this user.")


class LedgerEntryView(BaseModel):
    kind: str = Field(description="Kind of ledger entry.")
    free_delta: int = Field(description="Change in free credits.")
    paid_delta: int = Field(description="Change in paid credits.")
    job_id: int | None = Field(description="Job this entry is for, if any.")
    note: str | None = Field(description="Admin note, when set.")
    created_at: datetime = Field(description="When the entry was recorded.")


class TopupRequest(BaseModel):
    beans: int = Field(ge=1, le=MAX_TOPUP_BEANS, description="Beans to convert into credits.")


class TopupView(BaseModel):
    beans_url: str = Field(description="Beans page to confirm the transfer.")


def get_or_create_user(session: Session, logto_sub: str, username: str) -> User:
    user = session.scalar(select(User).where(User.logto_sub == logto_sub))
    if user is None:
        user = User(logto_sub=logto_sub, username=username)
        session.add(user)
        session.flush()
        return user
    if user.username != username:
        user.username = username
    return user


def _irc_accounts(session: Session, user: User) -> list[str]:
    query = select(IrcLink.irc_account).where(IrcLink.user_id == user.id)
    return list(session.scalars(query))


@router.get("/me")
async def get_me(user: LoggedInUser, session: Db, services: AppServices) -> MeView:
    grant_daily(session, user)
    session.commit()
    return MeView(
        username=user.username,
        free_credits=user.free_credits,
        paid_credits=user.paid_credits,
        admin=is_admin(user, services.settings),
        irc_accounts=_irc_accounts(session, user),
    )


@router.get("/me/ledger")
async def get_ledger(user: LoggedInUser, session: Db) -> list[LedgerEntryView]:
    query = (
        select(LedgerEntry)
        .where(LedgerEntry.user_id == user.id)
        .order_by(LedgerEntry.id.desc())
        .limit(LEDGER_LIMIT)
    )
    return [
        LedgerEntryView(
            kind=entry.kind,
            free_delta=entry.free_delta,
            paid_delta=entry.paid_delta,
            job_id=entry.job_id,
            note=entry.note,
            created_at=entry.created_at,
        )
        for entry in session.scalars(query)
    ]


@router.post("/topups")
async def create_topup(body: TopupRequest, user: LoggedInUser, services: AppServices) -> TopupView:
    url = f"{services.settings.beans_url}/transfer/{user.username}/workflows/{body.beans}"
    return TopupView(beans_url=url)
