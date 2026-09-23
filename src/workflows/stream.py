import json
from collections.abc import AsyncIterator

from fastapi import APIRouter
from sse_starlette import EventSourceResponse, ServerSentEvent

from workflows.api import get_job_or_404, job_detail_view, queue_view
from workflows.bus import QUEUE_TOPIC, BusEvent, job_topic
from workflows.db import JsonObject
from workflows.jobs import STATUS_EVENT, TERMINAL_STATUSES
from workflows.state import AppServices, Services

router = APIRouter(prefix="/api")


def _event(kind: str, data: JsonObject) -> ServerSentEvent:
    return ServerSentEvent(event=kind, data=json.dumps(data))


def _is_terminal_status_event(event: BusEvent) -> bool:
    return event.kind == STATUS_EVENT and event.data["status"] in TERMINAL_STATUSES


async def _job_events(job_id: int, services: Services) -> AsyncIterator[ServerSentEvent]:
    # We subscribe before reading the snapshot so a status change between the two never
    # gets lost; an event that already happened once more just replays as a duplicate.
    with services.bus.subscribe(job_topic(job_id)) as queue:
        with services.sessions() as session:
            snapshot = job_detail_view(session, services, job_id)
        yield _event("snapshot", snapshot.model_dump(mode="json"))
        if snapshot.status in TERMINAL_STATUSES:
            return
        while True:
            event = await queue.get()
            yield _event(event.kind, event.data)
            if _is_terminal_status_event(event):
                return


async def _queue_events(services: Services) -> AsyncIterator[ServerSentEvent]:
    def build() -> JsonObject:
        with services.sessions() as session:
            return queue_view(session, services).model_dump(mode="json")

    with services.bus.subscribe(QUEUE_TOPIC) as queue:
        yield _event("queue", build())
        while True:
            await queue.get()
            while not queue.empty():
                queue.get_nowait()
            yield _event("queue", services.bus.snapshot(QUEUE_TOPIC, build))


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: int, services: AppServices) -> EventSourceResponse:
    with services.sessions() as session:
        get_job_or_404(session, job_id)
    return EventSourceResponse(_job_events(job_id, services))


@router.get("/queue/stream")
async def stream_queue(services: AppServices) -> EventSourceResponse:
    return EventSourceResponse(_queue_events(services))
