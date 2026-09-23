import secrets
from datetime import timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import Response

from workflows.api import JobView, get_job_type, job_view
from workflows.auth import csrf_token, current_user, require_service, verify_csrf
from workflows.db import ExternalIdentity, Job, JobStatus, JsonObject, LinkRequest, User, utcnow
from workflows.jobs import (
    announce,
    create_job,
    enqueue,
    ensure_available,
    hash_token,
    job_type_for,
)
from workflows.jobtypes import JobType, quote
from workflows.ledger import InsufficientCreditsError
from workflows.state import AppServices, Db, Services
from workflows.webviews import is_playable_url, me_chip

LINK_EXPIRY = timedelta(hours=1)
IDENTITY_PATTERN = r"^\S{1,200}$"
router = APIRouter(prefix="/api/clients", dependencies=[Depends(require_service)])
pages_router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.tests["playable"] = is_playable_url

IdentityStr = Annotated[
    str,
    Field(
        pattern=IDENTITY_PATTERN,
        description="Opaque identity the client chooses, e.g. 'irc:mattf'. Never parsed.",
    ),
]


class ClientJobRequest(BaseModel):
    identity: IdentityStr | None = Field(
        None, description="Identity acting for this job, or null for an anonymous order."
    )
    type: str = Field(description="Job type name.")
    params: JsonObject = Field(default_factory=dict, description="Job parameters.")


class ClientJobView(BaseModel):
    job: JobView = Field(description="The created job.")
    confirm_url: str | None = Field(None, description="Confirm this job by logging in here.")


class ClientLinkRequest(BaseModel):
    identity: IdentityStr = Field(description="Identity to link.")


class ClientLinkView(BaseModel):
    link_url: str = Field(description="One-time page to confirm the link by logging in.")


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
        existing.user_id = user.id


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
    return ClientJobView(
        job=job_view(job, job_type_for(services.registry, job.type)), confirm_url=None
    )


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
    job_type = get_job_type(services, body.type)
    ensure_available(job_type)
    try:
        params = job_type.params_model.model_validate(body.params)
    except ValidationError as error:
        detail = error.errors(include_url=False, include_context=False)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail) from error
    priced = await quote(params, services.prober)
    job = create_job(session, job_type, params, priced, None)
    job.identity = body.identity
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
    return IdentityView(
        username=user.username, free_credits=user.free_credits, paid_credits=user.paid_credits
    )


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
async def confirm_page(
    token: str, request: Request, session: Db, services: AppServices
) -> Response:
    user = await current_user(request, session)
    if user is None:
        return RedirectResponse(f"/login?next=/confirm/{token}")
    job = _job_by_confirm_token(session, token)
    job_type = job_type_for(services.registry, job.type)
    return templates.TemplateResponse(
        request,
        "confirm.html",
        {
            "job": job,
            "job_type": job_type,
            "csrf_token": csrf_token(request),
            "me": me_chip(session, services.settings, user),
        },
    )


@pages_router.post("/confirm/{token}")
async def confirm_submit(
    token: str,
    request: Request,
    session: Db,
    services: AppServices,
    csrf_token_field: Annotated[str, Form(alias="csrf_token")],
    link_account: Annotated[bool, Form()] = False,
) -> Response:
    user = await current_user(request, session)
    if user is None:
        return RedirectResponse(
            f"/login?next=/confirm/{token}", status_code=status.HTTP_303_SEE_OTHER
        )
    verify_csrf(request, csrf_token_field)
    job = _job_by_confirm_token(session, token)
    try:
        enqueue(session, job, user)
    except InsufficientCreditsError as error:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(error)) from error
    job.confirm_token_hash = None
    if link_account and job.identity:
        _link_identity(session, job.identity, user)
    session.commit()
    announce(services.bus, job)
    return RedirectResponse(f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


@pages_router.get("/link/{token}")
async def link_page(token: str, request: Request, session: Db, services: AppServices) -> Response:
    user = await current_user(request, session)
    if user is None:
        return RedirectResponse(f"/login?next=/link/{token}")
    link_request = _link_request_by_token(session, token)
    return templates.TemplateResponse(
        request,
        "link.html",
        {
            "link_request": link_request,
            "csrf_token": csrf_token(request),
            "me": me_chip(session, services.settings, user),
        },
    )


@pages_router.post("/link/{token}")
async def link_submit(
    token: str,
    request: Request,
    session: Db,
    csrf_token_field: Annotated[str, Form(alias="csrf_token")],
) -> Response:
    user = await current_user(request, session)
    if user is None:
        return RedirectResponse(f"/login?next=/link/{token}", status_code=status.HTTP_303_SEE_OTHER)
    verify_csrf(request, csrf_token_field)
    link_request = _link_request_by_token(session, token)
    _link_identity(session, link_request.identity, user)
    link_request.used = True
    session.commit()
    return RedirectResponse("/wallet", status_code=status.HTTP_303_SEE_OTHER)
