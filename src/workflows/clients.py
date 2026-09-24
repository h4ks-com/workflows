import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import Response

from workflows.api import QuoteRequest, price_request
from workflows.auth import csrf_token, require_service
from workflows.catalog import JobType
from workflows.db import (
    ExternalIdentity,
    Job,
    JobStatus,
    LinkRequest,
    Subscription,
    User,
    utcnow,
)
from workflows.jobs import announce, create_job, enqueue, hash_token
from workflows.ledger import InsufficientCreditsError, grant_daily
from workflows.state import AppServices, Db, Services
from workflows.views import JobView, job_view
from workflows.webhooks import Subscribe, Webhook
from workflows.webviews import (
    TOPUP_HINT,
    Page,
    PageCtx,
    TemplateValue,
    login_redirect,
    render,
    verified_form,
)

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


class ClientJobRequest(QuoteRequest):
    identity: IdentityStr | None = Field(
        None, description="Identity acting for this job, or null for an anonymous submission."
    )
    webhook: Webhook | None = Field(None, description="Where to post every status change.")


class ClientJobView(BaseModel):
    job: JobView = Field(description="The created job.")
    confirm_url: str | None = Field(None, description="Confirm this job by logging in here.")


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


def _submit_for_linked_user(
    session: Session, services: Services, job: Job, owner: User
) -> ClientJobView:
    try:
        enqueue(session, job, owner)
    except InsufficientCreditsError as error:
        session.rollback()
        topup_url = f"{services.settings.base_url}/wallet"
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED, f"{error}; top up at {topup_url}"
        ) from error
    session.commit()
    announce(services.bus, job)
    return ClientJobView(job=job_view(job, services.catalog.find(job.type)), confirm_url=None)


def _submit_awaiting_confirmation(
    session: Session, services: Services, job: Job, job_type: JobType
) -> ClientJobView:
    token = secrets.token_urlsafe(32)
    job.confirm_token_hash = hash_token(token)
    session.commit()
    confirm_url = f"{services.settings.base_url}/confirm/{token}"
    return ClientJobView(job=job_view(job, job_type), confirm_url=confirm_url)


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def submit_job(body: ClientJobRequest, session: Db, services: AppServices) -> ClientJobView:
    job_type, params, priced = await price_request(services, body)
    job = create_job(session, job_type, params, priced, None)
    job.identity = body.identity
    job.webhook = body.webhook.model_dump(mode="json") if body.webhook else None
    linked_user = _linked_user(session, body.identity) if body.identity else None
    if linked_user is not None:
        return _submit_for_linked_user(session, services, job, linked_user)
    return _submit_awaiting_confirmation(session, services, job, job_type)


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


def _job_by_confirm_token(session: Session, token: str) -> Job:
    digest = hash_token(token)
    job = session.scalar(select(Job).where(Job.confirm_token_hash == digest))
    if job is None or job.status != JobStatus.AWAITING_CONFIRMATION:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "this confirm link is no longer valid")
    return job


def _link_request_by_token(session: Session, token: str) -> LinkRequest:
    digest = hash_token(token)
    link_request = session.scalar(select(LinkRequest).where(LinkRequest.token_hash == digest))
    if link_request is None or link_request.used or link_request.expires_at < utcnow():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "this link is no longer valid")
    return link_request


@pages_router.get("/confirm/{token}")
async def confirm_page(token: str, page: PageCtx) -> Response:
    if page.user is None:
        return login_redirect(f"/confirm/{token}")
    return _render_confirm(page, _job_by_confirm_token(page.session, token))


def _render_confirm(
    page: Page, job: Job, error: str | None = None, status_code: int = 200
) -> Response:
    context: dict[str, TemplateValue] = {
        "job": job,
        "job_type": page.services.catalog.find(job.type),
        "csrf_token": csrf_token(page.request),
        "error": error,
    }
    return render(page, "confirm.html", context, status_code)


@pages_router.post("/confirm/{token}")
async def confirm_submit(token: str, page: PageCtx) -> Response:
    if page.user is None:
        return login_redirect(f"/confirm/{token}")
    form = await verified_form(page.request)
    job = _job_by_confirm_token(page.session, token)
    if form.get("link_account") == "true" and job.identity:
        _link_identity(page.session, job.identity, page.user)
    try:
        enqueue(page.session, job, page.user)
    except InsufficientCreditsError:
        page.session.rollback()
        return _render_confirm(page, job, TOPUP_HINT, status.HTTP_402_PAYMENT_REQUIRED)
    job.confirm_token_hash = None
    page.session.commit()
    announce(page.services.bus, job)
    return RedirectResponse(f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


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
