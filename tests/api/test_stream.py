import asyncio

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session

from conftest import make_user
from conftest import queue_job
from workflows.api.stream import _job_events
from workflows.api.stream import _queue_events
from workflows.api.views import QueueView
from workflows.api.views import queue_view
from workflows.db import JobStatus
from workflows.runs.bus import QUEUE_TOPIC
from workflows.runs.bus import BusEvent
from workflows.runs.bus import job_topic
from workflows.runs.jobs import start
from workflows.state import Services


def _kinds(body: str) -> list[str]:
    return [line.removeprefix("event: ") for line in body.splitlines() if line.startswith("event:")]


async def test_job_stream_snapshot_then_events_then_closes_on_terminal(
    app: FastAPI, services: Services, session: Session
) -> None:
    user = make_user(session, "alice")
    job = queue_job(session, services, user)
    token = start(job)
    session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        task = asyncio.create_task(client.get(f"/api/jobs/{job.id}/stream"))
        await asyncio.sleep(0.05)
        headers = {"Authorization": f"Bearer {token}"}
        await client.post(
            f"/api/jobs/{job.id}/events", json={"kind": "log", "message": "hi"}, headers=headers
        )
        await client.post(
            f"/api/jobs/{job.id}/events",
            json={"kind": "result", "files": [], "title": "t"},
            headers=headers,
        )
        response = await asyncio.wait_for(task, timeout=2)

    assert response.status_code == 200
    assert _kinds(response.text) == ["snapshot", "log", "result", "status"]
    assert '"status": "succeeded"' in response.text


async def test_job_stream_closes_immediately_for_a_terminal_job(
    app: FastAPI, services: Services, session: Session
) -> None:
    user = make_user(session, "alice")
    job = queue_job(session, services, user)
    start(job)
    job.status = JobStatus.FAILED
    job.error = "boom"
    session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/jobs/{job.id}/stream")

    assert response.status_code == 200
    assert _kinds(response.text) == ["snapshot"]


async def test_job_stream_404s_for_an_unknown_job(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/jobs/999/stream")

    assert response.status_code == 404


async def test_job_events_generator_closes_after_a_terminal_status_event(
    services: Services, session: Session
) -> None:
    user = make_user(session, "alice")
    job = queue_job(session, services, user)
    start(job)
    session.commit()

    events = _job_events(job.id, services)
    assert (await events.__anext__()).event == "snapshot"

    services.bus.publish(job_topic(job.id), BusEvent("log", {"message": "hi"}))
    assert (await events.__anext__()).event == "log"

    services.bus.publish(
        job_topic(job.id), BusEvent("status", {"job_id": job.id, "status": "succeeded"})
    )
    assert (await events.__anext__()).event == "status"

    with pytest.raises(StopAsyncIteration):
        await events.__anext__()


async def test_queue_events_generator_coalesces_a_burst_into_one_update(
    services: Services, session: Session
) -> None:
    user = make_user(session, "alice")
    queue_job(session, services, user)

    events = _queue_events(services)
    first = await events.__anext__()
    assert first.event == "queue"

    services.bus.publish(QUEUE_TOPIC, BusEvent("status", {"job_id": 1, "status": "queued"}))
    services.bus.publish(QUEUE_TOPIC, BusEvent("status", {"job_id": 1, "status": "running"}))

    second = await events.__anext__()
    assert second.event == "queue"

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(events.__anext__(), timeout=0.1)


async def test_queue_subscribers_share_one_snapshot_per_change(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    views: list[int] = []

    def counted_queue_view(session: Session, services: Services) -> QueueView:
        views.append(1)
        return queue_view(session, services)

    monkeypatch.setattr("workflows.api.stream.queue_view", counted_queue_view)
    first, second = _queue_events(services), _queue_events(services)
    await first.__anext__()
    await second.__anext__()

    services.bus.publish(QUEUE_TOPIC, BusEvent("status", {"job_id": 1, "status": "queued"}))
    await first.__anext__()
    await second.__anext__()

    assert len(views) == 3
