from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, ValidationError

from workflows.accounts.auth import LoggedInUser, bearer_token, is_admin, require_fetch_header
from workflows.api.views import (
    JobDetailView,
    JobTypeView,
    JobView,
    QueueView,
    QuoteView,
    get_job_or_404,
    job_detail_view,
    job_view,
    job_views,
    queue_view,
    quote_view,
    type_view,
)
from workflows.db import Job, JobStatus, JsonObject, User
from workflows.jobtypes.catalog import JobType, Quote
from workflows.runs.bus import BusEvent, job_topic
from workflows.runs.eta import Estimator
from workflows.runs.jobs import (
    ExecutorEvent,
    announce,
    apply_event,
    cancel,
    create_job,
    enqueue,
    ensure_available,
    is_terminal_repeat,
    token_matches,
)
from workflows.state import AppServices, Db, Services

DEFAULT_JOB_LIMIT = 20
MAX_JOB_LIMIT = 100

router = APIRouter(prefix="/api", dependencies=[Depends(require_fetch_header)])


class QuoteRequest(BaseModel):
    type: str = Field(description="Job type name.")
    params: JsonObject = Field(default_factory=dict, description="Job parameters.")


def get_job_type(services: Services, name: str) -> JobType:
    job_type = services.catalog.get(name)
    if job_type is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no job type named {name}")
    return job_type


async def price_request(
    services: Services, request: QuoteRequest
) -> tuple[JobType, JsonObject, Quote]:
    job_type = get_job_type(services, request.type)
    ensure_available(job_type)
    try:
        params = job_type.validate(request.params)
    except ValidationError as error:
        detail = error.errors(include_url=False, include_context=False)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail) from error
    return job_type, params, await job_type.quote(params, services.prober)


def _ensure_can_cancel(job: Job, user: User, services: Services) -> None:
    if is_admin(user, services.settings):
        return
    if job.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the owner or an admin can cancel")
    if job.status != JobStatus.QUEUED:
        raise HTTPException(status.HTTP_409_CONFLICT, "only queued jobs can be cancelled")


def _ensure_callback_token(request: Request, job: Job) -> None:
    token = bearer_token(request)
    if not (token and job.callback_token_hash and token_matches(token, job.callback_token_hash)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid callback token")


@router.get("/types")
async def list_types(services: AppServices) -> list[JobTypeView]:
    return [type_view(job_type) for job_type in services.catalog.all()]


@router.post("/quote")
async def quote_job(body: QuoteRequest, services: AppServices) -> QuoteView:
    _, _, priced = await price_request(services, body)
    return quote_view(priced)


@router.get("/queue")
async def get_queue(session: Db, services: AppServices) -> QueueView:
    return queue_view(session, services)


@router.get("/jobs")
async def list_jobs(
    session: Db,
    services: AppServices,
    user: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_JOB_LIMIT)] = DEFAULT_JOB_LIMIT,
) -> list[JobView]:
    return job_views(session, services, user, limit)


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, session: Db, services: AppServices) -> JobDetailView:
    return job_detail_view(session, services, job_id)


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def submit_job(
    body: QuoteRequest, user: LoggedInUser, session: Db, services: AppServices
) -> JobView:
    job_type, params, priced = await price_request(services, body)
    job = create_job(session, job_type, params, priced, user)
    enqueue(session, job, user)
    session.commit()
    announce(services.bus, job)
    return job_view(job, job_type, Estimator(session).slot(job))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: int, user: LoggedInUser, session: Db, services: AppServices
) -> JobView:
    job = get_job_or_404(session, job_id)
    _ensure_can_cancel(job, user, services)
    cancel(session, job)
    session.commit()
    announce(services.bus, job)
    return job_view(job, services.catalog.find(job.type))


@router.post("/jobs/{job_id}/events", status_code=status.HTTP_204_NO_CONTENT)
async def executor_event(
    job_id: int,
    event: Annotated[ExecutorEvent, Body()],
    request: Request,
    session: Db,
    services: AppServices,
) -> None:
    job = get_job_or_404(session, job_id)
    _ensure_callback_token(request, job)
    if is_terminal_repeat(job, event):
        return
    data = apply_event(session, job, services.catalog.find(job.type), event)
    session.commit()
    services.bus.publish(job_topic(job.id), BusEvent(event.kind, data))
    if job.status != JobStatus.RUNNING:
        announce(services.bus, job)
