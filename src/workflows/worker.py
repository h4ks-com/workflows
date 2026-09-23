import asyncio
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy.orm import Session, sessionmaker

from workflows.bus import EventBus
from workflows.db import Job, JobStatus, JsonObject, utcnow
from workflows.jobs import (
    announce,
    expire_stale_confirmations,
    fail,
    queued_jobs,
    running_job,
    start,
)
from workflows.jobtypes import JobType
from workflows.settings import Settings

POLL_SECONDS = 1.0
DISPATCH_TIMEOUT_SECONDS = 30.0
EXECUTOR_AUTH_HEADER = "X-API-Key"


@dataclass(frozen=True)
class Dispatch:
    job_id: int
    url: str
    payload: JsonObject


class QueueWorker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        registry: dict[str, JobType],
        bus: EventBus,
        http: httpx.AsyncClient,
        settings: Settings,
    ) -> None:
        self._sessions = sessions
        self._registry = registry
        self._bus = bus
        self._http = http
        self._settings = settings
        self.paused = False

    async def run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(POLL_SECONDS)

    async def tick(self) -> None:
        with self._sessions.begin() as session:
            expired = expire_stale_confirmations(session)
        for job in expired:
            announce(self._bus, job)
        with self._sessions.begin() as session:
            running = running_job(session)
            if running is not None:
                self._fail_if_silent(session, running)
                return
            dispatch = None if self.paused else self._start_next(session)
        if dispatch is not None:
            await self._dispatch(dispatch)

    def _fail_if_silent(self, session: Session, job: Job) -> None:
        silence = timedelta(seconds=self._settings.executor_timeout_factor * job.estimate_seconds)
        if job.last_event_at and utcnow() - job.last_event_at > silence:
            fail(session, job, f"the executor sent no update for {round(silence.total_seconds())}s")
            announce(self._bus, job)

    def _start_next(self, session: Session) -> Dispatch | None:
        job = next(iter(queued_jobs(session)), None)
        if job is None:
            return None
        token = start(job)
        announce(self._bus, job)
        payload: JsonObject = {
            "job_id": job.id,
            "type": job.type,
            "params": job.params,
            "callback_url": f"{self._settings.base_url}/api/jobs/{job.id}/events",
            "callback_token": token,
        }
        return Dispatch(job.id, self._registry[job.type].executor_url, payload)

    async def _dispatch(self, dispatch: Dispatch) -> None:
        try:
            response = await self._http.post(
                dispatch.url,
                json=dispatch.payload,
                headers={EXECUTOR_AUTH_HEADER: self._settings.executor_token},
                timeout=DISPATCH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            self._fail_dispatch(dispatch.job_id, f"the executor refused the job: {error}")

    def _fail_dispatch(self, job_id: int, message: str) -> None:
        with self._sessions.begin() as session:
            job = session.get_one(Job, job_id)
            if job.status == JobStatus.RUNNING:
                fail(session, job, message)
                announce(self._bus, job)
