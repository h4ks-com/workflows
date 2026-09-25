from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from workflows.accounts.auth import require_admin
from workflows.accounts.auth import require_fetch_header
from workflows.accounts.ledger import adjust
from workflows.api.views import JobView
from workflows.api.views import get_job_or_404
from workflows.api.views import job_view
from workflows.db import Job
from workflows.db import JobEvent
from workflows.db import User
from workflows.db import utcnow
from workflows.runs.jobs import StoredResult
from workflows.runs.jobs import announce
from workflows.runs.jobs import cancel
from workflows.state import AppServices
from workflows.state import Db
from workflows.state import Services
from workflows.storage import object_location

REMOVED_MESSAGE = "removed by an admin"
STORAGE_NOT_CONFIGURED = "storage is not configured"

router = APIRouter(
    prefix="/api/admin", dependencies=[Depends(require_fetch_header), Depends(require_admin)]
)


class AdjustCreditsRequest(BaseModel):
    credits: int = Field(description="Credit delta to apply. Negative deducts.")
    note: str = Field(min_length=1, description="Why the adjustment was made.")


class AdjustedBalanceView(BaseModel):
    username: str = Field(description="User whose balance changed.")
    free_credits: int = Field(description="Daily free credits left today.")
    paid_credits: int = Field(description="Credits bought with beans.")


class PauseView(BaseModel):
    paused: bool = Field(description="Whether the queue holds new jobs back.")


class HealthView(BaseModel):
    executors: dict[str, bool] = Field(description="Whether each job type has an executor.")
    worker_paused: bool = Field(description="Whether the queue is paused.")
    worker_alive: bool = Field(description="Whether the queue worker task is running.")
    beans_poller_last_success: datetime | None = Field(
        description="When the beans poller last read transactions successfully."
    )
    skipped_workflows: dict[str, str] = Field(
        description="Workflows a provider could not turn into a job type, with the reason."
    )


def user_or_404(session: Session, username: str) -> User:
    user = session.scalar(select(User).where(User.username == username))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no user named {username}")
    return user


def cancel_job(session: Session, services: Services, job_id: int) -> Job:
    job = get_job_or_404(session, job_id)
    cancel(session, job)
    session.commit()
    announce(services.bus, job)
    return job


def adjust_credits(session: Session, username: str, body: AdjustCreditsRequest) -> User:
    user = user_or_404(session, username)
    adjust(session, user, body.credits, body.note)
    session.commit()
    return user


def set_paused(services: Services, paused: bool) -> PauseView:
    services.worker.paused = paused
    return PauseView(paused=paused)


async def remove_job_files(session: Session, services: Services, job_id: int) -> Job:
    if services.storage is None:
        raise HTTPException(status.HTTP_409_CONFLICT, STORAGE_NOT_CONFIGURED)
    job = get_job_or_404(session, job_id)
    stored = StoredResult.model_validate(job.result) if job.result else StoredResult(files=[])
    for url in stored.urls():
        located = object_location(services.settings.minio_endpoint, url)
        if located is not None:
            await services.storage.remove(*located)
    job.result = None
    job.removed_at = utcnow()
    for event in job.events:
        if event.kind == "result":
            event.data = {}
    session.add(JobEvent(job_id=job.id, kind="log", data={"message": REMOVED_MESSAGE}))
    session.commit()
    announce(services.bus, job)
    return job


def health(services: Services) -> HealthView:
    return HealthView(
        executors={job_type.name: job_type.available for job_type in services.catalog.all()},
        worker_paused=services.worker.paused,
        worker_alive=services.worker.alive,
        beans_poller_last_success=services.beans_poller.last_success
        if services.beans_poller
        else None,
        skipped_workflows=services.catalog.skipped(),
    )


@router.post("/jobs/{job_id}/cancel")
async def admin_cancel_job(job_id: int, session: Db, services: AppServices) -> JobView:
    job = cancel_job(session, services, job_id)
    return job_view(job, services.catalog.find(job.type))


@router.post("/jobs/{job_id}/remove")
async def admin_remove_job(job_id: int, session: Db, services: AppServices) -> JobView:
    job = await remove_job_files(session, services, job_id)
    return job_view(job, services.catalog.find(job.type))


@router.post("/users/{username}/credits")
async def admin_adjust_credits(
    username: str, body: AdjustCreditsRequest, session: Db
) -> AdjustedBalanceView:
    user = adjust_credits(session, username, body)
    return AdjustedBalanceView(
        username=user.username, free_credits=user.free_credits, paid_credits=user.paid_credits
    )


@router.post("/queue/pause")
async def admin_pause_queue(services: AppServices) -> PauseView:
    return set_paused(services, True)


@router.post("/queue/resume")
async def admin_resume_queue(services: AppServices) -> PauseView:
    return set_paused(services, False)


@router.get("/health")
async def admin_health(services: AppServices) -> HealthView:
    return health(services)
