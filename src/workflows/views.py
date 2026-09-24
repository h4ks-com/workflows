from datetime import datetime

from fastapi import HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from workflows.catalog import JobType, Quote
from workflows.db import Job, JobStatus, JsonObject, User
from workflows.eta import Estimator, QueueSlot, progress_fraction
from workflows.jobs import EventKind
from workflows.settings import CREDITS_PER_BEAN
from workflows.state import Services


class StepView(BaseModel):
    name: str = Field(description="Step name, as executors report it.")
    weight: int = Field(description="Share of the total work, in percent.")


class JobTypeView(BaseModel):
    name: str = Field(description="Identifier used in requests.")
    title: str = Field(description="Human readable name.")
    description: str = Field(description="What the job produces.")
    pricing: str = Field(description="How the quote is computed.")
    available: bool = Field(description="Whether the job type accepts submissions now.")
    params_schema: JsonObject = Field(description="JSON schema of the job parameters.")
    steps: list[StepView] = Field(description="Steps in execution order.")


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
    status: JobStatus = Field(description="Lifecycle status.")
    owner: str | None = Field(description="Username of the owner.")
    quote: int = Field(description="Price in credits.")
    params: JsonObject = Field(description="Job parameters.")
    progress: ProgressView = Field(description="Executor progress.")
    result: JsonObject | None = Field(description="Files and title the executor produced.")
    removed_at: datetime | None = Field(description="When an admin removed the result files.")
    error: str | None = Field(description="Why the job failed.")
    created_at: datetime = Field(description="When the job was submitted.")
    queued_at: datetime | None = Field(description="When credits were reserved.")
    started_at: datetime | None = Field(description="When the executor got the job.")
    finished_at: datetime | None = Field(description="When the job ended.")
    position: int | None = Field(None, description="Place in the queue; 0 while running.")
    starts_in_seconds: int | None = Field(None, description="Seconds until the job starts.")
    eta_seconds: int | None = Field(None, description="Seconds until the job finishes.")


class JobEventView(BaseModel):
    kind: EventKind = Field(description="Event kind: step, log, result or error.")
    data: JsonObject = Field(description="Event payload.")
    created_at: datetime = Field(description="When the executor sent it.")


class JobDetailView(JobView):
    events: list[JobEventView] = Field(description="Executor events, oldest first.")


class QueueView(BaseModel):
    paused: bool = Field(description="Whether the queue holds new jobs back.")
    running: JobView | None = Field(description="The job running now.")
    queued: list[JobView] = Field(description="Waiting jobs, first in line first.")


def get_job_or_404(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no job {job_id}")
    return job


def type_view(job_type: JobType) -> JobTypeView:
    return JobTypeView(
        name=job_type.name,
        title=job_type.title,
        description=job_type.description,
        pricing=job_type.pricing,
        available=job_type.available,
        params_schema=job_type.form.json_schema(),
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
        removed_at=job.removed_at,
        error=job.error,
        created_at=job.created_at,
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        position=slot.position if slot else None,
        starts_in_seconds=slot.starts_in_seconds if slot else None,
        eta_seconds=slot.eta_seconds if slot else None,
    )


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
    running, queued = Estimator(session).queue()
    catalog = services.catalog
    return QueueView(
        paused=services.worker.paused,
        running=job_view(running.job, catalog.find(running.job.type), running) if running else None,
        queued=[job_view(slot.job, catalog.find(slot.job.type), slot) for slot in queued],
    )


def job_views(session: Session, services: Services, user: str | None, limit: int) -> list[JobView]:
    query = select(Job).order_by(Job.id.desc()).limit(limit)
    if user is not None:
        query = query.join(Job.owner).where(User.username == user)
    return [job_view(job, services.catalog.find(job.type)) for job in session.scalars(query)]


def finished_job_views(
    session: Session, services: Services, type_name: str | None, before: int | None, limit: int
) -> list[JobView]:
    query = (
        select(Job)
        .where(Job.status == JobStatus.SUCCEEDED, Job.removed_at.is_(None))
        .order_by(Job.id.desc())
        .limit(limit)
    )
    if type_name is not None:
        query = query.where(Job.type == type_name)
    if before is not None:
        query = query.where(Job.id < before)
    return [job_view(job, services.catalog.find(job.type)) for job in session.scalars(query)]


def job_detail_view(session: Session, services: Services, job_id: int) -> JobDetailView:
    job = get_job_or_404(session, job_id)
    slot = Estimator(session).slot(job)
    view = job_view(job, services.catalog.find(job.type), slot)
    events = [
        JobEventView(kind=event.kind, data=event.data, created_at=event.created_at)
        for event in job.events
    ]
    return JobDetailView(**view.model_dump(), events=events)
