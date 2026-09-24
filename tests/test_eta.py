from datetime import timedelta

from sqlalchemy.orm import Session

from conftest import make_job
from workflows.db import utcnow
from workflows.eta import Estimator
from workflows.state import Services


def test_remaining_counts_down_from_the_start_time(session: Session, services: Services) -> None:
    job = make_job(session, services, None)
    estimator = Estimator(session, services.registry)
    assert estimator.remaining(job) == 60

    job.started_at = utcnow() - timedelta(seconds=20)
    assert 39 <= estimator.remaining(job) <= 40

    job.started_at = utcnow() - timedelta(seconds=90)
    assert estimator.remaining(job) == 0
