import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from conftest import BASE_URL
from conftest import EXECUTOR_TOKEN
from conftest import SONG_EXECUTOR
from conftest import assert_ledger_matches
from conftest import make_user
from conftest import queue_job
from workflows.db import Job
from workflows.db import JobStatus
from workflows.db import utcnow
from workflows.runs.bus import QUEUE_TOPIC
from workflows.runs.bus import EventBus
from workflows.runs.holds import hold_queue
from workflows.runs.jobs import hash_token
from workflows.runs.jobs import start
from workflows.runs.worker import REFUSED_ERROR
from workflows.runs.worker import RETIRED_ERROR
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
    assert payload["steps"] == ["write", "generate", "store"]
    assert first.callback_token_hash == hash_token(payload["callback_token"])
    assert route.call_count == 1


@respx.mock
async def test_tick_announces_after_commit(
    session: Session, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.post(SONG_EXECUTOR).respond(202)
    queue_job(session, services, make_user(session, "alice"))
    seen: list[str] = []

    def read_committed_status(bus: EventBus, announced: Job) -> None:
        with services.sessions() as other:
            seen.append(other.get_one(Job, announced.id).status)

    monkeypatch.setattr("workflows.runs.worker.announce", read_committed_status)

    await services.worker.tick()

    assert seen == [JobStatus.RUNNING]


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
    assert (job.status, job.error) == (JobStatus.FAILED, REFUSED_ERROR)
    assert user.free_credits == 500
    assert_ledger_matches(session, user)


@respx.mock
async def test_dispatch_timeout_leaves_the_job_for_the_watchdog(
    session: Session, services: Services
) -> None:
    respx.post(SONG_EXECUTOR).mock(side_effect=httpx.ReadTimeout("slow"))
    job = queue_job(session, services, make_user(session, "alice"))

    await services.worker.tick()

    session.expire_all()
    assert job.status == JobStatus.RUNNING


async def test_queued_job_of_a_retired_type_fails_and_refunds(
    session: Session, services: Services
) -> None:
    user = make_user(session, "alice")
    job = queue_job(session, services, user)
    job.type = "karaoke"
    session.commit()

    await services.worker.tick()

    session.expire_all()
    assert (job.status, job.error) == (JobStatus.FAILED, RETIRED_ERROR)
    assert_ledger_matches(session, user)
    assert user.free_credits == 500


async def test_held_queue_starts_no_job(session: Session, services: Services) -> None:
    job = queue_job(session, services, make_user(session, "alice"))
    hold_queue(session, "gpu work", None)
    session.commit()

    await services.worker.tick()

    session.expire_all()
    assert job.status == JobStatus.QUEUED


@respx.mock
async def test_expired_hold_lets_the_queue_run(session: Session, services: Services) -> None:
    respx.post(SONG_EXECUTOR).respond(202)
    job = queue_job(session, services, make_user(session, "alice"))
    hold = hold_queue(session, "short break", 1)
    hold.until = utcnow() - timedelta(seconds=1)
    session.commit()

    await services.worker.tick()

    session.expire_all()
    assert job.status == JobStatus.RUNNING


async def test_silent_running_job_times_out(session: Session, services: Services) -> None:
    user = make_user(session, "alice")
    job = queue_job(session, services, user)
    start(job)
    job.last_event_at = utcnow()
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
    assert_ledger_matches(session, user)


async def test_running_job_without_a_first_event_fails_after_the_deadline(
    session: Session, services: Services
) -> None:
    user = make_user(session, "alice")
    job = queue_job(session, services, user)
    start(job)
    session.commit()

    await services.worker.tick()
    session.expire_all()
    assert job.status == JobStatus.RUNNING

    job.started_at = utcnow() - timedelta(seconds=121)
    session.commit()
    await services.worker.tick()

    session.expire_all()
    assert (job.status, job.error) == (
        JobStatus.FAILED,
        "the executor did not start the job within 120s",
    )
    assert_ledger_matches(session, user)


async def test_run_survives_database_errors(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    tick = AsyncMock(
        side_effect=[OperationalError("tick", {}, Exception()), asyncio.CancelledError]
    )
    monkeypatch.setattr(services.worker, "tick", tick)
    monkeypatch.setattr("workflows.runs.worker.asyncio.sleep", AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await services.worker.run()

    assert tick.await_count == 2


async def test_worker_is_alive_only_while_its_task_runs(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services.worker, "run", AsyncMock())
    assert not services.worker.alive

    task = services.worker.start()
    assert services.worker.alive
    await task

    assert not services.worker.alive
