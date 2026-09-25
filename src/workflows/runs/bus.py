import asyncio
import logging
from collections import Counter
from collections import defaultdict
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from workflows.db import JsonObject

logger = logging.getLogger(__name__)

QUEUE_TOPIC = "queue"
SUBSCRIBER_QUEUE_SIZE = 100


def job_topic(job_id: int) -> str:
    return f"job:{job_id}"


@dataclass(frozen=True)
class BusEvent:
    kind: str
    data: JsonObject


class EventBus:
    def __init__(self) -> None:
        self._subscribers: defaultdict[str, set[asyncio.Queue[BusEvent]]] = defaultdict(set)
        self._overflowed: set[asyncio.Queue[BusEvent]] = set()
        self._versions: Counter[str] = Counter()
        self._snapshots: dict[str, tuple[int, JsonObject]] = {}

    def publish(self, topic: str, event: BusEvent) -> None:
        self._versions[topic] += 1
        for queue in self._subscribers.get(topic, ()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._warn_once(topic, queue)

    def _warn_once(self, topic: str, queue: asyncio.Queue[BusEvent]) -> None:
        if queue not in self._overflowed:
            self._overflowed.add(queue)
            logger.warning("dropping events for a slow subscriber of %s", topic)

    def snapshot(self, topic: str, build: Callable[[], JsonObject]) -> JsonObject:
        version = self._versions[topic]
        cached = self._snapshots.get(topic)
        if cached is None or cached[0] != version:
            cached = (version, build())
            self._snapshots[topic] = cached
        return cached[1]

    @contextmanager
    def subscribe(self, topic: str) -> Iterator[asyncio.Queue[BusEvent]]:
        queue: asyncio.Queue[BusEvent] = asyncio.Queue(SUBSCRIBER_QUEUE_SIZE)
        self._subscribers[topic].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[topic].discard(queue)
            self._overflowed.discard(queue)
            if not self._subscribers[topic]:
                del self._subscribers[topic]
