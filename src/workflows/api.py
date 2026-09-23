from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from workflows.auth import CurrentUser, LoggedInUser, bearer_token, is_admin, is_service
from workflows.bus import BusEvent, job_topic
from workflows.db import Job, JobStatus, JsonObject, User
from workflows.eta import Estimator, QueueSlot, progress_fraction
from workflows.jobs import (
    ExecutorEvent,
    announce,
    apply_event,
    cancel,
    create_job,
    enqueue,
    ensure_available,
    is_terminal_repeat,
    job_type_for,
    token_matches,
)
from workflows.jobtypes import JobParams, JobType, Quote, quote
from workflows.settings import CREDITS_PER_BEAN
from workflows.state import AppServices, Db, Services

DEFAULT_JOB_LIMIT = 20
MAX_JOB_LIMIT = 100

router = APIRouter(prefix="/api")


class StepView(BaseModel):
    name: str = Field(description="Step name, as executors report it.")
    weight: int = Field(description="Share of the total work, in percent.")


class JobTypeView(BaseModel):
    name: str = Field(description="Identifier used in requests.")
    title: str = Field(description="Human readable name.")
    description: str = Field(description="What the job produces.")
    pricing: str = Field(description="How the quote is computed.")
    available: bool = Field(description="Whether the job type accepts orders now.")
    params_schema: JsonObject = Field(description="JSON schema of the job parameters.")
    steps: list[StepView] = Field(description="Steps in execution order.")


class QuoteRequest(BaseModel):
    type: str = Field(description="Job type name.")
    params: JsonObject = Field(default_factory=dict, description="Job parameters.")


class SubmitRequest(QuoteRequest):
    on_behalf_of: str | None = Field(
        None, description="Username that owns the job. Only valid with the service token."
    )


class ProbeView(BaseModel):
    duration_seconds: float = Field(description="Duration of the input media.")
    title: str = Field(description="Title of the input media.")


class QuoteView(BaseModel):
    credits: int = Field(description="Fixed price of the job.")
    beans: int = Field(description="Beans needed to buy the credits, rounded up.")
    probe: ProbeView | None = Field(description="Input media metadata, when the job has one.")
    estimate_seconds: int = Field(description="Expected run time.")


class ProgressView(BaseModel):
    step: str | None = Field(description="Step the executor reported last.")
    done: int | None = Field(description="Units finished within the step.")
    total: int | None = Field(description="Units in the step.")
    fraction: float = Field(description="Overall progress from 0 to 1, by step weight.")


class JobView(BaseModel):
    id: int = Field(description="Job id.")
    type: str = Field(description="Job type name.")
    status: str = Field(description="Lifecycle status.")
    owner: str | None = Field(description="Username of the owner.")
    quote: int = Field(description="Price in credits.")
    params: JsonObject = Field(description="Job parameters.")
    progress: ProgressView = Field(description="Executor progress.")
    result: JsonObject | None = Field(description="Files and title the executor produced.")
    error: str | None = Field(description="Why the job failed.")
    created_at: datetime = Field(description="When the job was ordered.")
    queued_at: datetime | None = Field(description="When credits were reserved.")
    started_at: datetime | None = Field(description="When the executor got the job.")
    finished_at: datetime | None = Field(description="When the job ended.")
    position: int | None = Field(None, description="Place in the queue; 0 while running.")
    starts_in_seconds: int | None = Field(None, description="Seconds until the job starts.")
    eta_seconds: int | None = Field(None, description="Seconds until the job finishes.")


class JobEventView(BaseModel):
    kind: str = Field(description="Event kind: step, log, result or error.")
    data: JsonObject = Field(description="Event payload.")
    created_at: datetime = Field(description="When the executor sent it.")


class JobDetailView(JobView):
    events: list[JobEventView] = Field(description="Executor events, oldest first.")


class QueueView(BaseModel):
    paused: bool = Field(description="Whether the queue holds new jobs back.")
    running: JobView | None = Field(description="The job running now.")
    queued: list[JobView] = Field(description="Waiting jobs, first in line first.")


def type_view(job_type: JobType) -> JobTypeView:
    return JobTypeView(
        name=job_type.name,
        title=job_type.title,
        description=job_type.description,
        pricing=job_type.pricing,
        available=job_type.available,
        params_schema=job_type.params_model.model_json_schema(),
        steps=[StepView(name=step.name, weight=step.weight) for step in job_type.steps],
    )


