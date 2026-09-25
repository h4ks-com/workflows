from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from workflows.db import Job
from workflows.db import JobStatus
from workflows.db import utcnow
from workflows.jobtypes.catalog import JobType
from workflows.runs.jobs import queued_jobs
from workflows.runs.jobs import running_job

ROLLING_WINDOW = 20


def speed_ratio(session: Session, type_name: str) -> float | None:
    query = (
        select(Job.started_at, Job.finished_at, Job.estimate_seconds)
        .where(Job.type == type_name, Job.status == JobStatus.SUCCEEDED)
        .order_by(Job.finished_at.desc())
        .limit(ROLLING_WINDOW)
    )
    ratios = [
        (finished - started).total_seconds() / estimate
        for started, finished, estimate in session.execute(query)
        if started and finished and estimate > 0
    ]
    return sum(ratios) / len(ratios) if ratios else None


def progress_fraction(job: Job, job_type: JobType) -> float:
    total_weight = sum(step.weight for step in job_type.steps)
    weight_before = 0
    for step in job_type.steps:
        if step.name == job.progress_step:
            within = (job.progress_done or 0) / job.progress_total if job.progress_total else 0.0
            return (weight_before + step.weight * min(within, 1.0)) / total_weight
        weight_before += step.weight
    return 0.0


@dataclass(frozen=True)
class QueueSlot:
    job: Job
    position: int
    starts_in_seconds: int
    eta_seconds: int


class Estimator:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._ratios: dict[str, float | None] = {}

    def duration(self, job: Job) -> float:
        if job.type not in self._ratios:
            self._ratios[job.type] = speed_ratio(self._session, job.type)
        return job.estimate_seconds * (self._ratios[job.type] or 1.0)

    def remaining(self, job: Job) -> float:
        if job.started_at is None:
            return self.duration(job)
        elapsed = (utcnow() - job.started_at).total_seconds()
        return max(0.0, self.duration(job) - elapsed)

    def queue(self) -> tuple[QueueSlot | None, list[QueueSlot]]:
        running = running_job(self._session)
        finish_at = self.remaining(running) if running else 0.0
        running_slot = QueueSlot(running, 0, 0, round(finish_at)) if running else None
        slots = []
        for position, job in enumerate(queued_jobs(self._session), start=1):
            starts_in = finish_at
            finish_at += self.duration(job)
            slots.append(QueueSlot(job, position, round(starts_in), round(finish_at)))
        return running_slot, slots

    def slot(self, job: Job) -> QueueSlot | None:
        running_slot, slots = self.queue()
        return next((slot for slot in [running_slot, *slots] if slot and slot.job is job), None)
