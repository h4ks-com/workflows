from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from workflows.accounts.auth import LoggedInUser, is_admin, require_fetch_header
from workflows.accounts.ledger import grant_daily
from workflows.db import ExternalIdentity, LedgerEntry, User
from workflows.state import AppServices, Db

MAX_TOPUP_BEANS = 1000
LEDGER_LIMIT = 100

router = APIRouter(prefix="/api", dependencies=[Depends(require_fetch_header)])


class MeView(BaseModel):
    username: str = Field(description="Logto username.")
    free_credits: int = Field(description="Daily free credits left today.")
    paid_credits: int = Field(description="Credits bought with beans.")
    admin: bool = Field(description="Whether the user is a workflows admin.")
    identities: list[str] = Field(description="External identities linked to this user.")


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


def linked_identities(session: Session, user: User) -> list[str]:
    query = select(ExternalIdentity.identity).where(ExternalIdentity.user_id == user.id)
    return list(session.scalars(query))


def unlink_identity(session: Session, user: User, identity: str) -> None:
    link = session.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.identity == identity, ExternalIdentity.user_id == user.id
        )
    )
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{identity} is not linked to you")
    session.delete(link)
    session.commit()


def ledger_entries(session: Session, user: User, limit: int = LEDGER_LIMIT) -> list[LedgerEntry]:
    query = (
        select(LedgerEntry)
        .where(LedgerEntry.user_id == user.id)
        .order_by(LedgerEntry.id.desc())
        .limit(limit)
    )
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
        identities=linked_identities(session, user),
    )


@router.delete("/me/identities/{identity}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_identity(identity: str, user: LoggedInUser, session: Db) -> None:
    unlink_identity(session, user, identity)


@router.get("/me/ledger")
async def get_ledger(user: LoggedInUser, session: Db) -> list[LedgerEntryView]:
    return [
        LedgerEntryView(
            kind=entry.kind,
            free_delta=entry.free_delta,
            paid_delta=entry.paid_delta,
            job_id=entry.job_id,
            note=entry.note,
            created_at=entry.created_at,
        )
        for entry in ledger_entries(session, user)
    ]


def topup_url(beans_url: str, username: str, beans: int) -> str:
    return f"{beans_url}/transfer/{username}/workflows/{beans}"


@router.post("/topups")
async def create_topup(body: TopupRequest, user: LoggedInUser, services: AppServices) -> TopupView:
    return TopupView(beans_url=topup_url(services.settings.beans_url, user.username, body.beans))