def job_view(job: Job, job_type: JobType, slot: QueueSlot | None = None) -> JobView:
    progress = ProgressView(
        step=job.progress_step,
        done=job.progress_done,
        total=job.progress_total,
        fraction=1.0 if job.status == JobStatus.SUCCEEDED else progress_fraction(job, job_type),
    )
    return JobView(
        id=job.id,
        type=job.type,
        status=job.status,
        owner=job.owner.username if job.owner else None,
        quote=job.quote,
        params=job.params,
        progress=progress,
        result=job.result,
        error=job.error,
        created_at=job.created_at,
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        position=slot.position if slot else None,
        starts_in_seconds=slot.starts_in_seconds if slot else None,
        eta_seconds=slot.eta_seconds if slot else None,
    )


def get_job_type(services: Services, name: str) -> JobType:
    job_type = services.registry.get(name)
    if job_type is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no job type named {name}")
    return job_type


def get_job_or_404(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no job {job_id}")
    return job


async def price_request(
    services: Services, request: QuoteRequest
) -> tuple[JobType, JobParams, Quote]:
    job_type = get_job_type(services, request.type)
    ensure_available(job_type)
    try:
        params = job_type.params_model.model_validate(request.params)
    except ValidationError as error:
        detail = error.errors(include_url=False, include_context=False)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail) from error
    return job_type, params, await quote(params, services.prober)


def quote_view(priced: Quote) -> QuoteView:
    probe = priced.probe
    return QuoteView(
        credits=priced.credits,
        beans=-(-priced.credits // CREDITS_PER_BEAN),
        probe=ProbeView(duration_seconds=probe.duration_seconds, title=probe.title)
        if probe
        else None,
        estimate_seconds=priced.estimate_seconds,
    )


def queue_view(session: Session, services: Services) -> QueueView:
    running, queued = Estimator(session, services.registry).queue()
    registry = services.registry
    return QueueView(
        paused=services.worker.paused,
        running=job_view(running.job, job_type_for(registry, running.job.type), running)
        if running
        else None,
        queued=[job_view(slot.job, job_type_for(registry, slot.job.type), slot) for slot in queued],
    )


def job_views(session: Session, services: Services, user: str | None, limit: int) -> list[JobView]:
    query = select(Job).order_by(Job.id.desc()).limit(limit)
    if user is not None:
        query = query.join(Job.owner).where(User.username == user)
    return [
        job_view(job, job_type_for(services.registry, job.type)) for job in session.scalars(query)
    ]


def job_detail_view(session: Session, services: Services, job_id: int) -> JobDetailView:
    job = get_job_or_404(session, job_id)
    slot = Estimator(session, services.registry).slot(job)
    view = job_view(job, job_type_for(services.registry, job.type), slot)
    events = [
        JobEventView(kind=event.kind, data=event.data, created_at=event.created_at)
        for event in job.events
    ]
    return JobDetailView(**view.model_dump(), events=events)


def _user_named(session: Db, username: str | None) -> User:
    if username is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "on_behalf_of is required with the service token"
        )
    user = session.scalar(select(User).where(User.username == username))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no user named {username}")
    return user


def _submitter(
    request: Request, services: Services, session: Db, user: User | None, body: SubmitRequest
) -> User:
    if is_service(request, services.settings):
        return _user_named(session, body.on_behalf_of)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "log in first")
    return user


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
    return [type_view(job_type) for job_type in services.registry.values()]


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
    body: SubmitRequest, request: Request, user: CurrentUser, session: Db, services: AppServices
) -> JobView:
    owner = _submitter(request, services, session, user, body)
    job_type, params, priced = await price_request(services, body)
    job = create_job(session, job_type, params, priced, owner)
    enqueue(session, job, owner)
    session.commit()
    announce(services.bus, job)
    return job_view(job, job_type, Estimator(session, services.registry).slot(job))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: int, user: LoggedInUser, session: Db, services: AppServices
) -> JobView:
    job = get_job_or_404(session, job_id)
    _ensure_can_cancel(job, user, services)
    cancel(session, job)
    session.commit()
    announce(services.bus, job)
    return job_view(job, job_type_for(services.registry, job.type))


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
    data = apply_event(session, job, job_type_for(services.registry, job.type), event)
    session.commit()
    services.bus.publish(job_topic(job.id), BusEvent(event.kind, data))
    if job.status != JobStatus.RUNNING:
        announce(services.bus, job)
