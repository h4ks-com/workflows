import json
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from conftest import BASE_URL, EXECUTOR_TOKEN, SONG_EXECUTOR, make_user, queue_job
from workflows.bus import QUEUE_TOPIC
from workflows.db import JobStatus, utcnow
from workflows.jobs import hash_token, start
from workflows.state import Services


@respx.mock
async def test_tick_dispatches_the_oldest_queued_job(session: Session, services: Services) -> None:
    route = respx.post(SONG_EXECUTOR).respond(202)
    user = make_user(session, "alice")
    first = queue_job(session, services, user)
    second = queue_job(session, services, user)

    with services.bus.subscribe(QUEUE_TOPIC) as queue:
        await services.worker.tick()
        await services.worker.tick()
        announced = queue.get_nowait()

    session.expire_all()
    assert (first.status, second.status) == (JobStatus.RUNNING, JobStatus.QUEUED)
    assert announced.data == {"job_id": first.id, "status": JobStatus.RUNNING}
    request = route.calls.last.request
    payload = json.loads(request.content)
    assert request.headers["X-API-Key"] == EXECUTOR_TOKEN
    assert payload["callback_url"] == f"{BASE_URL}/api/jobs/{first.id}/events"
    assert payload["params"] == first.params
    assert first.callback_token_hash == hash_token(payload["callback_token"])
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize("failure", [httpx.Response(500), httpx.ConnectError("down")])
async def test_refused_dispatch_fails_and_refunds(
    session: Session, services: Services, failure: httpx.Response | Exception
) -> None:
    respx.post(SONG_EXECUTOR).mock(side_effect=[failure])
    user = make_user(session, "alice")
    job = queue_job(session, services, user)

    await services.worker.tick()

    session.expire_all()
    assert job.status == JobStatus.FAILED
    assert job.error is not None
    assert job.error.startswith("the executor refused the job")
    assert user.free_credits == 500


async def test_paused_worker_leaves_the_queue_alone(session: Session, services: Services) -> None:
    job = queue_job(session, services, make_user(session, "alice"))
    services.worker.paused = True

    await services.worker.tick()

    session.expire_all()
    assert job.status == JobStatus.QUEUED


async def test_silent_running_job_times_out(session: Session, services: Services) -> None:
    user = make_user(session, "alice")
    job = queue_job(session, services, user)
    start(job)
    session.commit()

    await services.worker.tick()
    session.expire_all()
    assert job.status == JobStatus.RUNNING

    job.last_event_at = utcnow() - timedelta(seconds=3 * job.estimate_seconds + 1)
    session.commit()
    await services.worker.tick()

    session.expire_all()
    assert job.status == JobStatus.FAILED
    assert job.error == "the executor sent no update for 180s"
    assert user.free_credits == 500
