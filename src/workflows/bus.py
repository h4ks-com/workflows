import asyncio
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from workflows.db import JsonObject

QUEUE_TOPIC = "queue"


def job_topic(job_id: int) -> str:
    return f"job:{job_id}"


@dataclass(frozen=True)
class BusEvent:
    kind: str
    data: JsonObject


class EventBus:
    def __init__(self) -> None:
        self._subscribers: defaultdict[str, set[asyncio.Queue[BusEvent]]] = defaultdict(set)

    def publish(self, topic: str, event: BusEvent) -> None:
        for queue in self._subscribers.get(topic, ()):
            queue.put_nowait(event)

    @contextmanager
    def subscribe(self, topic: str) -> Iterator[asyncio.Queue[BusEvent]]:
        queue: asyncio.Queue[BusEvent] = asyncio.Queue()
        self._subscribers[topic].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[topic].discard(queue)
            if not self._subscribers[topic]:
                del self._subscribers[topic]
