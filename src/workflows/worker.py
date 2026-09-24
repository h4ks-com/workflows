import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from workflows.bus import EventBus
from workflows.catalog import Catalog, DispatchRequest, Executor
from workflows.db import Job, JobStatus, utcnow
from workflows.jobs import (
    announce,
    expire_stale_confirmations,
    fail,
    queued_jobs,
    running_job,
    start,
)
from workflows.settings import Settings

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0
REFUSED_ERROR = "the executor refused the job"
RETIRED_ERROR = "this job type is no longer offered"


@dataclass(frozen=True)
class Dispatch:
    executor: Executor
    request: DispatchRequest


class QueueWorker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        catalog: Catalog,
        bus: EventBus,
        settings: Settings,
    ) -> None:
        self._sessions = sessions
        self._catalog = catalog
        self._bus = bus
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self.paused = False

    def start(self) -> asyncio.Task[None]:
        self._task = asyncio.create_task(self.run(), name="queue worker")
        return self._task

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except SQLAlchemyError:
                logger.exception("queue worker tick failed")
            await asyncio.sleep(POLL_SECONDS)

    async def tick(self) -> None:
        dispatch = None
        with self._sessions.begin() as session:
            changed = expire_stale_confirmations(session)
            running = running_job(session)
            if running is not None:
                changed += self._fail_if_silent(session, running)
            elif not self.paused:
                started, dispatch = self._start_next(session)
                changed += started
        for job in changed:
            announce(self._bus, job)
        if dispatch is not None:
            await self._dispatch(dispatch)

    def _silence_error(self, job: Job) -> str | None:
        now = utcnow()
        if job.last_event_at is None:
            deadline = timedelta(seconds=self._settings.first_event_timeout_seconds)
            if job.started_at and now - job.started_at > deadline:
                return (
                    f"the executor did not start the job within {round(deadline.total_seconds())}s"
                )
            return None
        silence = timedelta(seconds=self._settings.executor_timeout_factor * job.estimate_seconds)
        if now - job.last_event_at > silence:
            return f"the executor sent no update for {round(silence.total_seconds())}s"
        return None

    def _fail_if_silent(self, session: Session, job: Job) -> list[Job]:
        error = self._silence_error(job)
        if error is None:
            return []
        fail(session, job, error)
        return [job]

    def _start_next(self, session: Session) -> tuple[list[Job], Dispatch | None]:
        job = next(iter(queued_jobs(session)), None)
        if job is None:
            return [], None
        job_type = self._catalog.find(job.type)
        if job_type.executor is None:
            fail(session, job, RETIRED_ERROR)
            return [job], None
        request = DispatchRequest(
            job_id=job.id,
            type=job.type,
            params=job.params,
            steps=job_type.step_names(),
            callback_url=f"{self._settings.base_url}/api/jobs/{job.id}/events",
            callback_token=start(job),
        )
        return [job], Dispatch(job_type.executor, request)

    async def _dispatch(self, dispatch: Dispatch) -> None:
        if await dispatch.executor.dispatch(dispatch.request) == "refused":
            self._fail_dispatch(dispatch.request.job_id)

    def _fail_dispatch(self, job_id: int) -> None:
        with self._sessions.begin() as session:
            job = session.get_one(Job, job_id)
            if job.status != JobStatus.RUNNING:
                return
            fail(session, job, REFUSED_ERROR)
        announce(self._bus, job)
