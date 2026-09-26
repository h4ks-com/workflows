import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from workflows.db import Job
from workflows.db import JobStatus
from workflows.db import JsonObject
from workflows.db import utcnow
from workflows.jobtypes.catalog import Catalog
from workflows.jobtypes.catalog import DispatchRequest
from workflows.jobtypes.catalog import Executor
from workflows.jobtypes.catalog import JobType
from workflows.runs.bus import EventBus
from workflows.runs.holds import active_hold
from workflows.runs.jobs import announce
from workflows.runs.jobs import expire_stale_confirmations
from workflows.runs.jobs import fail
from workflows.runs.jobs import note
from workflows.runs.jobs import queued_jobs
from workflows.runs.jobs import running_job
from workflows.runs.jobs import start
from workflows.runs.media import MediaStager
from workflows.runs.media import StagedMedia
from workflows.runs.media import StagingError
from workflows.runs.media import media_fields
from workflows.settings import Settings

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0
REFUSED_ERROR = "the executor refused the job"
RETIRED_ERROR = "this job type is no longer offered"


@dataclass(frozen=True)
class Dispatch:
    executor: Executor
    request: DispatchRequest


@dataclass(frozen=True)
class Candidate:
    """The next job to start, with the audio links we download before dispatching it."""

    job_id: int
    links: dict[str, str]


@dataclass(frozen=True)
class Staged:
    media: dict[str, StagedMedia]
    error: str | None = None


def _minutes(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes, rest = divmod(round(seconds), 60)
    return f" ({minutes}:{rest:02d})"


def _dispatch_params(params: JsonObject, staged: Staged) -> tuple[JsonObject, JsonObject]:
    urls: JsonObject = {name: media.url for name, media in staged.media.items()}
    details: JsonObject = {
        name: {"title": media.title, "duration": media.duration_seconds}
        for name, media in staged.media.items()
    }
    return {**params, **urls}, details


class QueueWorker:
    """Runs one job at a time and fails a running job that goes silent."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        catalog: Catalog,
        bus: EventBus,
        settings: Settings,
        stager: MediaStager | None = None,
    ) -> None:
        self._sessions = sessions
        self._stager = stager
        self._catalog = catalog
        self._bus = bus
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

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
        candidate = None
        with self._sessions.begin() as session:
            changed = expire_stale_confirmations(session)
            running = running_job(session)
            if running is not None:
                changed += self._fail_if_silent(session, running)
            elif active_hold(session) is None:
                candidate = self._next_candidate(session, changed)
        for job in changed:
            announce(self._bus, job)
        if candidate is None:
            return
        dispatch = self._start(candidate, await self._stage(candidate))
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

    def _next_candidate(self, session: Session, changed: list[Job]) -> Candidate | None:
        job = next(iter(queued_jobs(session)), None)
        if job is None:
            return None
        job_type = self._catalog.find(job.type)
        if job_type.executor is None:
            fail(session, job, RETIRED_ERROR)
            changed.append(job)
            return None
        fields = media_fields(job_type.form) if self._stager else []
        links = {name: str(job.params[name]) for name in fields if job.params.get(name)}
        for link in links.values():
            note(session, job, f"downloading {link}")
        return Candidate(job.id, links)

    async def _stage(self, candidate: Candidate) -> Staged:
        media: dict[str, StagedMedia] = {}
        if self._stager is None:
            return Staged(media)
        for name, link in candidate.links.items():
            try:
                media[name] = await self._stager.stage(f"{candidate.job_id}/{name}", link)
            except StagingError as error:
                return Staged(media, str(error))
        return Staged(media)

    def _start(self, candidate: Candidate, staged: Staged) -> Dispatch | None:
        with self._sessions.begin() as session:
            job = session.get_one(Job, candidate.job_id)
            if job.status != JobStatus.QUEUED:
                return None
            if staged.error is not None:
                fail(session, job, staged.error)
                dispatch = None
            else:
                dispatch = self._dispatch_for(session, job, self._catalog.find(job.type), staged)
        announce(self._bus, job)
        return dispatch

    def _dispatch_for(
        self, session: Session, job: Job, job_type: JobType, staged: Staged
    ) -> Dispatch | None:
        if job_type.executor is None:
            fail(session, job, RETIRED_ERROR)
            return None
        for media in staged.media.values():
            note(
                session,
                job,
                f"downloaded {media.title or 'the file'}{_minutes(media.duration_seconds)}",
            )
        params, details = _dispatch_params(job.params, staged)
        request = DispatchRequest(
            job_id=job.id,
            type=job.type,
            params=params,
            steps=job_type.step_names(),
            callback_url=f"{self._settings.base_url}/api/jobs/{job.id}/events",
            callback_token=start(job),
            media=details,
        )
        return Dispatch(job_type.executor, request)

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
