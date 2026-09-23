from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from workflows.auth import require_admin, require_fetch_header
from workflows.db import Job, User
from workflows.jobs import announce, cancel, job_type_for
from workflows.ledger import adjust
from workflows.state import AppServices, Db, Services
from workflows.views import JobView, get_job_or_404, job_view

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


def _user_or_404(session: Session, username: str) -> User:
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
    user = _user_or_404(session, username)
    adjust(session, user, body.credits, body.note)
    session.commit()
    return user


def set_paused(services: Services, paused: bool) -> PauseView:
    services.worker.paused = paused
    return PauseView(paused=paused)


def health(services: Services) -> HealthView:
    return HealthView(
        executors={name: job_type.available for name, job_type in services.registry.items()},
        worker_paused=services.worker.paused,
        worker_alive=services.worker.alive,
        beans_poller_last_success=services.beans_poller.last_success
        if services.beans_poller
        else None,
    )


@router.post("/jobs/{job_id}/cancel")
async def admin_cancel_job(job_id: int, session: Db, services: AppServices) -> JobView:
    job = cancel_job(session, services, job_id)
    return job_view(job, job_type_for(services.registry, job.type))


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
