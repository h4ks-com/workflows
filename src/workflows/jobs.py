import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from workflows.bus import QUEUE_TOPIC, BusEvent, EventBus, job_topic
from workflows.catalog import JobType, Quote
from workflows.db import Job, JobEvent, JobStatus, JsonObject, User, utcnow
from workflows.ledger import capture, refund, reserve

STATUS_EVENT = "status"
TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})
CONFIRMATION_EXPIRY = timedelta(hours=1)
REPEATABLE_TERMINAL_EVENTS = {"result": JobStatus.SUCCEEDED, "error": JobStatus.FAILED}


class JobError(Exception):
    pass


class InvalidEventError(Exception):
    pass


type EventKind = Literal["step", "log", "result", "error"]


CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def one_line(text: str) -> str:
    # Clients relay this text into line-based protocols, where a newline starts a new command.
    return " ".join(CONTROL_CHARS.sub(" ", text).split())


OneLine = Annotated[str, AfterValidator(one_line)]


HttpLink = Annotated[str, Field(max_length=2000, pattern=r"^https?://\S+$")]


class StepEvent(BaseModel):
    kind: Literal["step"] = Field(description="Reports the step the executor is on.")
    step: OneLine = Field(
        max_length=100, description="Name of the step the executor is working on."
    )
    done: int | None = Field(None, ge=0, description="Units finished within the step.")
    total: int | None = Field(None, ge=1, description="Units in the step.")


class LogEvent(BaseModel):
    kind: Literal["log"] = Field(description="Adds a line to the run log.")
    message: OneLine = Field(max_length=500, description="Log line to show on the run page.")


class ResultFile(BaseModel):
    url: HttpLink = Field(description="Permanent bucket URL of the file.")
    name: OneLine = Field(max_length=200, description="File name.")
    mime: OneLine = Field(max_length=100, description="MIME type of the file.")


class ResultEvent(BaseModel):
    kind: Literal["result"] = Field(description="Finishes the job successfully.")
    files: list[ResultFile] = Field(max_length=20, description="Files the job produced.")
    title: OneLine | None = Field(None, max_length=200, description="Title of the result.")
    metadata_url: HttpLink | None = Field(
        None, description="URL of the metadata JSON beside the files."
    )


class StoredResult(BaseModel):
    files: list[ResultFile] = Field(description="Files the job produced.")
    metadata_url: str | None = Field(None, description="URL of the metadata JSON.")

    def urls(self) -> list[str]:
        metadata_urls = [self.metadata_url] if self.metadata_url else []
        return [file.url for file in self.files] + metadata_urls


class ErrorEvent(BaseModel):
    kind: Literal["error"] = Field(description="Fails the job and refunds it.")
    message: OneLine = Field(max_length=500, description="Why the job failed.")


ExecutorEvent = Annotated[
    StepEvent | LogEvent | ResultEvent | ErrorEvent, Field(discriminator="kind")
]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(token: str, digest: str) -> bool:
    return hmac.compare_digest(hash_token(token).encode(), digest.encode())


def ensure_available(job_type: JobType) -> None:
    if not job_type.available:
        raise JobError(f"{job_type.name} is not available yet")


def create_job(
    session: Session, job_type: JobType, params: JsonObject, quote: Quote, owner: User | None
) -> Job:
    job = Job(
        type=job_type.name,
        params=params,
        status=JobStatus.AWAITING_CONFIRMATION,
        owner=owner,
        quote=quote.credits,
        estimate_seconds=quote.estimate_seconds,
    )
    session.add(job)
    session.flush()
    return job


def enqueue(session: Session, job: Job, owner: User) -> None:
    if job.status != JobStatus.AWAITING_CONFIRMATION:
        raise JobError(f"job is {job.status}")
    job.owner = owner
    job.queued_at = utcnow()
    reserve(session, owner, job)
    job.status = JobStatus.QUEUED


def queued_jobs(session: Session) -> list[Job]:
    query = select(Job).where(Job.status == JobStatus.QUEUED).order_by(Job.queued_at, Job.id)
    return list(session.scalars(query))


def running_job(session: Session) -> Job | None:
    return session.scalars(select(Job).where(Job.status == JobStatus.RUNNING)).first()


def start(job: Job) -> str:
    token = secrets.token_urlsafe(32)
    job.status = JobStatus.RUNNING
    job.started_at = utcnow()
    job.callback_token_hash = hash_token(token)
    return token


def _finish(job: Job, status: JobStatus) -> None:
    job.status = status
    job.finished_at = utcnow()


def succeed(session: Session, job: Job, result: JsonObject) -> None:
    job.result = result
    _finish(job, JobStatus.SUCCEEDED)
    capture(session, job)


def fail(session: Session, job: Job, message: str) -> None:
    job.error = message
    _finish(job, JobStatus.FAILED)
    refund(session, job)


def cancel(session: Session, job: Job) -> None:
    if job.status in TERMINAL_STATUSES:
        raise JobError(f"job is already {job.status}")
    _finish(job, JobStatus.CANCELLED)
    refund(session, job)


def _set_progress(job: Job, event: StepEvent) -> None:
    job.progress_step = event.step
    job.progress_done = event.done
    job.progress_total = event.total


def is_terminal_repeat(job: Job, event: ExecutorEvent) -> bool:
    return REPEATABLE_TERMINAL_EVENTS.get(event.kind) == job.status


def apply_event(session: Session, job: Job, job_type: JobType, event: ExecutorEvent) -> JsonObject:
    if job.status != JobStatus.RUNNING:
        raise JobError(f"job is {job.status}")
    if isinstance(event, StepEvent) and event.step not in job_type.step_names():
        raise InvalidEventError(f"unknown step {event.step}")
    job.last_event_at = utcnow()
    data = event.model_dump(mode="json", exclude={"kind"})
    session.add(JobEvent(job_id=job.id, kind=event.kind, data=data))
    match event:
        case StepEvent():
            _set_progress(job, event)
        case ResultEvent():
            succeed(session, job, data)
        case ErrorEvent():
            fail(session, job, event.message)
        case LogEvent():
            pass
    return data


def expire_stale_confirmations(session: Session) -> list[Job]:
    cutoff = utcnow() - CONFIRMATION_EXPIRY
    query = select(Job).where(
        Job.status == JobStatus.AWAITING_CONFIRMATION, Job.created_at < cutoff
    )
    stale = list(session.scalars(query))
    for job in stale:
        _finish(job, JobStatus.CANCELLED)
    return stale


def announce(bus: EventBus, job: Job) -> None:
    event = BusEvent(STATUS_EVENT, {"job_id": job.id, "status": job.status})
    bus.publish(job_topic(job.id), event)
    bus.publish(QUEUE_TOPIC, event)
