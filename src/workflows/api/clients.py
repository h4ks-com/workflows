import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from pydantic import Field
from pydantic import HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import Response

from workflows.accounts.auth import csrf_token
from workflows.accounts.auth import require_service
from workflows.accounts.ledger import grant_daily
from workflows.api.drafts import DraftView
from workflows.api.drafts import share_form
from workflows.api.routes import QuoteRequest
from workflows.db import ExternalIdentity
from workflows.db import LinkRequest
from workflows.db import Subscription
from workflows.db import User
from workflows.db import utcnow
from workflows.runs.jobs import hash_token
from workflows.runs.webhooks import Subscribe
from workflows.state import AppServices
from workflows.state import Db
from workflows.web.render import PageCtx
from workflows.web.render import TemplateValue
from workflows.web.render import login_redirect
from workflows.web.render import render
from workflows.web.render import verified_form

LINK_EXPIRY = timedelta(hours=1)
IDENTITY_PATTERN = r"^\S{1,200}$"
router = APIRouter(prefix="/api/clients", dependencies=[Depends(require_service)])
pages_router = APIRouter()

IdentityStr = Annotated[
    str,
    Field(
        pattern=IDENTITY_PATTERN,
        description="Opaque identity the client chooses, for example 'chat:alice'.",
    ),
]


class ClientLinkRequest(BaseModel):
    identity: IdentityStr = Field(description="Identity to link.")


class ClientLinkView(BaseModel):
    link_url: str = Field(description="One-time page to confirm the link by logging in.")


class SubscriptionView(BaseModel):
    url: str = Field(description="URL that receives a signed POST on every job event.")


class IdentityView(BaseModel):
    username: str = Field(description="Username linked to the identity.")
    free_credits: int = Field(description="Daily free credits left today.")
    paid_credits: int = Field(description="Credits bought with beans.")


def _linked_user(session: Session, identity: str) -> User | None:
    link = session.scalar(select(ExternalIdentity).where(ExternalIdentity.identity == identity))
    return link.user if link else None


def _link_identity(session: Session, identity: str, user: User) -> None:
    existing = session.scalar(select(ExternalIdentity).where(ExternalIdentity.identity == identity))
    if existing is None:
        session.add(ExternalIdentity(identity=identity, user_id=user.id))
    elif existing.user_id != user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{identity} is linked to another account")


@router.post("/drafts", status_code=status.HTTP_201_CREATED)
async def create_draft_link(body: QuoteRequest, services: AppServices) -> DraftView:
    return await share_form(services, body)


@router.post("/links")
async def create_link(
    body: ClientLinkRequest, session: Db, services: AppServices
) -> ClientLinkView:
    token = secrets.token_urlsafe(32)
    session.add(
        LinkRequest(
            identity=body.identity,
            token_hash=hash_token(token),
            expires_at=utcnow() + LINK_EXPIRY,
        )
    )
    session.commit()
    return ClientLinkView(link_url=f"{services.settings.base_url}/link/{token}")


@router.get("/identities/{identity}")
async def get_identity(identity: str, session: Db) -> IdentityView:
    user = _linked_user(session, identity)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no user linked to {identity}")
    grant_daily(session, user)
    session.commit()
    return IdentityView(
        username=user.username, free_credits=user.free_credits, paid_credits=user.paid_credits
    )


@router.put("/subscription")
async def put_subscription(body: Subscribe, session: Db) -> SubscriptionView:
    url = str(body.url)
    subscription = session.scalar(select(Subscription).where(Subscription.url == url))
    if subscription is None:
        subscription = Subscription(url=url)
        session.add(subscription)
    subscription.signing_key = body.signing_key
    session.commit()
    return SubscriptionView(url=url)


@router.delete("/subscription", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subscription(url: HttpUrl, session: Db) -> None:
    subscription = session.scalar(select(Subscription).where(Subscription.url == str(url)))
    if subscription is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no subscription for this url")
    session.delete(subscription)
    session.commit()


def _link_request_by_token(session: Session, token: str) -> LinkRequest:
    digest = hash_token(token)
    link_request = session.scalar(select(LinkRequest).where(LinkRequest.token_hash == digest))
    if link_request is None or link_request.used or link_request.expires_at < utcnow():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "this link is no longer valid")
    return link_request


@pages_router.get("/link/{token}")
async def link_page(token: str, page: PageCtx) -> Response:
    if page.user is None:
        return login_redirect(f"/link/{token}")
    link_request = _link_request_by_token(page.session, token)
    context: dict[str, TemplateValue] = {
        "link_request": link_request,
        "csrf_token": csrf_token(page.request),
    }
    return render(page, "link.html", context)


@pages_router.post("/link/{token}")
async def link_submit(token: str, page: PageCtx) -> Response:
    if page.user is None:
        return login_redirect(f"/link/{token}")
    await verified_form(page.request)
    link_request = _link_request_by_token(page.session, token)
    _link_identity(page.session, link_request.identity, page.user)
    link_request.used = True
    page.session.commit()
    return RedirectResponse("/wallet", status_code=status.HTTP_303_SEE_OTHER)
